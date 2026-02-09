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
from stage6_telemetry import TelemetryLogger, TelemetryCorrelation
from stage6_telegram import send_telegram_with_telemetry

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


def _normalize_symbol_ws(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).upper().strip()
    s = s.replace("#", "").replace("/", "").replace("-", "")
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s


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
        telemetry: Optional[TelemetryLogger] = None,
        worker_id: str = "stage4-main",
    ):
        self.store = store
        self.bingx = bingx
        self.telegram_client = telegram_client
        _pid = telegram_chat_id or getattr(config, "PERSONAL_CHANNEL_ID", None)
        self.telegram_chat_id = int(_pid) if _pid is not None else None
        self.telemetry = telemetry
        self.worker_id = worker_id

        self._ws_queue: asyncio.Queue = asyncio.Queue()
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_last_event_ts: float = 0.0
        self._last_reconcile_ts: float = 0.0
        self._ws_seq_by_topic: Dict[str, int] = {}
        self._last_trade_id_by_symbol: Dict[str, str] = {}

    async def run_forever(self) -> None:
        poll_s = max(int(getattr(config, "STAGE4_POLL_INTERVAL_SECONDS", 3)), 1)
        init_batch = max(int(getattr(config, "STAGE4_INIT_BATCH_LIMIT", 10)), 1)
        ws_enabled = bool(getattr(config, "STAGE4_WS_ENABLE", True))
        ws_stale_s = max(int(getattr(config, "STAGE4_WS_STALE_SECONDS", 20)), 5)
        rest_fallback_s = max(int(getattr(config, "STAGE4_REST_FALLBACK_INTERVAL_SECONDS", 10)), 3)

        if ws_enabled:
            await self._start_ws_listener()
            if getattr(config, "STAGE4_RECONCILE_ON_START", True):
                await self._rest_reconcile_once()

        while True:
            try:
                # 1) Initialize new Stage2 COMPLETED rows into Stage4 positions
                await self._initialize_new_positions(limit=init_batch)

                # 2) Drain WS events (WS-first)
                if ws_enabled:
                    if not self._ws_task or self._ws_task.done():
                        await self._start_ws_listener()
                    await self._drain_ws_events()

                # 3) REST fallback + reconciliation
                now = asyncio.get_running_loop().time()
                ws_stale = (now - self._ws_last_event_ts) > ws_stale_s
                needs_reconcile = (now - self._last_reconcile_ts) > rest_fallback_s
                if not ws_enabled or ws_stale or needs_reconcile:
                    await self._rest_reconcile_once()
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
        orig_leverage = str(stage2.get("leverage")) if stage2.get("leverage") is not None else None
        f = _d(((stage2.get("fills") or {}).get("f")), Decimal("0"))
        N = _d(((stage2.get("fills") or {}).get("N")), Decimal("0"))
        avg_entry = None
        if f > 0 and N > 0:
            avg_entry = str((N / f))

        # CRITICAL: Use actual filled quantity from Stage 2, not planned quantity
        # Stage 2 stores actual fills in stage2_json.fills.f
        actual_qty = str(f) if f > 0 else planned_qty

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
            signal_type=row.signal_type,
            planned_qty=planned_qty,
            remaining_qty=actual_qty,
            position_qty=actual_qty,
            avg_entry=avg_entry,
            realized_pnl="0",
            unrealized_pnl="0",
            sl_price=str(row.sl_price) if row.sl_price is not None else None,
            tp_active_order_ids=[],
            signal_entry_price=str(row.entry_price) if row.entry_price is not None else None,
            signal_sl_price=str(row.sl_price) if row.sl_price is not None else None,
            signal_leverage=orig_leverage,
            # Back-compat writes
            orig_entry_price=str(row.entry_price) if row.entry_price is not None else None,
            orig_sl_price=str(row.sl_price) if row.sl_price is not None else None,
            orig_leverage=orig_leverage,
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
            tp_active_oids: List[str] = []

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
                    post_only=False,
                    time_in_force="GTC",
                    reduce_only=True,
                    position_side=side_norm,
                )
                oid = resp.get("orderId")
                if oid:
                    lvl["order_id"] = str(oid)
                    tp_active_oids.append(str(oid))
                    await asyncio.to_thread(
                        self.store.upsert_order_tracker,
                        ssot_id=ssot_id,
                        order_id=str(oid),
                        kind="TP",
                        level_index=int(lvl.get("index", 0)),
                    )

            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, tp_levels=tp_levels, tp_active_order_ids=tp_active_oids)

        sl_price = _d(pos.get("sl_price"), Decimal("0"))
        if sl_price > 0 and not pos.get("sl_order_id"):
            current_price = await asyncio.to_thread(self.bingx.get_current_price, symbol)
            sl_valid = (
                (side_norm == "LONG" and sl_price < current_price)
                or (side_norm == "SHORT" and sl_price > current_price)
            )
            if not sl_valid and current_price > 0:
                await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
                if (pos.get("status") or "").upper() != "NEEDS_MANUAL_PROTECTION":
                    reason = "SL above current (LONG) - set manual SL below current" if side_norm == "LONG" else "SL below current (SHORT) - set manual SL above current"
                    await self._send_telegram(
                        f"⚠️ Stage4: SL not placed (needs manual protection)\n"
                        f"ssot_id={ssot_id}\n"
                        f"symbol={symbol}\n"
                        f"side={side_norm}\n"
                        f"sl={sl_price}\n"
                        f"current={current_price}\n"
                        f"reason={reason}",
                        ssot_id=ssot_id,
                    )
            else:
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
                    await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, sl_order_id=str(oid))
                    await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)
                else:
                    await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
                    if (pos.get("status") or "").upper() != "NEEDS_MANUAL_PROTECTION":
                        await self._send_telegram(
                            f"⚠️ Stage4: SL placement failed (needs manual protection)\n"
                            f"ssot_id={ssot_id}\n"
                            f"symbol={symbol}\n"
                            f"side={side_norm}\n"
                            f"sl={sl_price}\n"
                            f"error={resp.get('error') or resp.get('raw')}",
                            ssot_id=ssot_id,
                        )

    # ------------------------------------------------------------------
    # WebSocket-first monitoring
    # ------------------------------------------------------------------
    async def _start_ws_listener(self) -> None:
        if self._ws_task and not self._ws_task.done():
            return

        topics = list(getattr(config, "BINGX_WS_TOPICS", []) or [])

        async def _on_msg(msg: Dict) -> None:
            await self._ws_queue.put(msg)
            self._ws_last_event_ts = asyncio.get_running_loop().time()

        async def _on_disconnect(exc: Exception) -> None:
            logger.error("Stage4 WS disconnected: %s", exc)

        async def _runner() -> None:
            await self.bingx.ws_listen(topics=topics, on_message=_on_msg, on_disconnect=_on_disconnect)

        self._ws_task = asyncio.create_task(_runner())

    async def _drain_ws_events(self) -> None:
        drained = 0
        while not self._ws_queue.empty():
            msg = await self._ws_queue.get()
            try:
                await self._handle_ws_message(msg)
            except Exception as e:
                logger.error("Stage4 WS event error: %s", e, exc_info=True)
            drained += 1
            if drained >= 500:
                break

    async def _handle_ws_message(self, msg: Dict) -> None:
        if not isinstance(msg, dict):
            return

        topic = str(msg.get("topic") or msg.get("channel") or msg.get("stream") or "").lower()
        data = msg.get("data") if "data" in msg else msg.get("result") if "result" in msg else msg

        seq = None
        if isinstance(data, dict) and data.get("seq") is not None:
            seq = data.get("seq")
        elif msg.get("seq") is not None:
            seq = msg.get("seq")
        if seq is not None and topic:
            try:
                seq_i = int(seq)
                last = self._ws_seq_by_topic.get(topic)
                if last is not None and seq_i > last + 1:
                    logger.warning("Stage4 WS sequence gap detected: topic=%s last=%s now=%s", topic, last, seq_i)
                    self._ws_last_event_ts = 0.0
                self._ws_seq_by_topic[topic] = seq_i
            except Exception:
                pass

        if isinstance(data, list):
            for item in data:
                await self._handle_ws_message({"topic": topic, "data": item})
            return

        if not isinstance(data, dict):
            return

        if "position" in topic or data.get("positionSide") or data.get("positionAmt") or data.get("positionQty"):
            await self._apply_position_update(data)
            return

        if "order" in topic or "execution" in topic or data.get("orderId") or data.get("execId"):
            await self._apply_order_event(data)
            return

        # Wallet/balance updates are informational; no state mutation yet.

    async def _apply_order_event(self, data: Dict) -> None:
        order_id = data.get("orderId") or data.get("orderID") or data.get("id")
        if not order_id:
            return

        exec_id = data.get("execId") or data.get("tradeId") or data.get("fillId")
        if exec_id:
            is_new = await asyncio.to_thread(self.store.record_execution_if_new, order_id=str(order_id), exec_id=str(exec_id))
            if not is_new:
                return

        status = (data.get("status") or data.get("orderStatus") or "").upper()
        executed_total = _d(data.get("executedQty") or data.get("cumQty") or data.get("filledQty"), Decimal("0"))
        last_fill_qty = _d(data.get("lastFillQty") or data.get("fillQty") or data.get("qty"), Decimal("0"))
        avg_price = _d(data.get("avgPrice") or data.get("fillPrice") or data.get("price"), Decimal("0"))

        tracker = await asyncio.to_thread(self.store.get_order_tracker, order_id=str(order_id))
        if not tracker:
            # Try to infer by matching TP/SL order ids
            pos = await self._find_position_by_order_id(order_id=str(order_id))
            if pos:
                kind, level_index = self._infer_order_kind_from_position(pos, str(order_id))
                if kind:
                    await asyncio.to_thread(
                        self.store.upsert_order_tracker,
                        ssot_id=int(pos["ssot_id"]),
                        order_id=str(order_id),
                        kind=kind,
                        level_index=level_index,
                    )
                    tracker = await asyncio.to_thread(self.store.get_order_tracker, order_id=str(order_id))

        if not tracker:
            return

        last_exec = _d(tracker.get("last_executed_qty"), Decimal("0"))
        fill_qty = last_fill_qty
        if fill_qty <= 0 and executed_total > last_exec:
            fill_qty = executed_total - last_exec

        if fill_qty > 0:
            await self._apply_fill(
                ssot_id=int(tracker["ssot_id"]),
                kind=str(tracker.get("kind") or ""),
                order_id=str(order_id),
                level_index=tracker.get("level_index"),
                fill_qty=fill_qty,
                fill_avg_price=avg_price if avg_price > 0 else None,
                status=status,
            )

        if status or executed_total > 0:
            new_exec = executed_total if executed_total > 0 else last_exec
            await asyncio.to_thread(self.store.update_order_tracker, order_id=str(order_id), last_executed_qty=str(new_exec), last_status=status)

        if (tracker.get("kind") or "").upper() == "SL" and status == "FILLED":
            await self._close_position(ssot_id=int(tracker["ssot_id"]), reason="SL filled")

    async def _apply_position_update(self, data: Dict) -> None:
        symbol = _normalize_symbol_ws(data.get("symbol") or data.get("s"))
        side_norm = (data.get("positionSide") or data.get("side") or "").upper()
        if not symbol or side_norm not in {"LONG", "SHORT"}:
            return

        pos = await asyncio.to_thread(self.store.get_position_by_symbol_side, symbol=symbol, side=side_norm)
        if not pos:
            return

        position_qty = _d(data.get("positionAmt") or data.get("positionQty") or data.get("qty"), Decimal("0"))
        avg_entry = _d(data.get("avgPrice") or data.get("entryPrice") or data.get("avgEntryPrice"), Decimal("0"))
        realized = _d(data.get("realizedProfit") or data.get("realizedPnl") or data.get("realizedPNL"), Decimal("0"))
        unrealized = _d(data.get("unrealizedProfit") or data.get("unrealizedPnl") or data.get("unrealizedPNL"), Decimal("0"))

        await asyncio.to_thread(
            self.store.update_position,
            ssot_id=int(pos["ssot_id"]),
            position_qty=str(position_qty),
            remaining_qty=str(position_qty),
            avg_entry=str(avg_entry) if avg_entry > 0 else None,
            realized_pnl=str(realized),
            unrealized_pnl=str(unrealized),
            last_reconcile_at_utc=datetime.utcnow().replace(tzinfo=None).isoformat() + "Z",
        )

        if position_qty <= 0:
            await self._close_position(ssot_id=int(pos["ssot_id"]), reason="Position qty zero (BingX)")

    async def _find_position_by_order_id(self, *, order_id: str) -> Optional[Dict]:
        positions = await asyncio.to_thread(self.store.list_positions_by_status, statuses=["OPEN", "HEDGE_MODE"], limit=500)
        for pos in positions:
            if pos.get("sl_order_id") == str(order_id):
                return pos
            for lvl in pos.get("tp_levels") or []:
                if str(lvl.get("order_id")) == str(order_id):
                    return pos
        return None

    def _infer_order_kind_from_position(self, pos: Dict, order_id: str) -> Tuple[Optional[str], Optional[int]]:
        if pos.get("sl_order_id") == str(order_id):
            return "SL", None
        for lvl in pos.get("tp_levels") or []:
            if str(lvl.get("order_id")) == str(order_id):
                return "TP", int(lvl.get("index") or 0)
        return None, None

    async def _rest_reconcile_once(self) -> None:
        await self._reconcile_trades_from_rest()
        await self._poll_tracked_orders_once()
        await self._reconcile_positions_from_rest()
        self._last_reconcile_ts = asyncio.get_running_loop().time()

    async def _reconcile_trades_from_rest(self) -> None:
        positions = await asyncio.to_thread(self.store.list_positions_by_status, statuses=["OPEN", "HEDGE_MODE"], limit=500)
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            trades = await asyncio.to_thread(self.bingx.get_my_trades, symbol, 200, None)
            if not trades:
                continue

            # Process newest first but keep stable order by time if available
            trades_sorted = sorted(
                trades,
                key=lambda t: int(t.get("time") or t.get("timestamp") or 0),
            )

            last_seen_id = self._last_trade_id_by_symbol.get(str(symbol))
            last_seen_int = None
            if last_seen_id is not None:
                try:
                    last_seen_int = int(str(last_seen_id))
                except Exception:
                    last_seen_int = None
            for t in trades_sorted:
                trade_id = t.get("tradeId") or t.get("execId") or t.get("id")
                if trade_id is None:
                    continue
                trade_id_str = str(trade_id)
                trade_id_int = None
                try:
                    trade_id_int = int(trade_id_str)
                except Exception:
                    trade_id_int = None
                order_id = t.get("orderId") or t.get("orderID")
                if not order_id:
                    continue

                tracker = await asyncio.to_thread(self.store.get_order_tracker, order_id=str(order_id))
                if not tracker:
                    continue

                qty = _d(t.get("qty") or t.get("execQty") or t.get("quantity"), Decimal("0"))
                price = _d(t.get("price") or t.get("execPrice"), Decimal("0"))
                status = str(t.get("status") or t.get("tradeType") or "FILLED").upper()

                if last_seen_int is not None and trade_id_int is not None:
                    if trade_id_int <= last_seen_int:
                        continue

                is_new = await asyncio.to_thread(
                    self.store.record_execution_if_new,
                    order_id=str(order_id),
                    exec_id=trade_id_str,
                )
                if not is_new:
                    continue

                if qty > 0:
                    await self._apply_fill(
                        ssot_id=int(tracker["ssot_id"]),
                        kind=str(tracker.get("kind") or ""),
                        order_id=str(order_id),
                        level_index=tracker.get("level_index"),
                        fill_qty=qty,
                        fill_avg_price=price if price > 0 else None,
                        status=status,
                    )

            # update last seen trade id
            if trades_sorted:
                last_trade = trades_sorted[-1]
                last_id = last_trade.get("tradeId") or last_trade.get("execId") or last_trade.get("id")
                if last_id is not None:
                    self._last_trade_id_by_symbol[str(symbol)] = str(last_id)

    async def _reconcile_positions_from_rest(self) -> None:
        positions = await asyncio.to_thread(self.store.list_positions_by_status, statuses=["OPEN", "HEDGE_MODE"], limit=500)
        for pos in positions:
            symbol = pos.get("symbol")
            side_norm = (pos.get("side") or "").upper()
            if not symbol or side_norm not in {"LONG", "SHORT"}:
                continue
            open_orders = await asyncio.to_thread(self.bingx.get_open_orders, symbol)
            open_order_ids = {str(o.get("orderId")) for o in (open_orders or []) if o.get("orderId")}
            exchange_positions = await asyncio.to_thread(self.bingx.get_positions, symbol)
            if not exchange_positions:
                continue
            match = None
            for p in exchange_positions:
                p_side = (p.get("positionSide") or "").upper()
                if p_side == side_norm:
                    match = p
                    break
            if not match:
                continue

            position_qty = _d(match.get("positionAmt") or match.get("positionQty") or match.get("qty"), Decimal("0"))
            avg_entry = _d(match.get("avgPrice") or match.get("entryPrice") or match.get("avgEntryPrice"), Decimal("0"))
            realized = _d(match.get("realizedProfit") or match.get("realizedPnl") or match.get("realizedPNL"), Decimal("0"))
            unrealized = _d(match.get("unrealizedProfit") or match.get("unrealizedPnl") or match.get("unrealizedPNL"), Decimal("0"))

            await asyncio.to_thread(
                self.store.update_position,
                ssot_id=int(pos["ssot_id"]),
                position_qty=str(position_qty),
                remaining_qty=str(position_qty),
                avg_entry=str(avg_entry) if avg_entry > 0 else None,
                realized_pnl=str(realized),
                unrealized_pnl=str(unrealized),
                last_reconcile_at_utc=datetime.utcnow().replace(tzinfo=None).isoformat() + "Z",
            )

            # Detect missing SL/TP orders (REST only)
            tp_levels = pos.get("tp_levels") or []
            tp_changed = False
            for lvl in tp_levels:
                oid = str(lvl.get("order_id")) if lvl.get("order_id") else None
                if not oid:
                    continue
                if (lvl.get("status") or "").upper() == "COMPLETED":
                    continue
                if oid not in open_order_ids:
                    st = await asyncio.to_thread(self.bingx.get_order_status, self.bingx._format_symbol(symbol), oid)
                    st_status = (st.get("status") or st.get("orderStatus") or "").upper() if st else None
                    executed_qty = _d(st.get("executedQty") or st.get("cumQty") or st.get("filledQty"), Decimal("0")) if st else Decimal("0")
                    if st_status in {"FILLED", "CLOSED", "DONE"} or executed_qty > 0:
                        lvl["status"] = "COMPLETED"
                        tp_changed = True
                    else:
                        if (lvl.get("status") or "").upper() != "MISSING":
                            lvl["status"] = "MISSING"
                            tp_changed = True
                            await self._send_telegram(
                                f"⚠️ Stage4: TP order missing (REST)\n"
                                f"ssot_id={pos['ssot_id']}\n"
                                f"symbol={symbol}\n"
                                f"tp_index={int(lvl.get('index') or 0) + 1}\n"
                                f"order_id={oid}\n"
                                f"time={_now_local_str()}"
                                ,
                                ssot_id=int(pos["ssot_id"]),
                            )

            if tp_changed:
                await asyncio.to_thread(self.store.update_position, ssot_id=int(pos["ssot_id"]), tp_levels=tp_levels)

            sl_oid = pos.get("sl_order_id")
            if sl_oid and str(sl_oid) not in open_order_ids:
                st = await asyncio.to_thread(self.bingx.get_order_status, self.bingx._format_symbol(symbol), str(sl_oid))
                st_status = (st.get("status") or st.get("orderStatus") or "").upper() if st else None
                if st_status not in {"FILLED", "CLOSED", "DONE"}:
                    await asyncio.to_thread(self.store.update_position, ssot_id=int(pos["ssot_id"]), status="NEEDS_MANUAL_PROTECTION")
                    if (pos.get("status") or "").upper() != "NEEDS_MANUAL_PROTECTION":
                        await self._send_telegram(
                            f"⚠️ Stage4: SL order missing (REST)\n"
                            f"ssot_id={pos['ssot_id']}\n"
                            f"symbol={symbol}\n"
                            f"order_id={sl_oid}\n"
                            f"time={_now_local_str()}"
                            ,
                            ssot_id=int(pos["ssot_id"]),
                        )

            if position_qty <= 0:
                await self._close_position(ssot_id=int(pos["ssot_id"]), reason="Position qty zero (REST)")

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
            if (pos.get("status") or "").upper() in {"HEDGE_MODE"}:
                # Stage 5 owns the controlling logic in hedge mode.
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
                avg_price = _d(st.get("avgPrice"), Decimal("0"))
                status = (st.get("status") or "").upper() if st.get("status") is not None else None

                if executed < last_exec:
                    # weird -> just update tracker and continue (reconcile later)
                    await asyncio.to_thread(self.store.update_order_tracker, order_id=oid, last_executed_qty=str(executed), last_status=status)
                    continue

                delta = executed - last_exec
                if delta > 0:
                    await self._apply_fill(
                        ssot_id=ssot_id,
                        kind=kind,
                        order_id=oid,
                        level_index=ot.get("level_index"),
                        fill_qty=delta,
                        fill_avg_price=avg_price if avg_price > 0 else None,
                        status=status,
                    )

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
        fill_avg_price: Optional[Decimal],
        status: Optional[str] = None,
    ) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return

        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        new_remaining = remaining - fill_qty
        if new_remaining < 0:
            new_remaining = Decimal("0")

        tp_levels = pos.get("tp_levels") or []

        # ENTRY fills are informational here (Stage 2 already completed).
        # Do NOT mutate remaining_qty; remaining_qty tracks open position qty (reduce-only exits).
        if kind == "ENTRY":
            return

        if kind == "TP" and level_index is not None and 0 <= int(level_index) < len(tp_levels):
            lvl = tp_levels[int(level_index)]
            filled_prev = _d(lvl.get("filled_qty"), Decimal("0"))
            lvl["filled_qty"] = str(filled_prev + fill_qty)
            if (status or "").upper() == "FILLED":
                lvl["status"] = "COMPLETED"
            else:
                lvl["status"] = "PARTIAL"

            realized_pnl = _d(pos.get("realized_pnl"), Decimal("0"))
            if fill_avg_price is not None:
                side_norm = (pos.get("side") or "").upper()
                avg_entry = _d(pos.get("avg_entry"), Decimal("0"))
                if avg_entry > 0:
                    if side_norm == "LONG":
                        realized_pnl += (Decimal(fill_avg_price) - avg_entry) * fill_qty
                    else:
                        realized_pnl += (avg_entry - Decimal(fill_avg_price)) * fill_qty

            tp_active = [str(x) for x in (pos.get("tp_active_order_ids") or []) if x]
            if (status or "").upper() == "FILLED" and str(order_id) in tp_active:
                tp_active = [x for x in tp_active if x != str(order_id)]

            await asyncio.to_thread(
                self.store.update_position,
                ssot_id=ssot_id,
                remaining_qty=str(new_remaining),
                tp_levels=tp_levels,
                realized_pnl=str(realized_pnl),
                tp_active_order_ids=tp_active,
            )

            if self.telemetry is not None:
                pnl_usdt = None
                try:
                    side_norm = (pos.get("side") or "").upper()
                    avg_entry = _d(pos.get("avg_entry"), Decimal("0"))
                    if fill_avg_price is not None and avg_entry > 0:
                        if side_norm == "LONG":
                            pnl_usdt = (Decimal(fill_avg_price) - avg_entry) * fill_qty
                        else:
                            pnl_usdt = (avg_entry - Decimal(fill_avg_price)) * fill_qty
                except Exception:
                    pnl_usdt = None
                self.telemetry.emit(
                    event_type="TP_FILL",
                    level="INFO",
                    subsystem="STAGE4",
                    message="TP fill confirmed",
                    correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}", bingx_order_id=str(order_id)),
                    payload={
                        "symbol": pos.get("symbol"),
                        "tp_index": int(level_index) + 1,
                        "fill_qty": str(fill_qty),
                        "fill_avg_price": str(fill_avg_price) if fill_avg_price is not None else None,
                        "pnl_usdt": str(pnl_usdt) if pnl_usdt is not None else None,
                        "remaining_qty": str(new_remaining),
                    },
                )

            await self._send_telegram(
                f"✅ TP fill confirmed (BingX)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={pos['symbol']}\n"
                f"order_id={order_id}\n"
                f"tp_index={int(level_index)+1}\n"
                f"fill_qty={fill_qty}\n"
                f"remaining_qty={new_remaining}\n"
                f"time={_now_local_str()}"
                ,
                ssot_id=ssot_id,
            )

            # Move SL to BE after first TP fill (confirmed fill event)
            if int(level_index) == 0 and getattr(config, "STAGE4_MOVE_SL_TO_BE_AFTER_TP1", True):
                await self._move_sl_to_be(ssot_id=ssot_id)

            # Trailing activation after TP2+ (if enabled)
            if int(level_index) >= int(getattr(config, "STAGE4_TRAILING_AFTER_TP_INDEX", 1)):
                if getattr(config, "STAGE4_TRAILING_ENABLE", False):
                    await self._move_sl_trailing(ssot_id=ssot_id)
            return

        if kind == "SL":
            realized_pnl = _d(pos.get("realized_pnl"), Decimal("0"))
            if fill_avg_price is not None:
                side_norm = (pos.get("side") or "").upper()
                avg_entry = _d(pos.get("avg_entry"), Decimal("0"))
                if avg_entry > 0:
                    if side_norm == "LONG":
                        realized_pnl += (Decimal(fill_avg_price) - avg_entry) * fill_qty
                    else:
                        realized_pnl += (avg_entry - Decimal(fill_avg_price)) * fill_qty

            await asyncio.to_thread(
                self.store.update_position,
                ssot_id=ssot_id,
                remaining_qty=str(new_remaining),
                realized_pnl=str(realized_pnl),
            )
            if self.telemetry is not None:
                pnl_usdt = None
                try:
                    side_norm = (pos.get("side") or "").upper()
                    avg_entry = _d(pos.get("avg_entry"), Decimal("0"))
                    if fill_avg_price is not None and avg_entry > 0:
                        if side_norm == "LONG":
                            pnl_usdt = (Decimal(fill_avg_price) - avg_entry) * fill_qty
                        else:
                            pnl_usdt = (avg_entry - Decimal(fill_avg_price)) * fill_qty
                except Exception:
                    pnl_usdt = None
                self.telemetry.emit(
                    event_type="SL_FILL",
                    level="INFO",
                    subsystem="STAGE4",
                    message="SL fill confirmed",
                    correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}", bingx_order_id=str(order_id)),
                    payload={
                        "symbol": pos.get("symbol"),
                        "fill_qty": str(fill_qty),
                        "fill_avg_price": str(fill_avg_price) if fill_avg_price is not None else None,
                        "pnl_usdt": str(pnl_usdt) if pnl_usdt is not None else None,
                        "remaining_qty": str(new_remaining),
                    },
                )
            await self._send_telegram(
                f"🛑 SL fill confirmed (BingX)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={pos['symbol']}\n"
                f"order_id={order_id}\n"
                f"fill_qty={fill_qty}\n"
                f"remaining_qty={new_remaining}\n"
                f"time={_now_local_str()}"
                ,
                ssot_id=ssot_id,
            )
            return

        # Entry fills are informational in Stage 4 (Stage 2 already completed)
        # (handled above)

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
            position_side=side_norm,
        )
        oid = resp.get("orderId")
        if not oid:
            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
            if self.telemetry is not None:
                self.telemetry.emit(
                    event_type="SL_MOVE_FAILED",
                    level="ERROR",
                    subsystem="STAGE4",
                    message="Failed to move SL to BE",
                    correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}"),
                    payload={"symbol": symbol, "be": str(avg_entry), "resp": resp},
                )
            await self._send_telegram(
                f"⚠️ Stage4: Failed to move SL to BE (needs manual protection)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={symbol}\n"
                f"be={avg_entry}\n"
                f"error={resp.get('error') or resp.get('raw')}"
                ,
                ssot_id=ssot_id,
            )
            return

        await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, sl_order_id=str(oid), sl_price=str(avg_entry))
        await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="SL_MOVED_BE",
                level="INFO",
                subsystem="STAGE4",
                message="SL moved to Break-Even",
                correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}", bingx_order_id=str(oid)),
                payload={"symbol": symbol, "new_sl": str(avg_entry)},
            )

        await self._send_telegram(
            f"✅ SL moved to Break-Even (BingX confirmed)\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"new_sl={avg_entry}\n"
            f"time={_now_local_str()}"
            ,
            ssot_id=ssot_id,
        )

    async def _move_sl_trailing(self, *, ssot_id: int) -> None:
        pos = await asyncio.to_thread(self.store.get_position, ssot_id=ssot_id)
        if not pos:
            return
        if (pos.get("status") or "").upper() in {"NEEDS_MANUAL_PROTECTION", "CLOSED"}:
            return

        symbol = pos["symbol"]
        side_norm = (pos.get("side") or "").upper()
        if side_norm not in {"LONG", "SHORT"}:
            return

        remaining = _d(pos.get("remaining_qty"), Decimal("0"))
        if remaining <= 0:
            return

        offset = _d(getattr(config, "STAGE4_TRAILING_OFFSET_PCT", Decimal("0.003")), Decimal("0.003"))
        current = await asyncio.to_thread(self.bingx.get_current_price, symbol)
        if current <= 0:
            return

        if side_norm == "LONG":
            new_sl = current * (Decimal("1") - offset)
            sl_side = "SELL"
        else:
            new_sl = current * (Decimal("1") + offset)
            sl_side = "BUY"

        # Cancel old SL if exists
        old_sl_oid = pos.get("sl_order_id")
        if old_sl_oid:
            try:
                await asyncio.to_thread(self.bingx.cancel_order, self.bingx._format_symbol(symbol), str(old_sl_oid))
            except Exception:
                pass

        attempts = max(int(getattr(config, "STAGE4_SL_RETRY_ATTEMPTS", 3)), 1)
        delay_s = max(int(getattr(config, "STAGE4_SL_RETRY_DELAY_SECONDS", 2)), 1)
        last_resp = None
        oid = None
        for _ in range(attempts):
            resp = await asyncio.to_thread(
                self.bingx.place_stop_market_order,
                symbol=symbol,
                side=sl_side,
                stop_price=new_sl,
                quantity=remaining,
                reduce_only=True,
                position_side=side_norm,
            )
            last_resp = resp
            oid = resp.get("orderId")
            if oid:
                break
            await asyncio.sleep(delay_s)

        if not oid:
            await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, status="NEEDS_MANUAL_PROTECTION")
            if self.telemetry is not None:
                self.telemetry.emit(
                    event_type="SL_TRAILING_FAILED",
                    level="ERROR",
                    subsystem="STAGE4",
                    message="Failed to place trailing SL",
                    correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}"),
                    payload={"symbol": symbol, "trailing_sl": str(new_sl), "resp": last_resp},
                )
            await self._send_telegram(
                f"⚠️ Stage4: Failed to activate trailing SL (needs manual protection)\n"
                f"ssot_id={ssot_id}\n"
                f"symbol={symbol}\n"
                f"trailing_sl={new_sl}\n"
                f"error={(last_resp or {}).get('error') or (last_resp or {}).get('raw')}"
                ,
                ssot_id=ssot_id,
            )
            return

        await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, sl_order_id=str(oid), sl_price=str(new_sl))
        await asyncio.to_thread(self.store.upsert_order_tracker, ssot_id=ssot_id, order_id=str(oid), kind="SL", level_index=None)

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="SL_TRAILING_SET",
                level="INFO",
                subsystem="STAGE4",
                message="Trailing SL activated",
                correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}", bingx_order_id=str(oid)),
                payload={"symbol": symbol, "new_sl": str(new_sl)},
            )

        await self._send_telegram(
            f"✅ Trailing SL activated (BingX confirmed)\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"new_sl={new_sl}\n"
            f"time={_now_local_str()}"
            ,
            ssot_id=ssot_id,
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

        await asyncio.to_thread(
            self.store.update_position,
            ssot_id=ssot_id,
            status="CLOSED",
            remaining_qty="0",
            position_qty="0",
            tp_levels=tp_levels,
            tp_active_order_ids=[],
            closed_reason=str(reason),
            closed_at_utc=datetime.utcnow().replace(tzinfo=None).isoformat() + "Z",
        )

        if self.telemetry is not None:
            self.telemetry.emit(
                event_type="POSITION_CLOSED",
                level="INFO",
                subsystem="STAGE4",
                message="Position closed",
                correlation=TelemetryCorrelation(ssot_id=int(ssot_id), bot_order_id=f"ssot-{int(ssot_id)}"),
                payload={"symbol": symbol, "reason": str(reason)},
            )

        await self._send_telegram(
            f"🏁 Position CLOSED (BingX confirmed)\n"
            f"ssot_id={ssot_id}\n"
            f"symbol={symbol}\n"
            f"reason={reason}\n"
            f"time={_now_local_str()}"
            ,
            ssot_id=ssot_id,
        )

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
            logger.error("Stage 4 Telegram send failed: %s", e)


