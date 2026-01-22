#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 7 - Continuous Operation & Cleanup (BingX)
================================================
Persistent maintenance layer that runs alongside all other stages.

Implements:
- Orphan/stale entry order cleanup (24h + 6d)
- Reconcile/restore open positions after restart/connectivity recovery
- Protection repair (best-effort, exchange-confirmed only)

Principles:
- BingX-first: only exchange-confirmed state changes drive DB transitions.
- Conservative behavior: prefer alerting over guessing when signal mapping is unclear.
- Thread-safe: DB work is done via asyncio.to_thread and store locks.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import config
from bingx_client import BingXClient
from lifecycle_store import LifecycleStore
from ssot_store import SignalStore
from stage6_telemetry import TelemetryLogger, TelemetryCorrelation
from stage6_telegram import send_telegram_with_telemetry

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        txt = str(s).strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _norm_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("-", "").replace("/", "").strip()


def _norm_side(side: str) -> str:
    s = (side or "").upper().strip()
    if s in {"LONG", "SHORT"}:
        return s
    if s in {"BUY"}:
        return "LONG"
    if s in {"SELL"}:
        return "SHORT"
    return s


def _d(x: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if not s:
            return default
        return Decimal(s)
    except Exception:
        return default


@dataclass(frozen=True)
class Stage7Config:
    cleanup_short_interval_s: int
    cleanup_long_interval_s: int
    reconcile_interval_s: int
    timeout_short: timedelta
    timeout_long: timedelta


class Stage7Maintenance:
    def __init__(
        self,
        *,
        bingx: BingXClient,
        ssot_store: SignalStore,
        lifecycle_store: LifecycleStore,
        telegram_client=None,
        telegram_chat_id: Optional[str] = None,
        telemetry: Optional[TelemetryLogger] = None,
        stage4_manager: Optional[object] = None,
        worker_id: str = "stage7-maintenance",
    ):
        self.bingx = bingx
        self.ssot_store = ssot_store
        self.lifecycle_store = lifecycle_store
        self.telegram_client = telegram_client
        self.telegram_chat_id = telegram_chat_id or getattr(config, "PERSONAL_CHANNEL_ID", None)
        self.telemetry = telemetry
        self.stage4_manager = stage4_manager  # optional; if present we may call placement helper
        self.worker_id = worker_id

        self._lock = asyncio.Lock()

        self._cfg = Stage7Config(
            cleanup_short_interval_s=max(int(getattr(config, "STAGE7_CLEANUP_SHORT_INTERVAL_SECONDS", 900)), 30),
            cleanup_long_interval_s=max(int(getattr(config, "STAGE7_CLEANUP_LONG_INTERVAL_SECONDS", 6 * 3600)), 60),
            reconcile_interval_s=max(int(getattr(config, "STAGE7_RECONCILE_INTERVAL_SECONDS", 120)), 10),
            timeout_short=getattr(config, "TIMEOUT_SHORT", timedelta(hours=24)),
            timeout_long=getattr(config, "TIMEOUT_LONG", timedelta(days=6)),
        )

    async def run_forever(self) -> None:
        """
        Run Stage 7 jobs forever:
        - reconcile loop
        - 24h cleanup loop
        - 6d cleanup loop
        """
        # Do an immediate reconcile on boot for fail-safe behavior.
        try:
            await self._reconcile_once(reason="startup")
        except Exception as e:
            logger.error("Stage 7 initial reconcile failed: %s", e, exc_info=True)

        tasks = [
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._cleanup_short_loop()),
            asyncio.create_task(self._cleanup_long_loop()),
        ]
        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Cleanup loops
    # ------------------------------------------------------------------
    async def _cleanup_short_loop(self) -> None:
        while True:
            try:
                await self._cleanup_stage2_stale_once(
                    age=self._cfg.timeout_short,
                    marker_status="CLEANED_24H",
                    marker_reason="Stage7: stale entry orders canceled (24h)",
                )
            except Exception as e:
                logger.error("Stage 7 24h cleanup error: %s", e, exc_info=True)
            await asyncio.sleep(self._cfg.cleanup_short_interval_s)

    async def _cleanup_long_loop(self) -> None:
        while True:
            try:
                await self._cleanup_stage2_stale_once(
                    age=self._cfg.timeout_long,
                    marker_status="CLEANED_6D",
                    marker_reason="Stage7: stale entry orders canceled (6d)",
                )
                await self._cleanup_stage4_stale_once(age=self._cfg.timeout_long)
            except Exception as e:
                logger.error("Stage 7 6d cleanup error: %s", e, exc_info=True)
            await asyncio.sleep(self._cfg.cleanup_long_interval_s)

    async def _cleanup_stage2_stale_once(self, *, age: timedelta, marker_status: str, marker_reason: str) -> None:
        """
        Scan ssot_queue for stale Stage 2 rows and cancel their entry orders if no live position exists.
        """
        async with self._lock:
            min_age_s = int(max(age.total_seconds(), 0))
            statuses = [
                "CLAIMED",
                "STAGE2_RUNNING",
                "STAGE2_PLANNED",
                "WAITING_FOR_FILLS",
                "EXPIRED",
                "FAILED",
            ]
            rows = await asyncio.to_thread(
                self.ssot_store.list_stage2_rows_older_than,
                min_age_seconds=min_age_s,
                statuses=statuses,
                limit=300,
            )
            if not rows:
                return

            cleaned = 0
            for r in rows:
                ssot_id = int(r["id"])
                symbol = r.get("symbol")
                side_norm = _norm_side(r.get("side"))
                stage2_json = r.get("stage2_json")
                order_ids = self._extract_stage2_order_ids(stage2_json)
                if not symbol or side_norm not in {"LONG", "SHORT"} or not order_ids:
                    continue

                has_pos = await self._has_exchange_position(symbol=symbol, side_norm=side_norm)
                if has_pos:
                    continue

                formatted_symbol = self.bingx._format_symbol(symbol)
                canceled_any = False
                for oid in order_ids:
                    did = await self._cancel_if_open(formatted_symbol=formatted_symbol, order_id=str(oid))
                    canceled_any = canceled_any or did

                if canceled_any:
                    cleaned += 1
                    await asyncio.to_thread(
                        self.ssot_store.mark_queue_row,
                        ssot_id=ssot_id,
                        status=marker_status,
                        last_error=marker_reason,
                    )

                    if self.telemetry is not None:
                        self.telemetry.emit(
                            event_type="STAGE7_CLEANUP",
                            level="INFO",
                            subsystem="STAGE7",
                            message=marker_reason,
                            correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                            payload={"symbol": symbol, "side": side_norm, "order_ids": order_ids, "marker": marker_status},
                        )

                    await self._send_telegram(
                        text=(
                            "🧹 Stage 7 CLEANUP\n"
                            f"Type: {marker_status}\n"
                            f"ssot_id={ssot_id}\n"
                            f"symbol={symbol}\n"
                            f"side={side_norm}\n"
                            f"canceled_orders={len(order_ids)}\n"
                            f"time_utc={_utc_now_iso()}"
                        ),
                        ssot_id=ssot_id,
                    )

            if cleaned and self.telemetry is not None:
                self.telemetry.emit(
                    event_type="STAGE7_CLEANUP_SUMMARY",
                    level="INFO",
                    subsystem="STAGE7",
                    message="Cleanup summary",
                    payload={"marker": marker_status, "cleaned_count": int(cleaned)},
                )

    async def _cleanup_stage4_stale_once(self, *, age: timedelta) -> None:
        """
        Long-term reconciliation: for Stage 4 positions older than age with no live exchange position,
        cancel tracked orders and hard-close the internal position.
        """
        async with self._lock:
            min_age_s = int(max(age.total_seconds(), 0))
            positions = await asyncio.to_thread(self.lifecycle_store.list_positions_not_closed, limit=800)
            if not positions:
                return

            now = _utc_now()
            closed = 0
            for pos in positions:
                ssot_id = int(pos["ssot_id"])
                created = _parse_iso_utc(pos.get("created_at_utc"))
                if created is None:
                    continue
                if int((now - created).total_seconds()) < min_age_s:
                    continue

                symbol = pos.get("symbol")
                side_norm = _norm_side(pos.get("side"))
                if not symbol or side_norm not in {"LONG", "SHORT"}:
                    continue
                status = (pos.get("status") or "").upper()
                if status == "HEDGE_MODE":
                    # Stage 5 owns the controlling logic in hedge mode; don't interfere.
                    continue

                has_pos = await self._has_exchange_position(symbol=symbol, side_norm=side_norm)
                if has_pos:
                    continue

                tracked = await asyncio.to_thread(self.lifecycle_store.list_tracked_orders_for_ssot_id, ssot_id=ssot_id, limit=2000)
                formatted_symbol = self.bingx._format_symbol(symbol)
                for t in tracked:
                    await self._cancel_if_open(formatted_symbol=formatted_symbol, order_id=str(t.get("order_id")))

                await asyncio.to_thread(
                    self.lifecycle_store.update_position,
                    ssot_id=ssot_id,
                    status="CLOSED",
                    remaining_qty="0",
                    closed_reason="Stage7: hard reset (6d) - no exchange position",
                    closed_at_utc=_utc_now_iso(),
                )
                await asyncio.to_thread(self.lifecycle_store.delete_tracked_orders_for_ssot_id, ssot_id=ssot_id)
                closed += 1

                if self.telemetry is not None:
                    self.telemetry.emit(
                        event_type="STAGE7_STAGE4_HARD_CLOSE",
                        level="WARNING",
                        subsystem="STAGE7",
                        message="Hard closed stale Stage4 position (no exchange position)",
                        correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                        payload={"symbol": symbol, "side": side_norm},
                    )

            if closed and self.telemetry is not None:
                self.telemetry.emit(
                    event_type="STAGE7_STAGE4_HARD_CLOSE_SUMMARY",
                    level="INFO",
                    subsystem="STAGE7",
                    message="Stage4 hard close summary",
                    payload={"closed_count": int(closed)},
                )

    # ------------------------------------------------------------------
    # Reconcile / restore loop
    # ------------------------------------------------------------------
    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self._reconcile_once(reason="periodic")
            except Exception as e:
                logger.error("Stage 7 reconcile error: %s", e, exc_info=True)
            await asyncio.sleep(self._cfg.reconcile_interval_s)

    async def _reconcile_once(self, *, reason: str) -> None:
        """
        Reconcile open exchange positions into internal Stage 4 store.

        Deterministic behavior:
        - If we can map (symbol, side) -> latest ssot_id, we restore Stage 4 row + protections.
        - If mapping is unclear, we alert instead of guessing.
        """
        async with self._lock:
            positions = await asyncio.to_thread(self.bingx.get_positions, None)
            if not positions:
                return

            restored = 0
            repaired = 0
            unknown = 0

            for p in positions:
                sym_raw = p.get("symbol") or p.get("symbolName") or ""
                symbol_norm = _norm_symbol(sym_raw)
                if not symbol_norm:
                    continue

                # Normalize to our internal (BTCUSDT-like) form for mapping. BingXClient expects raw "BTCUSDT".
                symbol_internal = symbol_norm

                side_norm = _norm_side(p.get("positionSide") or p.get("side") or "")
                if side_norm not in {"LONG", "SHORT"}:
                    # Best-effort infer from position amount sign.
                    amt = _d(p.get("positionAmt"), Decimal("0"))
                    if amt > 0:
                        side_norm = "LONG"
                    elif amt < 0:
                        side_norm = "SHORT"
                    else:
                        continue

                # Ignore zero-size positions.
                amt = _d(p.get("positionAmt") or p.get("positionSize") or p.get("positionQty"), Decimal("0"))
                if amt == 0:
                    continue

                # Map to latest ssot_id for (symbol, side)
                ssot_id = await asyncio.to_thread(
                    self.ssot_store.find_latest_ssot_id_for_symbol_side,
                    symbol=symbol_internal,
                    side=side_norm,
                )
                if ssot_id is None:
                    # Before alerting, check if this is a known Stage 5 hedge position
                    is_known_hedge = await self._check_if_hedge_position(symbol_internal, side_norm, amt)
                    if is_known_hedge:
                        # This is a managed hedge - skip the warning
                        continue
                    
                    unknown += 1
                    await self._send_telegram(
                        text=(
                            "⚠️ Stage 7: Unmapped open position detected\n"
                            f"symbol={symbol_internal}\n"
                            f"side={side_norm}\n"
                            f"qty={amt}\n"
                            f"reason=No matching ssot_queue row\n"
                            f"time_utc={_utc_now_iso()}"
                        ),
                        ssot_id=None,
                    )
                    continue

                pos_row = await asyncio.to_thread(self.lifecycle_store.get_position, ssot_id=int(ssot_id))
                if pos_row is None:
                    ok = await self._restore_stage4_from_ssot(ssot_id=int(ssot_id), symbol=symbol_internal, side_norm=side_norm, exchange_pos=p)
                    if ok:
                        restored += 1
                        pos_row = await asyncio.to_thread(self.lifecycle_store.get_position, ssot_id=int(ssot_id))

                if pos_row is None:
                    continue

                status = (pos_row.get("status") or "").upper()
                if status == "HEDGE_MODE":
                    continue

                did_repair = await self._ensure_protections(ssot_id=int(ssot_id))
                repaired += 1 if did_repair else 0

                # Mark reconcile timestamp
                await asyncio.to_thread(self.lifecycle_store.update_position, ssot_id=int(ssot_id), last_reconcile_at_utc=_utc_now_iso())

            if self.telemetry is not None and (restored or repaired or unknown):
                self.telemetry.emit(
                    event_type="STAGE7_RECONCILE_SUMMARY",
                    level="INFO",
                    subsystem="STAGE7",
                    message="Reconcile summary",
                    payload={"reason": reason, "restored": int(restored), "repaired": int(repaired), "unknown": int(unknown)},
                )

    async def _check_if_hedge_position(self, symbol: str, side: str, qty: Decimal) -> bool:
        """
        Check if an "unmapped" position is actually a Stage 5 hedge.
        
        Strategy:
        - Look for the opposite-side signal for the same symbol
        - Check if that position has Stage 5 hedge tracking fields populated
        - If yes, this is a known hedge
        """
        opposite_side = "SHORT" if side == "LONG" else "LONG"
        
        # Find the opposite-side signal
        parent_ssot_id = await asyncio.to_thread(
            self.ssot_store.find_latest_ssot_id_for_symbol_side,
            symbol=symbol,
            side=opposite_side,
        )
        if parent_ssot_id is None:
            return False
        
        # Check if it has hedge tracking
        pos_row = await asyncio.to_thread(self.lifecycle_store.get_position, ssot_id=int(parent_ssot_id))
        if pos_row is None:
            return False
        
        # Check hedge state (both old and new field names)
        hedge_state = (pos_row.get("stage5_hedge_state") or pos_row.get("stage5_hedge_status") or "").upper()
        if hedge_state in {"OPEN", "HEDGE_MODE"}:
            # This position has an active hedge - the unmapped position is likely it
            logger.info(
                "Stage 7: Recognized hedge position (symbol=%s, side=%s, qty=%s, parent_ssot_id=%s)",
                symbol, side, qty, parent_ssot_id
            )
            return True
        
        return False

    async def _restore_stage4_from_ssot(self, *, ssot_id: int, symbol: str, side_norm: str, exchange_pos: dict) -> bool:
        """
        Create Stage 4 position row from ssot_queue + exchange position snapshot.
        """
        q = await asyncio.to_thread(self.ssot_store.get_queue_row, ssot_id=ssot_id)
        if not q:
            return False

        # Build TP ladder from signal
        tp_prices = []
        try:
            tp_prices = json.loads(q.get("tp_prices_json") or "[]")
        except Exception:
            tp_prices = []
        tp_levels: List[Dict[str, Any]] = []
        for i, tp in enumerate(tp_prices or []):
            tp_levels.append({"index": i, "price": str(tp), "status": "OPEN", "filled_qty": "0", "order_id": None})

        planned_qty = None
        stage2_state = {}
        try:
            stage2_state = json.loads(q.get("stage2_json") or "{}")
        except Exception:
            stage2_state = {}
        if isinstance(stage2_state, dict):
            if stage2_state.get("Q") is not None:
                planned_qty = str(stage2_state.get("Q"))

        remaining_qty = str(abs(_d(exchange_pos.get("positionAmt"), Decimal("0")) or _d(exchange_pos.get("positionSize"), Decimal("0"))))
        avg_entry = None
        ae = _d(exchange_pos.get("avgPrice") or exchange_pos.get("avgEntryPrice") or exchange_pos.get("entryPrice"), Decimal("0"))
        if ae > 0:
            avg_entry = str(ae)

        inserted = await asyncio.to_thread(
            self.lifecycle_store.create_position_if_absent,
            ssot_id=int(ssot_id),
            symbol=str(symbol),
            side=str(side_norm),
            status="OPEN",
            planned_qty=planned_qty,
            remaining_qty=remaining_qty if remaining_qty != "0" else planned_qty,
            avg_entry=avg_entry,
            sl_price=str(q.get("sl_price")) if q.get("sl_price") is not None else None,
            signal_entry_price=str(q.get("entry_price")) if q.get("entry_price") is not None else None,
            signal_sl_price=str(q.get("sl_price")) if q.get("sl_price") is not None else None,
            signal_leverage=None,
            orig_entry_price=str(q.get("entry_price")) if q.get("entry_price") is not None else None,
            orig_sl_price=str(q.get("sl_price")) if q.get("sl_price") is not None else None,
            orig_leverage=None,
            tp_levels=tp_levels,
        )
        if not inserted:
            return True

        # Track Stage2 entry orders if present
        order_ids = self._extract_stage2_order_ids(q.get("stage2_json"))
        for oid in order_ids:
            await asyncio.to_thread(self.lifecycle_store.upsert_order_tracker, ssot_id=int(ssot_id), order_id=str(oid), kind="ENTRY", level_index=None)

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="STAGE7_RESTORED_STAGE4_ROW",
                level="WARNING",
                subsystem="STAGE7",
                message="Restored Stage4 position row from exchange snapshot",
                correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}"),
                payload={"symbol": symbol, "side": side_norm},
            )

        await self._send_telegram(
            text=(
                "🔄 Stage 7 RESTORE\n"
                "SYSTEM ÅTERANSLUTET\n"
                "ÅTERSTÄLLNING GENOMFÖRD\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={symbol}\n"
                f"side={side_norm}\n"
                f"time_utc={_utc_now_iso()}"
            ),
            ssot_id=ssot_id,
        )
        return True

    async def _ensure_protections(self, *, ssot_id: int) -> bool:
        """
        Ensure SL/TP orders exist for a Stage4 position row.
        Returns True if any repair action was taken.
        """
        pos = await asyncio.to_thread(self.lifecycle_store.get_position, ssot_id=ssot_id)
        if not pos:
            return False

        if (pos.get("status") or "").upper() in {"CLOSED", "HEDGE_MODE"}:
            return False

        symbol = pos.get("symbol")
        side_norm = _norm_side(pos.get("side"))
        if not symbol or side_norm not in {"LONG", "SHORT"}:
            return False

        # If exchange already has open orders for this symbol, avoid guessing and alert instead.
        # (Prevents accidental duplicate protections when user placed manual orders.)
        open_orders = await asyncio.to_thread(self.bingx.get_open_orders, symbol)
        # logger.info("Stage 7: Checking open orders for protection repair: symbol=%s, open_orders=%s", symbol, open_orders.__getitem__(0))
        if open_orders:
            # Best-effort: if we already have tracked order ids, we can still repair missing ones.
            # If nothing is tracked, it's ambiguous -> alert.
            has_any_tracked = bool(pos.get("sl_order_id")) or any((lvl or {}).get("order_id") for lvl in (pos.get("tp_levels") or []))
            if not has_any_tracked:
                # Avoid spamming the same warning every reconcile tick. Once we mark the row as
                # NEEDS_MANUAL_PROTECTION, we only re-alert if the status changes back.
                if (pos.get("status") or "").upper() == "NEEDS_MANUAL_PROTECTION":
                    return False
                await asyncio.to_thread(self.lifecycle_store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
                await self._send_telegram(
                    text=(
                        "⚠️ Stage 7: Protection ambiguous (open orders exist)\n"
                        f"ssot_id={ssot_id}\n"
                        f"symbol={symbol}\n"
                        f"side={side_norm}\n"
                        "action=NOT placing TP/SL automatically\n"
                        f"time_utc={_utc_now_iso()}"
                    ),
                    ssot_id=ssot_id,
                )
                return False

        repaired = False

        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        if remaining <= 0:
            return False

        # 1) Place missing TP orders (reduce-only)
        tp_levels = pos.get("tp_levels") or []
        if tp_levels and any((lvl.get("order_id") in (None, "", 0)) for lvl in tp_levels):
            tp_side = "SELL" if side_norm == "LONG" else "BUY"
            n = len(tp_levels)
            per = remaining / Decimal(str(n)) if n > 0 else remaining
            q_allocs: List[Decimal] = []
            for i in range(n):
                q_allocs.append(per if i < n - 1 else (remaining - sum(q_allocs)))

            formatted_symbol = self.bingx._format_symbol(symbol)
            for lvl, q in zip(tp_levels, q_allocs):
                if q <= 0:
                    continue
                if lvl.get("order_id"):
                    continue
                price = _d(lvl.get("price"), Decimal("0"))
                if price <= 0:
                    continue
                resp = await asyncio.to_thread(
                    self.bingx.place_limit_order,
                    symbol=formatted_symbol,
                    side=tp_side,
                    price=price,
                    quantity=q,
                    leverage=Decimal("1"),
                    post_only=False,
                    time_in_force="GTC",
                    reduce_only=True,
                    position_side=side_norm,
                )
                oid = resp.get("orderId")
                if oid:
                    lvl["order_id"] = str(oid)
                    repaired = True
                    await asyncio.to_thread(
                        self.lifecycle_store.upsert_order_tracker,
                        ssot_id=ssot_id,
                        order_id=str(oid),
                        kind="TP",
                        level_index=int(lvl.get("index", 0)),
                    )

            await asyncio.to_thread(self.lifecycle_store.update_position, ssot_id=ssot_id, tp_levels=tp_levels)

        # 2) Place missing SL (reduce-only stop market)
        if not pos.get("sl_order_id"):
            sl_price = _d(pos.get("sl_price"), Decimal("0"))
            if sl_price > 0:
                sl_side = "SELL" if side_norm == "LONG" else "BUY"
                resp = await asyncio.to_thread(
                    self.bingx.place_stop_market_order,
                    symbol=symbol,
                    side=sl_side,
                    stop_price=sl_price,
                    quantity=remaining,
                    reduce_only=True,
                    position_side=side_norm,
                )
                oid = resp.get("orderId")
                if oid:
                    repaired = True
                    await asyncio.to_thread(self.lifecycle_store.update_position, ssot_id=ssot_id, sl_order_id=str(oid))
                    await asyncio.to_thread(self.lifecycle_store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)
                else:
                    await asyncio.to_thread(self.lifecycle_store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
                    await self._send_telegram(
                        text=(
                            "⚠️ Stage 7: SL placement failed (needs manual protection)\n"
                            f"ssot_id={ssot_id}\n"
                            f"symbol={symbol}\n"
                            f"side={side_norm}\n"
                            f"sl={sl_price}\n"
                            f"error={resp.get('error') or resp.get('raw')}\n"
                            f"time_utc={_utc_now_iso()}"
                        ),
                        ssot_id=ssot_id,
                    )

        if repaired and self.telemetry is not None:
            self.telemetry.emit(
                event_type="STAGE7_PROTECTION_REPAIRED",
                level="INFO",
                subsystem="STAGE7",
                message="Protection repaired",
                correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                payload={"symbol": symbol, "side": side_norm},
            )
        return repaired

    # ------------------------------------------------------------------
    # Exchange helpers
    # ------------------------------------------------------------------
    async def _has_exchange_position(self, *, symbol: str, side_norm: str) -> bool:
        sym = _norm_symbol(symbol)
        positions = await asyncio.to_thread(self.bingx.get_positions, symbol)
        for p in positions or []:
            psym = _norm_symbol(p.get("symbol") or p.get("symbolName") or "")
            if psym != sym:
                continue
            pside = _norm_side(p.get("positionSide") or p.get("side") or "")
            if pside not in {"LONG", "SHORT"}:
                amt = _d(p.get("positionAmt"), Decimal("0"))
                if amt > 0:
                    pside = "LONG"
                elif amt < 0:
                    pside = "SHORT"
            if pside != side_norm:
                continue
            amt = _d(p.get("positionAmt") or p.get("positionSize") or p.get("positionQty"), Decimal("0"))
            if amt != 0:
                return True
        return False

    async def _cancel_if_open(self, *, formatted_symbol: str, order_id: str) -> bool:
        """
        Cancel order if it's in an open status. Returns True if we issued a cancel attempt.
        """
        if not order_id:
            return False
        st = await asyncio.to_thread(self.bingx.get_order_status, formatted_symbol, str(order_id))
        if not st:
            return False
        status = (st.get("status") or "").upper()
        if status not in {"NEW", "PARTIALLY_FILLED"}:
            return False
        try:
            await asyncio.to_thread(self.bingx.cancel_order, formatted_symbol, str(order_id))
        except Exception:
            pass
        return True

    @staticmethod
    def _extract_stage2_order_ids(stage2_json: Optional[str]) -> List[str]:
        if not stage2_json:
            return []
        try:
            d = json.loads(stage2_json)
        except Exception:
            return []
        if not isinstance(d, dict):
            return []
        orders = d.get("orders") or {}
        if not isinstance(orders, dict):
            return []
        out: List[str] = []
        original = orders.get("original") or []
        if isinstance(original, list):
            out.extend([str(x) for x in original if x])
        rep = orders.get("replacement")
        if rep:
            out.append(str(rep))
        # Dedup preserve order
        seen = set()
        uniq: List[str] = []
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq

    # ------------------------------------------------------------------
    # Telegram helper
    # ------------------------------------------------------------------
    async def _send_telegram(self, *, text: str, ssot_id: Optional[int]) -> None:
        if not self.telegram_client or not self.telegram_chat_id:
            return
        try:
            corr = None
            if ssot_id is not None:
                corr = TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}")
            await send_telegram_with_telemetry(
                telegram_client=self.telegram_client,
                chat_id=str(self.telegram_chat_id),
                text=text,
                telemetry=self.telemetry,
                correlation=corr,
            )
        except Exception:
            # Telegram must be best-effort.
            return


