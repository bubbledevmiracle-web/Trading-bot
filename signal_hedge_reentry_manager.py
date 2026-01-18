#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Hedge & Re-entry Manager (Stage 5 - BingX, deterministic)
===============================================================

Project naming conventions:
- "signal_*.py" modules own the execution/management logic for a stage.
- Lifecycle state lives in SQLite (LifecycleStore) as snake_case columns.

Stage 5 flow (exact):
- Monitor adverse move of -2% against SIGNAL entry (immutable, not avg entry).
- When triggered (once per position), open a 100% hedge in opposite direction.
  - Hedge TP = signal SL
  - Hedge SL = signal entry
- After hedge closes (TP or SL), attempt Stage 2 Dual-Limit re-entry in original direction.
  - Max 3 attempts
  - Hard stop via (symbol, side) lock until a new external Telegram signal arrives.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict

import config
from bingx_client import BingXClient
from lifecycle_store import LifecycleStore
from signal_dual_limit_entry import DualLimitEntryExecutor
from stage6_telemetry import TelemetryLogger, TelemetryCorrelation
from stage6_telegram import send_telegram_with_telemetry

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(x: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if not s:
            return default
        return Decimal(s)
    except Exception:
        return default


def _opp_side(side_norm: str) -> str:
    s = (side_norm or "").upper()
    if s == "LONG":
        return "SHORT"
    return "LONG"


def _close_side_for_position(position_side_norm: str) -> str:
    # To close LONG -> SELL. To close SHORT -> BUY.
    s = (position_side_norm or "").upper()
    return "SELL" if s == "LONG" else "BUY"


def _get_signal_entry_price(pos: dict) -> Decimal:
    # New preferred naming
    v = _d(pos.get("signal_entry_price"), Decimal("0"))
    if v > 0:
        return v
    # Back-compat (older column name)
    return _d(pos.get("orig_entry_price"), Decimal("0"))


def _get_signal_sl_price(pos: dict) -> Decimal:
    v = _d(pos.get("signal_sl_price"), Decimal("0"))
    if v > 0:
        return v
    return _d(pos.get("orig_sl_price"), Decimal("0"))


def _get_signal_leverage(pos: dict) -> Decimal:
    v = _d(pos.get("signal_leverage"), Decimal("0"))
    if v > 0:
        return v
    return _d(pos.get("orig_leverage"), Decimal("0"))


def _get_is_hedge_armed(pos: dict) -> int:
    v = pos.get("stage5_is_hedge_armed")
    if v is not None:
        try:
            return int(v)
        except Exception:
            return 0
    return int(pos.get("stage5_hedge_armed") or 0)


def _get_reentry_attempt_count(pos: dict) -> int:
    v = pos.get("stage5_reentry_attempt_count")
    if v is not None:
        try:
            return int(v)
        except Exception:
            return 0
    return int(pos.get("stage5_reentry_attempts") or 0)


@dataclass(frozen=True)
class _FakeQueuedSignal:
    id: int
    symbol: str
    side: str  # LONG/SHORT
    entry_price: str
    sl_price: str


class Stage5HedgeReentryManager:
    def __init__(
        self,
        *,
        store: LifecycleStore,
        bingx: BingXClient,
        stage2: DualLimitEntryExecutor,
        stage4_manager: object,
        telegram_client=None,
        telegram_chat_id: Optional[str] = None,
        telemetry: Optional[TelemetryLogger] = None,
        worker_id: str = "stage5-main",
    ):
        self.store = store
        self.bingx = bingx
        self.stage2 = stage2
        self.stage4_manager = stage4_manager  # Stage4LifecycleManager instance (used for TP/SL placement)
        self.telegram_client = telegram_client
        self.telegram_chat_id = telegram_chat_id or getattr(config, "PERSONAL_CHANNEL_ID", None)
        self.telemetry = telemetry
        self.worker_id = worker_id

        self._reentry_tasks: Dict[int, asyncio.Task] = {}

    async def run_forever(self) -> None:
        poll_s = max(int(getattr(config, "STAGE5_POLL_INTERVAL_SECONDS", 3)), 1)
        while True:
            try:
                await self._tick_once()
            except Exception as e:
                logger.error("Stage 5 loop error: %s", e, exc_info=True)
            await asyncio.sleep(poll_s)

    async def _tick_once(self) -> None:
        max_attempts = int(getattr(config, "STAGE5_MAX_REENTRY_ATTEMPTS", 3))
        adverse_pct = Decimal(str(getattr(config, "STAGE5_ADVERSE_MOVE_PCT", Decimal("0.02"))))

        # 1) Reset counters after successful TP closes (Position qty exhausted)
        closed = await asyncio.to_thread(self.store.list_positions_by_status, statuses=["CLOSED"], limit=200)
        for pos in closed:
            attempts = _get_reentry_attempt_count(pos)
            if attempts <= 0:
                continue
            reason = (pos.get("closed_reason") or "")
            if "Position qty exhausted" in reason:
                await asyncio.to_thread(
                    self.store.update_position,
                    ssot_id=int(pos["ssot_id"]),
                    stage5_reentry_attempts=0,
                    stage5_reentry_attempt_count=0,
                )
                await asyncio.to_thread(self.store.clear_stage5_lock, symbol=pos["symbol"], side=pos["side"])

        # 2) Monitor OPEN positions for adverse move, and HEDGE_MODE positions for hedge outcomes
        active = await asyncio.to_thread(self.store.list_positions_by_status, statuses=["OPEN", "HEDGE_MODE"], limit=500)
        for pos in active:
            status = (pos.get("status") or "").upper()
            side_norm = (pos.get("side") or "").upper()
            symbol = pos.get("symbol")
            if not symbol or side_norm not in {"LONG", "SHORT"}:
                continue

            signal_entry = _get_signal_entry_price(pos)
            signal_sl = _get_signal_sl_price(pos)
            if signal_entry <= 0 or signal_sl <= 0:
                continue

            if status == "OPEN":
                if _get_is_hedge_armed(pos) != 1:
                    continue

                ltp = await asyncio.to_thread(self.bingx.get_current_price, symbol)
                if ltp <= 0:
                    continue

                if side_norm == "LONG":
                    trigger = ltp <= (signal_entry * (Decimal("1.00") - adverse_pct))
                else:
                    trigger = ltp >= (signal_entry * (Decimal("1.00") + adverse_pct))

                if trigger:
                    await self._activate_hedge(pos)
                continue

            if status == "HEDGE_MODE":
                tp_oid = pos.get("stage5_hedge_tp_order_id")
                sl_oid = pos.get("stage5_hedge_sl_order_id")
                if not tp_oid and not sl_oid:
                    continue

                formatted = self.bingx._format_symbol(symbol)
                tp_filled = False
                sl_filled = False
                if tp_oid:
                    st = await asyncio.to_thread(self.bingx.get_order_status, formatted, str(tp_oid))
                    if st and (str(st.get("status") or "").upper() == "FILLED"):
                        tp_filled = True
                if sl_oid:
                    st = await asyncio.to_thread(self.bingx.get_order_status, formatted, str(sl_oid))
                    if st and (str(st.get("status") or "").upper() == "FILLED"):
                        sl_filled = True

                if tp_filled or sl_filled:
                    await self._handle_hedge_closed(pos, outcome=("TP" if tp_filled else "SL"), max_attempts=max_attempts)

    async def _activate_hedge(self, pos: dict) -> None:
        ssot_id = int(pos["ssot_id"])
        symbol = pos["symbol"]
        side_norm = (pos.get("side") or "").upper()

        signal_entry = _get_signal_entry_price(pos)
        signal_sl = _get_signal_sl_price(pos)
        if signal_entry <= 0 or signal_sl <= 0:
            return

        qty = _d(pos.get("planned_qty"), Decimal("0"))
        if qty <= 0:
            qty = _d(pos.get("remaining_qty"), Decimal("0"))
        if qty <= 0:
            return

        lev = _get_signal_leverage(pos)
        if lev > 0:
            try:
                await asyncio.to_thread(self.bingx.set_leverage, self.bingx._format_symbol(symbol), int(lev))
            except Exception:
                pass

        # Cancel original-side TP/SL orders (best-effort) so Stage 4 can't interfere.
        formatted = self.bingx._format_symbol(symbol)
        try:
            tp_levels = pos.get("tp_levels") or []
            for lvl in tp_levels:
                oid = lvl.get("order_id")
                if oid:
                    try:
                        await asyncio.to_thread(self.bingx.cancel_order, formatted, str(oid))
                    except Exception:
                        pass
            sl_oid = pos.get("sl_order_id")
            if sl_oid:
                try:
                    await asyncio.to_thread(self.bingx.cancel_order, formatted, str(sl_oid))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            await asyncio.to_thread(self.store.delete_tracked_orders_for_ssot_id, ssot_id=ssot_id)
        except Exception:
            pass

        hedge_side_norm = _opp_side(side_norm)
        hedge_open_side = "SELL" if hedge_side_norm == "SHORT" else "BUY"

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="HEDGE_ARMED_TRIGGERED",
                level="WARNING",
                subsystem="STAGE5",
                message="Adverse move triggered hedge activation",
                correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                payload={"symbol": symbol, "signal_side": side_norm, "hedge_side": hedge_side_norm, "qty": str(qty)},
            )

        await asyncio.to_thread(
            self.store.update_position,
            ssot_id=ssot_id,
            status="HEDGE_MODE",
            stage5_hedge_armed=0,          # back-compat
            stage5_is_hedge_armed=0,       # preferred
            stage5_hedge_status="OPEN",    # back-compat
            stage5_hedge_state="OPEN",     # preferred
        )
        await asyncio.to_thread(self.store.clear_position_fields, ssot_id=ssot_id, fields=["sl_order_id"])

        entry_resp = await asyncio.to_thread(
            self.bingx.place_market_order,
            symbol=symbol,
            side=hedge_open_side,
            quantity=qty,
            reduce_only=False,
            position_side=hedge_side_norm,
        )
        hedge_entry_oid = entry_resp.get("orderId")

        hedge_close_side = _close_side_for_position(hedge_side_norm)

        tp_resp = await asyncio.to_thread(
            self.bingx.place_limit_order,
            symbol=formatted,
            side=hedge_close_side,
            price=signal_sl,  # TP = signal SL
            quantity=qty,
            leverage=Decimal("1"),
            post_only=False,
            time_in_force="GTC",
            reduce_only=True,
            position_side=hedge_side_norm,
        )
        hedge_tp_oid = tp_resp.get("orderId")

        sl_resp = await asyncio.to_thread(
            self.bingx.place_stop_market_order,
            symbol=symbol,
            side=hedge_close_side,
            stop_price=signal_entry,  # SL = signal entry
            quantity=qty,
            reduce_only=True,
            position_side=hedge_side_norm,
        )
        hedge_sl_oid = sl_resp.get("orderId")

        await asyncio.to_thread(
            self.store.update_position,
            ssot_id=ssot_id,
            stage5_hedge_entry_order_id=str(hedge_entry_oid) if hedge_entry_oid else None,
            stage5_hedge_tp_order_id=str(hedge_tp_oid) if hedge_tp_oid else None,
            stage5_hedge_sl_order_id=str(hedge_sl_oid) if hedge_sl_oid else None,
        )

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="HEDGE_OPENED",
                level="INFO",
                subsystem="STAGE5",
                message="Hedge opened",
                correlation=TelemetryCorrelation(
                    ssot_id=ssot_id,
                    bot_order_id=f"ssot-{ssot_id}",
                    bingx_order_id=str(hedge_entry_oid) if hedge_entry_oid else None,
                ),
                payload={
                    "symbol": symbol,
                    "signal_side": side_norm,
                    "hedge_side": hedge_side_norm,
                    "qty": str(qty),
                    "hedge_tp_order_id": str(hedge_tp_oid) if hedge_tp_oid else None,
                    "hedge_sl_order_id": str(hedge_sl_oid) if hedge_sl_oid else None,
                },
            )

        await self._send_telegram(
            "🧊 Stage5: Hedge opened\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"signal_side={side_norm}\n"
            f"hedge_side={hedge_side_norm}\n"
            f"qty={qty}\n"
            f"hedge_TP(signal_SL)={signal_sl}\n"
            f"hedge_SL(signal_entry)={signal_entry}\n"
            f"time={_utc_now_iso()}"
            ,
            ssot_id=ssot_id,
        )

    async def _handle_hedge_closed(self, pos: dict, *, outcome: str, max_attempts: int) -> None:
        ssot_id = int(pos["ssot_id"])
        symbol = pos["symbol"]
        side_norm = (pos.get("side") or "").upper()

        attempts = _get_reentry_attempt_count(pos) + 1

        qty_close = _d(pos.get("remaining_qty"), Decimal("0"))
        if qty_close <= 0:
            qty_close = _d(pos.get("planned_qty"), Decimal("0"))

        close_side = _close_side_for_position(side_norm)
        if qty_close > 0:
            await asyncio.to_thread(
                self.bingx.place_market_order,
                symbol=symbol,
                side=close_side,
                quantity=qty_close,
                reduce_only=True,
                position_side=side_norm,
            )

        await asyncio.to_thread(
            self.store.update_position,
            ssot_id=ssot_id,
            status="CLOSED",
            remaining_qty="0",
            closed_reason=f"Stage5: Hedge {outcome} -> forced exit",
            closed_at_utc=_utc_now_iso(),
            stage5_hedge_status=f"CLOSED_{outcome}",  # back-compat
            stage5_hedge_state=f"CLOSED_{outcome}",   # preferred
            stage5_reentry_attempts=attempts,         # back-compat
            stage5_reentry_attempt_count=attempts,    # preferred
        )

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="HEDGE_CLOSED",
                level="INFO",
                subsystem="STAGE5",
                message="Hedge closed -> forced exit",
                correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                payload={"symbol": symbol, "signal_side": side_norm, "outcome": str(outcome), "attempts": int(attempts)},
            )

        if attempts >= max_attempts:
            await asyncio.to_thread(
                self.store.set_stage5_lock,
                symbol=symbol,
                side=side_norm,
                ssot_id=ssot_id,
                reason=f"Stage5: max re-entry attempts reached ({max_attempts})",
            )
            if self.telemetry is not None:
                self.telemetry.emit(
                    event_type="REENTRY_LOCKED",
                    level="WARNING",
                    subsystem="STAGE5",
                    message="Max re-entry attempts reached; locked until new external signal",
                    correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                    payload={"symbol": symbol, "side": side_norm, "max_attempts": int(max_attempts)},
                )
            return

        existing = self._reentry_tasks.get(ssot_id)
        if existing and not existing.done():
            return
        self._reentry_tasks[ssot_id] = asyncio.create_task(self._run_reentry_attempts(ssot_id=ssot_id, max_attempts=max_attempts))

    async def _run_reentry_attempts(self, *, ssot_id: int, max_attempts: int) -> None:
        while True:
            pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
            if not pos:
                return

            symbol = pos["symbol"]
            side_norm = (pos.get("side") or "").upper()
            lock = await asyncio.to_thread(self.store.get_stage5_lock, symbol=symbol, side=side_norm)
            if lock and int(lock.get("locked") or 0) == 1:
                return

            attempts = _get_reentry_attempt_count(pos)
            if attempts >= max_attempts:
                await asyncio.to_thread(
                    self.store.set_stage5_lock,
                    symbol=symbol,
                    side=side_norm,
                    ssot_id=ssot_id,
                    reason=f"Stage5: max re-entry attempts reached ({max_attempts})",
                )
                return

            signal_entry = _get_signal_entry_price(pos)
            signal_sl = _get_signal_sl_price(pos)
            if signal_entry <= 0 or signal_sl <= 0:
                return

            fake = _FakeQueuedSignal(
                id=ssot_id,
                symbol=symbol,
                side=side_norm,
                entry_price=str(signal_entry),
                sl_price=str(signal_sl),
            )
            if self.telemetry is not None:
                self.telemetry.emit(
                    event_type="REENTRY_ATTEMPT",
                    level="INFO",
                    subsystem="STAGE5",
                    message="Re-entry attempt via Stage2",
                    correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                    payload={"symbol": symbol, "side": side_norm, "attempt": int(attempts) + 1, "max_attempts": int(max_attempts)},
                )
            result = await self.stage2.execute_one(fake)
            if self.telemetry is not None:
                self.telemetry.emit(
                    event_type="REENTRY_COMPLETED",
                    level="INFO" if (result.status == "COMPLETED") else "WARNING",
                    subsystem="STAGE5",
                    message="Re-entry result",
                    correlation=TelemetryCorrelation(ssot_id=ssot_id, bot_order_id=f"ssot-{ssot_id}"),
                    payload={"symbol": symbol, "side": side_norm, "status": result.status},
                )

            if result.status != "COMPLETED":
                attempts += 1
                await asyncio.to_thread(
                    self.store.update_position,
                    ssot_id=ssot_id,
                    stage5_reentry_attempts=attempts,
                    stage5_reentry_attempt_count=attempts,
                )
                if attempts >= max_attempts:
                    await asyncio.to_thread(
                        self.store.set_stage5_lock,
                        symbol=symbol,
                        side=side_norm,
                        ssot_id=ssot_id,
                        reason=f"Stage5: max re-entry attempts reached ({max_attempts})",
                    )
                    return
                await asyncio.sleep(2)
                continue

            stage2 = result.details or {}
            Q = str(stage2.get("Q")) if stage2.get("Q") is not None else None
            f = _d(((stage2.get("fills") or {}).get("f")), Decimal("0"))
            N = _d(((stage2.get("fills") or {}).get("N")), Decimal("0"))
            avg_entry = str((N / f)) if f > 0 and N > 0 else None

            tp_levels = pos.get("tp_levels") or []
            for lvl in tp_levels:
                lvl["status"] = "OPEN"
                lvl["filled_qty"] = "0"
                lvl["order_id"] = None

            await asyncio.to_thread(
                self.store.update_position,
                ssot_id=ssot_id,
                status="OPEN",
                planned_qty=Q,
                remaining_qty=Q,
                avg_entry=avg_entry,
                sl_price=str(signal_sl),
                tp_levels=tp_levels,
                stage5_hedge_armed=1,          # back-compat
                stage5_is_hedge_armed=1,       # preferred
            )
            await asyncio.to_thread(
                self.store.clear_position_fields,
                ssot_id=ssot_id,
                fields=[
                    "sl_order_id",
                    "stage5_hedge_status",
                    "stage5_hedge_entry_order_id",
                    "stage5_hedge_tp_order_id",
                    "stage5_hedge_sl_order_id",
                    "closed_reason",
                    "closed_at_utc",
                ],
            )

            try:
                await getattr(self.stage4_manager, "_place_initial_tp_sl")(ssot_id=ssot_id)
            except Exception as e:
                logger.error("Stage5: failed to place TP/SL after re-entry (ssot_id=%s): %s", ssot_id, e, exc_info=True)
            return

    async def _send_telegram(self, text: str, *, ssot_id: Optional[int] = None) -> None:
        if not self.telegram_client or not self.telegram_chat_id:
            return
        try:
            corr = None
            if ssot_id is not None:
                corr = TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}")
            await send_telegram_with_telemetry(
                telegram_client=self.telegram_client,
                chat_id=self.telegram_chat_id,
                text=text,
                telemetry=self.telemetry,
                correlation=corr,
            )
        except Exception as e:
            logger.error("Stage 5 Telegram send failed: %s", e)


