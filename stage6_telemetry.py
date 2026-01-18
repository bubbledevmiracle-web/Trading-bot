#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 6 - Telemetry (JSONL SSOT)
================================
Lossless structured logging used as the Single Source of Truth for:
- Reporting (daily/weekly)
- Error statistics
- Audit reconstruction (correlated by ssot_id / bot_order_id / bingx_order_id / position_id)

Design goals:
- Append-only JSONL (one event per line)
- Deterministic keys and redaction (never log secrets)
- Thread-safe (Stage 2/4/5 use asyncio.to_thread)

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _redact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        s = value
        if len(s) <= 8:
            return "***"
        return s[:4] + "***" + s[-2:]
    return "***"


def redact_dict(payload: Any, *, redact_keys: Optional[set[str]] = None) -> Any:
    """
    Recursively redact sensitive keys in dictionaries/lists.
    """
    keys = redact_keys or {
        "api_key",
        "secret",
        "secret_key",
        "signature",
        "X-BX-APIKEY",
        "authorization",
        "auth",
        "token",
        "password",
        "phone_number",
        "TELEGRAM_API_HASH",
    }
    if payload is None:
        return None
    if isinstance(payload, list):
        return [redact_dict(x, redact_keys=keys) for x in payload]
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for k, v in payload.items():
            ks = str(k)
            if ks.lower() in keys or ks in keys:
                out[ks] = _redact_value(v)
            else:
                out[ks] = redact_dict(v, redact_keys=keys)
        return out
    return payload


@dataclass(frozen=True)
class TelemetryCorrelation:
    ssot_id: Optional[int] = None
    bot_order_id: Optional[str] = None
    bingx_order_id: Optional[str] = None
    position_id: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_id: Optional[int] = None


class TelemetryLogger:
    def __init__(
        self,
        *,
        jsonl_path: Path,
        bot_name: str = "trading_bot",
        env: str = "prod",
    ):
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.bot_name = str(bot_name)
        self.env = str(env)
        self._lock = threading.Lock()

    def emit(
        self,
        *,
        event_type: str,
        level: str = "INFO",
        subsystem: str,
        message: str,
        correlation: Optional[object] = None,
        payload: Optional[dict] = None,
        event_key: Optional[str] = None,
    ) -> None:
        """
        Append a single JSONL event. Never raises (best-effort).
        """
        try:
            corr_obj: Optional[TelemetryCorrelation] = None
            if correlation is None:
                corr_obj = TelemetryCorrelation()
            elif isinstance(correlation, TelemetryCorrelation):
                corr_obj = correlation
            elif isinstance(correlation, dict):
                # Allow callers to pass a dict for convenience.
                corr_obj = TelemetryCorrelation(
                    ssot_id=correlation.get("ssot_id"),
                    bot_order_id=correlation.get("bot_order_id"),
                    bingx_order_id=correlation.get("bingx_order_id"),
                    position_id=correlation.get("position_id"),
                    telegram_chat_id=correlation.get("telegram_chat_id"),
                    telegram_message_id=correlation.get("telegram_message_id"),
                )
            else:
                corr_obj = TelemetryCorrelation()

            corr = corr_obj

            # Deterministic event_key helps downstream de-dup.
            key = event_key
            if not key:
                key_material = json.dumps(
                    {
                        "event_type": event_type,
                        "subsystem": subsystem,
                        "ssot_id": corr.ssot_id,
                        "bot_order_id": corr.bot_order_id,
                        "bingx_order_id": corr.bingx_order_id,
                        "position_id": corr.position_id,
                        "telegram_chat_id": corr.telegram_chat_id,
                        "telegram_message_id": corr.telegram_message_id,
                        "message": message,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                key = _stable_hash(key_material)

            evt = {
                "ts_utc": _utc_now_iso(),
                "event_type": str(event_type),
                "level": str(level).upper(),
                "subsystem": str(subsystem),
                "message": str(message),
                "event_key": key,
                "bot": self.bot_name,
                "env": self.env,
                "correlation": {
                    "ssot_id": corr.ssot_id,
                    "bot_order_id": corr.bot_order_id,
                    "bingx_order_id": corr.bingx_order_id,
                    "position_id": corr.position_id,
                    "telegram_chat_id": corr.telegram_chat_id,
                    "telegram_message_id": corr.telegram_message_id,
                },
                "payload": redact_dict(payload) if payload is not None else None,
            }

            line = json.dumps(evt, separators=(",", ":"), ensure_ascii=False)
            with self._lock:
                with self.jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            # Telemetry must never take the bot down.
            return


