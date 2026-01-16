#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Lifecycle Manager (Stage 4 - TP/SL & Lifecycle Management, BingX)
=========================================================================
REST-polling implementation with idempotent processing based on executedQty deltas.

Principles:
- BingX-first: only exchange-confirmed order state changes drive transitions.
- Telegram is reporting only, and only after state is committed.
- WS hooks can be added later; this module is structured around "events" from polling.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import config
from bingx_client import BingXClient
from lifecycle_store import LifecycleStore, Stage2CompletedRow

logger = logging.getLogger(__name__)


def _now_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


@dataclass(frozen=True)
class Stage4Event:
    ssot_id: int
    kind: str  # TP_FILL | SL_FILL | SL_MOVED_BE | POSITION_CLOSED | INFO
    message: str


class Stage4LifecycleManager:
    def __init__(
        self,
        *,
        store: LifecycleStore,
        bingx: BingXClient,
        telegram_client=None,
        telegram_chat_id: Optional[str] = None,
        worker_id: str = "stage4-main",
    ):
        self.store = store
        self.bingx = bingx
        self.telegram_client = telegram_client
        self.telegram_chat_id = telegram_chat_id or getattr(config, "PERSONAL_CHANNEL_ID", None)
        self.worker_id = worker_id

    async def run_forever(self) -> None:
        poll_s = max(int(getattr(config, "STAGE4_POLL_INTERVAL_SECONDS", 3)), 1)
        init_batch = max(int(getattr(config, "STAGE4_INIT_BATCH_LIMIT", 10)), 1)

        while True:
            try:
                # 1) Initialize new Stage2 COMPLETED rows into Stage4 positions
                await self._initialize_new_positions(limit=init_batch)

                # 2) Poll tracked orders and apply lifecycle rules
                await self._poll_tracked_orders_once()
            except Exception as e:
                logger.error("Stage 4 loop error: %s", e, exc_info=True)

            await asyncio.sleep(poll_s)

    # ------------------------------------------------------------------
    # Initialization from Stage 2 results
    # ------------------------------------------------------------------
    async def _initialize_new_positions(self, *, limit: int) -> None:
        rows = await asyncio.to_thread(self.store.list_new_stage2_completed, limit=limit)
        if not rows:
            return

        for row in rows:
            try:
                await self._initialize_one(row)
            except Exception as e:
                logger.error("Stage 4 init failed (ssot_id=%s): %s", row.ssot_id, e, exc_info=True)

    async def _initialize_one(self, row: Stage2CompletedRow) -> None:
        # Parse Stage 2 JSON
        stage2 = {}
        if row.stage2_json:
            try:
                stage2 = json.loads(row.stage2_json)
            except Exception:
                stage2 = {}

        symbol = row.symbol
        side_norm = (row.side or "").upper()  # LONG/SHORT
        if side_norm not in {"LONG", "SHORT"}:
            return

        planned_qty = str(stage2.get("Q")) if stage2.get("Q") is not None else None
        f = _d(((stage2.get("fills") or {}).get("f")), Decimal("0"))
        N = _d(((stage2.get("fills") or {}).get("N")), Decimal("0"))
        avg_entry = None
        if f > 0 and N > 0:
            avg_entry = str((N / f))

        tp_prices = list(row.tp_prices or [])
        tp_levels: List[Dict] = []
        for i, tp in enumerate(tp_prices):
            tp_levels.append(
                {
                    "index": i,
                    "price": str(tp),
                    "status": "OPEN",  # OPEN | PARTIAL | COMPLETED
                    "filled_qty": "0",
                    "order_id": None,
                }
            )

        inserted = await asyncio.to_thread(
            self.store.create_position_if_absent,
            ssot_id=row.ssot_id,
            symbol=symbol,
            side=side_norm,
            status="OPEN",
            planned_qty=planned_qty,
            remaining_qty=planned_qty,
            avg_entry=avg_entry,
            sl_price=str(row.sl_price) if row.sl_price is not None else None,
            tp_levels=tp_levels,
        )
        if not inserted:
            return

        # Register Stage 2 entry orders so we can reconcile/observe if needed (informational for now)
        orders = (stage2.get("orders") or {})
        original = list((orders.get("original") or []))
        replacement = orders.get("replacement")
        for oid in original:
            await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=row.ssot_id, order_id=str(oid), kind="ENTRY", level_index=None)
        if replacement:
            await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=row.ssot_id, order_id=str(replacement), kind="ENTRY", level_index=None)

        # Place initial TP ladder + SL
        await self._place_initial_tp_sl(ssot_id=row.ssot_id)

    async def _place_initial_tp_sl(self, *, ssot_id: int) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return

        symbol = pos["symbol"]
        side_norm = (pos["side"] or "").upper()
        tp_levels = pos.get("tp_levels") or []

        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        if remaining <= 0:
            return

        # 1) Place TP reduce-only limit orders (equal split)
        if tp_levels:
            split_mode = str(getattr(config, "STAGE4_TP_SPLIT_MODE", "EQUAL")).upper()
            n = len(tp_levels)
            per = remaining / Decimal(str(n)) if n > 0 else remaining
            q_allocs: List[Decimal] = []
            if split_mode == "EQUAL":
                for i in range(n):
                    q_allocs.append(per if i < n - 1 else (remaining - sum(q_allocs)))
            else:
                for i in range(n):
                    q_allocs.append(per if i < n - 1 else (remaining - sum(q_allocs)))

            # Direction: LONG exits with SELL, SHORT exits with BUY
            tp_side = "SELL" if side_norm == "LONG" else "BUY"
            formatted_symbol = self.bingx._format_symbol(symbol)

            for lvl, q in zip(tp_levels, q_allocs):
                if q <= 0:
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
                    leverage=Decimal("1"),  # leverage already set at position level; BingX API ignores here
                    post_only=False,
                    time_in_force="GTC",
                    reduce_only=True,
                )
                oid = resp.get("orderId")
                if oid:
                    lvl["order_id"] = str(oid)
                    await asyncio.to_thread(
                        self.store.upsert_order_tracker,
                        ssot_id=ssot_id,
                        order_id=str(oid),
                        kind="TP",
                        level_index=int(lvl.get("index", 0)),
                    )

            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, tp_levels=tp_levels)

        # 2) Place SL reduce-only STOP_MARKET (best-effort)
        sl_price = _d(pos.get("sl_price"), Decimal("0"))
        if sl_price > 0 and not pos.get("sl_order_id"):
            sl_side = "SELL" if side_norm == "LONG" else "BUY"
            resp = await asyncio.to_thread(
                self.bingx.place_stop_market_order,
                symbol=symbol,
                side=sl_side,
                stop_price=sl_price,
                quantity=remaining,
                reduce_only=True,
            )
            oid = resp.get("orderId")
            if oid:
                await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, sl_order_id=str(oid))
                await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)
            else:
                # Can't confirm protection -> alert and mark state
                await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
                await self._send_telegram(
                    f"⚠️ Stage4: SL placement failed (needs manual protection)\n"
                    f"ssot_id={ssot_id}\n"
                    f"symbol={symbol}\n"
                    f"side={side_norm}\n"
                    f"sl={sl_price}\n"
                    f"error={resp.get('error') or resp.get('raw')}"
                )

    # ------------------------------------------------------------------
    # Polling + lifecycle rules
    # ------------------------------------------------------------------
    async def _poll_tracked_orders_once(self) -> None:
        tracked = await asyncio.to_thread(self.store.list_tracked_orders, limit=500)
        if not tracked:
            return

        # Group by ssot_id to minimize reads
        by_ssot: Dict[int, List[dict]] = {}
        for t in tracked:
            by_ssot.setdefault(int(t["ssot_id"]), []).append(t)

        for ssot_id, orders in by_ssot.items():
            pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
            if not pos:
                continue
            if (pos.get("status") or "").upper() in {"CLOSED"}:
                continue

            symbol = pos["symbol"]
            formatted_symbol = self.bingx._format_symbol(symbol)

            for ot in orders:
                oid = ot["order_id"]
                kind = (ot.get("kind") or "").upper()
                last_exec = _d(ot.get("last_executed_qty"), Decimal("0"))

                st = await asyncio.to_thread(self.bingx.get_order_status, formatted_symbol, oid)
                if not st:
                    continue

                executed = _d(st.get("executedQty"), Decimal("0"))
                status = (st.get("status") or "").upper() if st.get("status") is not None else None

                if executed < last_exec:
                    # weird -> just update tracker and continue (reconcile later)
                    await asyncio.to_thread(self.store.update_order_tracker, order_id=oid, last_executed_qty=str(executed), last_status=status)
                    continue

                delta = executed - last_exec
                if delta > 0:
                    await self._apply_fill(ssot_id=ssot_id, kind=kind, order_id=oid, level_index=ot.get("level_index"), fill_qty=delta)

                await asyncio.to_thread(self.store.update_order_tracker, order_id=oid, last_executed_qty=str(executed), last_status=status)

                # Terminal: SL filled => closed
                if kind == "SL" and status == "FILLED":
                    await self._close_position(ssot_id=ssot_id, reason="SL filled")
                    break

            # If remaining qty is zero => closed
            pos2 = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
            if pos2:
                if _d(pos2.get("remaining_qty"), Decimal("0")) <= 0:
                    await self._close_position(ssot_id=ssot_id, reason="Position qty exhausted")

    async def _apply_fill(
        self,
        *,
        ssot_id: int,
        kind: str,
        order_id: str,
        level_index: Optional[int],
        fill_qty: Decimal,
    ) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return

        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        new_remaining = remaining - fill_qty
        if new_remaining < 0:
            new_remaining = Decimal("0")

        tp_levels = pos.get("tp_levels") or []

        if kind == "TP" and level_index is not None and 0 <= int(level_index) < len(tp_levels):
            lvl = tp_levels[int(level_index)]
            filled_prev = _d(lvl.get("filled_qty"), Decimal("0"))
            lvl["filled_qty"] = str(filled_prev + fill_qty)
            # status heuristic: if order fully filled we will see it in status, but for now treat any fill as PARTIAL until remaining goes 0
            lvl["status"] = "PARTIAL"
            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, remaining_qty=str(new_remaining), tp_levels=tp_levels)

            await self._send_telegram(
                f"✅ TP fill confirmed (BingX)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={pos['symbol']}\n"
                f"order_id={order_id}\n"
                f"tp_index={int(level_index)+1}\n"
                f"fill_qty={fill_qty}\n"
                f"remaining_qty={new_remaining}\n"
                f"time={_now_local_str()}"
            )

            # Move SL to BE after first TP fill (confirmed fill event)
            if int(level_index) == 0 and getattr(config, "STAGE4_MOVE_SL_TO_BE_AFTER_TP1", True):
                await self._move_sl_to_be(ssot_id=ssot_id)
            return

        if kind == "SL":
            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, remaining_qty=str(new_remaining))
            await self._send_telegram(
                f"🛑 SL fill confirmed (BingX)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={pos['symbol']}\n"
                f"order_id={order_id}\n"
                f"fill_qty={fill_qty}\n"
                f"remaining_qty={new_remaining}\n"
                f"time={_now_local_str()}"
            )
            return

        # Entry fills are informational in Stage 4 (Stage 2 already completed)
        await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, remaining_qty=str(new_remaining))

    async def _move_sl_to_be(self, *, ssot_id: int) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return
        if (pos.get("status") or "").upper() in {"NEEDS_MANUAL_PROTECTION", "CLOSED"}:
            return

        avg_entry = _d(pos.get("avg_entry"), Decimal("0"))
        if avg_entry <= 0:
            return

        symbol = pos["symbol"]
        side_norm = (pos.get("side") or "").upper()
        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        if remaining <= 0:
            return

        # Cancel old SL if exists
        old_sl_oid = pos.get("sl_order_id")
        if old_sl_oid:
            try:
                await asyncio.to_thread(self.bingx.cancel_order, self.bingx._format_symbol(symbol), str(old_sl_oid))
            except Exception:
                pass

        sl_side = "SELL" if side_norm == "LONG" else "BUY"
        resp = await asyncio.to_thread(
            self.bingx.place_stop_market_order,
            symbol=symbol,
            side=sl_side,
            stop_price=avg_entry,
            quantity=remaining,
            reduce_only=True,
        )
        oid = resp.get("orderId")
        if not oid:
            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
            await self._send_telegram(
                f"⚠️ Stage4: Failed to move SL to BE (needs manual protection)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={symbol}\n"
                f"be={avg_entry}\n"
                f"error={resp.get('error') or resp.get('raw')}"
            )
            return

        await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, sl_order_id=str(oid), sl_price=str(avg_entry))
        await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)

        await self._send_telegram(
            f"✅ SL moved to Break-Even (BingX confirmed)\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"new_sl={avg_entry}\n"
            f"time={_now_local_str()}"
        )

    async def _close_position(self, *, ssot_id: int, reason: str) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return
        if (pos.get("status") or "").upper() == "CLOSED":
            return

        # Cancel remaining TP orders (best-effort)
        symbol = pos["symbol"]
        formatted_symbol = self.bingx._format_symbol(symbol)
        tp_levels = pos.get("tp_levels") or []
        for lvl in tp_levels:
            oid = lvl.get("order_id")
            if oid:
                try:
                    await asyncio.to_thread(self.bingx.cancel_order, formatted_symbol, str(oid))
                except Exception:
                    pass
            lvl["status"] = "COMPLETED" if _d(lvl.get("filled_qty"), Decimal("0")) > 0 else lvl.get("status", "OPEN")

        await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="CLOSED", remaining_qty="0", tp_levels=tp_levels)

        await self._send_telegram(
            f"🏁 Position CLOSED (BingX confirmed)\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"reason={reason}\n"
            f"time={_now_local_str()}"
        )

    async def _send_telegram(self, text: str) -> None:
        if not self.telegram_client or not self.telegram_chat_id:
            return
        try:
            await self.telegram_client.send_message(chat_id=self.telegram_chat_id, text=text)
        except Exception as e:
            logger.error("Stage 4 Telegram send failed: %s", e)


