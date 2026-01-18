#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 6 - Watchdog (Errors & Capacity)
======================================
Continuously evaluates system health and capacity limits.

Outputs:
- Safety block state (reject new signals temporarily)
- Structured telemetry events

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import config
from lifecycle_store import LifecycleStore
from ssot_store import SignalStore
from stage6_telemetry import TelemetryLogger, TelemetryCorrelation


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_error(*, subsystem: str, raw_message: str) -> str:
    """
    Deterministic, best-effort error classification.
    """
    s = (raw_message or "").lower()
    sub = (subsystem or "").upper()

    if "timeout" in s or "timed out" in s:
        return "API_TIMEOUT"
    if "connection" in s or "connect" in s or "dns" in s:
        return "API_CONNECTIVITY"
    if "insufficient" in s or "margin" in s or "balance" in s:
        return "INSUFFICIENT_FUNDS"
    if "parse" in s or "validation" in s:
        return "PARSING_VALIDATION"
    if sub in {"TELEGRAM"} and ("flood" in s or "floodwait" in s):
        return "TELEGRAM_RATE_LIMIT"
    return "UNKNOWN"


@dataclass
class Stage6WatchdogState:
    capacity_blocked: bool = False
    capacity_reason: Optional[str] = None
    active_trades: int = 0
    max_active_trades: int = 100
    last_tick_utc: Optional[str] = None


class Stage6CapacityGuard:
    """
    Lightweight read-only guard used by Stage 1 before accepting signals.
    """

    def __init__(self, state: Stage6WatchdogState):
        self._state = state

    def can_accept_signal(self) -> Tuple[bool, Dict[str, Any]]:
        st = self._state
        return (not st.capacity_blocked), {
            "capacity_blocked": st.capacity_blocked,
            "capacity_reason": st.capacity_reason,
            "active_trades": st.active_trades,
            "max_active_trades": st.max_active_trades,
            "last_tick_utc": st.last_tick_utc,
        }


class Stage6Watchdog:
    def __init__(
        self,
        *,
        telemetry: TelemetryLogger,
        ssot_store: Optional[SignalStore],
        lifecycle_store: Optional[LifecycleStore],
        state: Stage6WatchdogState,
        worker_id: str = "stage6-watchdog",
    ):
        self.telemetry = telemetry
        self.ssot_store = ssot_store
        self.lifecycle_store = lifecycle_store
        self.state = state
        self.worker_id = worker_id

    async def run_forever(self) -> None:
        poll_s = max(int(getattr(config, "STAGE6_WATCHDOG_POLL_INTERVAL_SECONDS", 10)), 1)
        self.state.max_active_trades = int(getattr(config, "STAGE6_MAX_ACTIVE_TRADES", 100))

        while True:
            try:
                await self._tick_once()
            except Exception as e:
                self.telemetry.emit(
                    event_type="STAGE6_WATCHDOG_ERROR",
                    level="ERROR",
                    subsystem="WATCHDOG",
                    message=str(e),
                    payload={"ts": _utc_now_iso()},
                )
            await asyncio.sleep(poll_s)

    async def _tick_once(self) -> None:
        max_active = int(getattr(config, "STAGE6_MAX_ACTIVE_TRADES", 100))
        active_trades = 0
        stage4_active = 0
        stage2_active = 0

        # Prefer Stage 4 store if present (represents actual opened positions lifecycle).
        if self.lifecycle_store is not None:
            stage4_active = await asyncio.to_thread(self.lifecycle_store.count_positions_not_closed)

        # Also count Stage 2 in-flight rows (can represent live entry orders / reserved capacity).
        if self.ssot_store is not None:
            try:
                stage2_active = await asyncio.to_thread(self.ssot_store.count_stage2_inflight)
            except Exception:
                stage2_active = 0

        # Conservative union (over-count is safer than under-count)
        active_trades = int(stage4_active) + int(stage2_active)

        self.state.active_trades = int(active_trades)
        self.state.max_active_trades = int(max_active)
        self.state.last_tick_utc = _utc_now_iso()

        blocked = active_trades >= max_active
        self.state.capacity_blocked = bool(blocked)
        self.state.capacity_reason = (
            f"Max active trades exceeded ({active_trades}/{max_active})" if blocked else None
        )

        self.telemetry.emit(
            event_type="WATCHDOG_CAPACITY",
            level="WARNING" if blocked else "INFO",
            subsystem="WATCHDOG",
            message="Capacity evaluation",
            payload={
                "active_trades": int(active_trades),
                "stage4_active": int(stage4_active),
                "stage2_active": int(stage2_active),
                "max_active_trades": int(max_active),
                "blocked": bool(blocked),
            },
        )


