#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 1 - Signal Ingestion & Normalization
==========================================
Implements the exact Stage 1 flow:

Receive Telegram Signal
  -> Validate Format (entry, TP, SL, type)
  -> Normalize Data (side, symbol, tick/step, map labels -> canonical)
  -> Deduplicate (HASH + TTL + % diff rules)
  -> Accepted? If yes: add to SQLite SSoT queue; if no: block + log/notify

Author: Trading Bot Project
Date: 2026-01-15
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple

import config
from bingx_client import BingXClient
from signal_parser import SignalParser
from ssot_store import SignalStore, StoredSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stage1Decision:
    status: str  # ACCEPTED | BLOCKED | INVALID
    reason: str
    details: dict
    stored_signal_id: Optional[int] = None


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_side(raw: str) -> Optional[str]:
    s = (raw or "").upper()
    if s in {"LONG", "BUY"}:
        return "LONG"
    if s in {"SHORT", "SELL"}:
        return "SHORT"
    return None


def _normalize_symbol(raw_symbol: str) -> Optional[str]:
    if not raw_symbol:
        return None
    s = raw_symbol.upper().strip()
    s = s.replace("#", "")
    s = s.replace("/", "")
    s = s.replace("-", "")
    s = re.sub(r"\s+", "", s)
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s


def _detect_type(message_text: str) -> Optional[str]:
    """
    Detect required signal type: SWING | DYNAMISK | FAST
    Deterministic mapping based on keywords.
    """
    t = (message_text or "").lower()
    if "swing" in t:
        return "SWING"
    if "dynamic" in t or "dynamisk" in t:
        return "DYNAMISK"
    if "fast" in t or "fixed" in t:
        return "FAST"
    return None


def _classify_type_from_leverage(leverage: Decimal) -> Optional[str]:
    """
    Deterministic leverage-classification (per spec):
    - SWING: lev ≤ 6.00x
    - DYNAMISK: lev ≥ 7.50x
    - 6.00x < lev < 7.50x: nearest of SWING/DYNAMISK
    """
    if leverage is None:
        return None

    lev = Decimal(leverage)
    swing_max = Decimal("6.00")
    dynamisk_min = Decimal("7.50")

    if lev <= swing_max:
        return "SWING"
    if lev >= dynamisk_min:
        return "DYNAMISK"

    # Intermediate range: classify to nearest threshold.
    # Tie-breaker: prefer SWING (safer).
    dist_to_swing = lev - swing_max
    dist_to_dyn = dynamisk_min - lev
    if dist_to_swing <= dist_to_dyn:
        return "SWING"
    return "DYNAMISK"


def _entry_price_from_entry_data(entry_data: dict) -> Optional[Decimal]:
    if not entry_data:
        return None
    if entry_data.get("type") == "price":
        return entry_data.get("price")
    if entry_data.get("type") == "zone":
        return entry_data.get("midpoint")
    return None


def _auto_sl(entry: Decimal, side: str) -> Decimal:
    """
    Deterministic fallback: if SL missing -> SL = -2.00% from entry (LONG), +2.00% (SHORT)
    """
    if side == "LONG":
        return (entry * (Decimal("1.00") - Decimal("0.02"))).quantize(Decimal("0.00000001"))
    return (entry * (Decimal("1.00") + Decimal("0.02"))).quantize(Decimal("0.00000001"))


def _percent_diff(a: Decimal, b: Decimal) -> Decimal:
    if a is None or b is None:
        return Decimal("999")
    if a == 0:
        return Decimal("999")
    return (abs(a - b) / abs(a)).copy_abs()


def _max_component_diff(
    *,
    entry_a: Decimal,
    sl_a: Decimal,
    tps_a: List[Decimal],
    entry_b: Decimal,
    sl_b: Decimal,
    tps_b: List[Decimal],
) -> Decimal:
    # If TP count differs, treat as not "in principle identical" -> accept path
    if len(tps_a) != len(tps_b):
        return Decimal("1.00")

    diffs: List[Decimal] = [
        _percent_diff(entry_a, entry_b),
        _percent_diff(sl_a, sl_b),
    ]
    for tp_a, tp_b in zip(tps_a, tps_b):
        diffs.append(_percent_diff(tp_a, tp_b))
    return max(diffs) if diffs else Decimal("1.00")


def _safe_decimal(value, default: Decimal) -> Decimal:
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        return Decimal(s)
    except Exception:
        return default


def _entry_bucket(entry: Decimal) -> int:
    """
    Deterministic 5-10% rule tie-breaker.
    Bucket entry into 1% bands and compare bucket index.
    """
    step = entry * Decimal("0.01")
    if step <= 0:
        return 0
    return int((entry / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class SignalIngestionNormalizerProcessor:
    def __init__(self, store: SignalStore):
        self.store = store
        self.parser = SignalParser()
        self.bingx = BingXClient(testnet=config.BINGX_TESTNET)
        self._symbol_info_cache: dict[str, dict] = {}

    def _get_symbol_info(self, symbol_usdt: str) -> Optional[dict]:
        # Cache within process lifetime for speed/determinism
        if symbol_usdt in self._symbol_info_cache:
            return self._symbol_info_cache[symbol_usdt]
        info = self.bingx.get_symbol_info(symbol_usdt)
        if info:
            self._symbol_info_cache[symbol_usdt] = info
        return info

    def process(
        self,
        *,
        channel_name: str,
        chat_id: str,
        message_id: int,
        message_dt: Optional[datetime],
        raw_text: str,
    ) -> Stage1Decision:
        # 1) Receive Telegram Signal (raw captured in memory here; no forwarding)
        raw_text = (raw_text or "").strip()
        if not raw_text:
            return Stage1Decision(status="INVALID", reason="Empty message text", details={})

        # 2) Validate format via strict parsing
        parsed = self.parser.parse_signal(raw_text)
        if not parsed:
            return Stage1Decision(status="INVALID", reason="Parse failed (missing symbol/direction)", details={})

        symbol = _normalize_symbol(parsed.get("symbol"))
        side = _normalize_side(parsed.get("direction"))
        entry_price = _entry_price_from_entry_data(parsed.get("entry") or {})
        tp_prices = [tp.get("price") for tp in (parsed.get("tp_list") or []) if tp.get("price") is not None]
        sl_price = parsed.get("sl_price")
        leverage = parsed.get("leverage")

        # Resolve required type: keywords -> leverage-classification -> channel/default fallback.
        signal_type = _detect_type(raw_text)
        if signal_type is None and leverage is not None:
            signal_type = _classify_type_from_leverage(leverage)
        if signal_type is None:
            channel_defaults = getattr(config, "DEFAULT_SIGNAL_TYPE_BY_CHANNEL", {}) or {}
            default_type = channel_defaults.get(channel_name) or getattr(config, "DEFAULT_SIGNAL_TYPE_WHEN_MISSING", None)
            if default_type:
                signal_type = str(default_type).upper()
                logger.warning(
                    "Type missing in message text; defaulting to %s (channel=%s, leverage=%s)",
                    signal_type,
                    channel_name,
                    leverage,
                )

        # Mandatory fields: symbol, side, entry, at least 1 TP, type
        if not symbol:
            return Stage1Decision(status="INVALID", reason="Missing/invalid symbol", details={"parsed": parsed})
        if side not in {"LONG", "SHORT"}:
            return Stage1Decision(status="INVALID", reason="Missing/invalid side", details={"parsed": parsed})
        if entry_price is None:
            return Stage1Decision(status="INVALID", reason="Missing entry", details={"parsed": parsed})
        if not tp_prices:
            return Stage1Decision(status="INVALID", reason="Missing TP", details={"parsed": parsed})

        # SL missing -> apply FAST fallback (deterministic)
        if sl_price is None:
            sl_price = _auto_sl(entry_price, side)
            signal_type = "FAST"

        if signal_type not in {"SWING", "DYNAMISK", "FAST"}:
            return Stage1Decision(status="INVALID", reason="Missing/invalid type", details={"parsed": parsed})

        # 3) Normalize data (symbol & side already normalized)
        symbol_info = self._get_symbol_info(symbol)
        if not symbol_info:
            return Stage1Decision(
                status="INVALID",
                reason="Unsupported symbol (not found in exchange instrument list)",
                details={"symbol": symbol},
            )

        tick_size = Decimal(str(symbol_info.get("tickSize", "0")))
        lot = symbol_info.get("lotSizeFilter", {}) or {}
        qty_step = Decimal(str(lot.get("qtyStep", "0")))

        # Guard against malformed exchange metadata (e.g., None/"" causing Decimal ConversionSyntax)
        tick_size = _safe_decimal(symbol_info.get("tickSize"), Decimal("0"))
        qty_step = _safe_decimal(lot.get("qtyStep"), Decimal("0"))

        if tick_size <= 0:
            logger.warning("Invalid tickSize from exchange metadata (symbol=%s, tickSize=%r)", symbol, symbol_info.get("tickSize"))
        if qty_step <= 0:
            logger.warning("Invalid qtyStep from exchange metadata (symbol=%s, qtyStep=%r)", symbol, lot.get("qtyStep"))

        # Quantize prices to tick size
        # Use BingX client's quantize implementation for consistency
        entry_q = self.bingx._quantize_price(Decimal(entry_price), tick_size)
        sl_q = self.bingx._quantize_price(Decimal(sl_price), tick_size)
        tps_q = [self.bingx._quantize_price(Decimal(tp), tick_size) for tp in tp_prices]

        normalized = StoredSignal(
            source_channel_name=channel_name,
            chat_id=str(chat_id),
            message_id=int(message_id),
            message_ts_utc=_utc_iso(message_dt),
            received_at_utc=datetime.now(timezone.utc).isoformat(),
            raw_text=raw_text,
            symbol=symbol,
            side=side,
            entry_price=str(entry_q),
            sl_price=str(sl_q),
            tp_prices=[str(x) for x in tps_q],
            signal_type=signal_type,
            tick_size=str(tick_size),
            qty_step=str(qty_step),
        )

        # 4) Deduplicate (TTL + % diff rules + hash)
        ttl_hours = int(getattr(config, "DUPLICATE_TTL_HOURS", 2))
        dedup = self.store.check_and_record_dedup(normalized, ttl_hours=ttl_hours)
        if dedup["decision"] == "BLOCK":
            return Stage1Decision(
                status="BLOCKED",
                reason=dedup["reason"],
                details={
                    "dedup": dedup,
                    "normalized": {
                        "symbol": normalized.symbol,
                        "side": normalized.side,
                        "entry_price": normalized.entry_price,
                        "sl_price": normalized.sl_price,
                        "tp_prices": normalized.tp_prices,
                        "type": normalized.signal_type,
                        "tick_size": normalized.tick_size,
                        "qty_step": normalized.qty_step,
                    },
                },
            )

        # 6) Add to SSoT queue (SQLite) ONLY after acceptance
        stored_id = self.store.insert_accepted_signal(normalized=normalized, dedup_hash=dedup["dedup_hash"])

        return Stage1Decision(
            status="ACCEPTED",
            reason="Signal accepted",
            details={
                "dedup": dedup,
                "normalized": {
                    "symbol": normalized.symbol,
                    "side": normalized.side,
                    "entry_price": normalized.entry_price,
                    "sl_price": normalized.sl_price,
                    "tp_prices": normalized.tp_prices,
                    "type": normalized.signal_type,
                    "tick_size": normalized.tick_size,
                    "qty_step": normalized.qty_step,
                },
            },
            stored_signal_id=stored_id,
        )


