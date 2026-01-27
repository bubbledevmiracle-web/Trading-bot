#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 2 - Dual-Limit Entry Executor
===================================
Implements the exact Stage 2 flow:

1) Dequeue/claim next signal from SSoT queue (idempotent claim)
2) Compute dual-limit prices: P1 = Em - Δ, P2 = Em + Δ (tick-quantized)
   - Enforce maker-safety relative to LTP: BUY both < LTP, SELL both > LTP (tick shifts)
3) Compute quantities: q1 = Q/2, q2 = Q-q1 (qty-step-quantized)
4) Place 2 GTC post-only limit orders
5) Poll for fills (order status): accumulate f and Σ(price*qty) using executedQty and avgPrice
6) Merge on first fill:
   pr = (Em*Q - Σ(fill_price*fill_qty)) / (Q-f)  (tick-quantized)
   cancel remaining original order(s) and place replacement post-only GTC at pr for remaining

Author: Trading Bot Project
Date: 2026-01-15
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, List

import config
from bingx_client import BingXClient, _safe_decimal
from ssot_store import SignalStore, QueuedSignal

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Stage2Result:
    ssot_id: int
    status: str  # COMPLETED | FAILED | EXPIRED
    details: dict


class DualLimitEntryExecutor:
    def __init__(
        self,
        *,
        store: SignalStore,
        bingx: BingXClient,
        worker_id: str,
    ):
        self.store = store
        self.bingx = bingx
        self.worker_id = worker_id

    async def run_forever(self) -> None:
        """
        Background loop: claim signals and execute Stage 2.
        """
        poll_s = max(int(getattr(config, "STAGE2_POLL_INTERVAL_SECONDS", 3)), 1)
        while True:
            sig = await asyncio.to_thread(self.store.claim_next_signal, worker_id=self.worker_id)
            if sig is None:
                await asyncio.sleep(poll_s)
                continue

            try:
                await asyncio.to_thread(
                    self.store.update_queue_row,
                    ssot_id=sig.id,
                    status="STAGE2_RUNNING",
                    stage2={"stage": "START", "ts": _utc_now_iso()},
                    last_error=None,
                )
                result = await self.execute_one(sig)
                await asyncio.to_thread(
                    self.store.update_queue_row,
                    ssot_id=sig.id,
                    status=result.status,
                    stage2=result.details,
                    last_error=None if result.status == "COMPLETED" else json.dumps(result.details, ensure_ascii=False),
                )
            except Exception as e:
                logger.error("Stage 2 fatal error (ssot_id=%s): %s", sig.id, e, exc_info=True)
                await asyncio.to_thread(
                    self.store.update_queue_row,
                    ssot_id=sig.id,
                    status="FAILED",
                    stage2={"stage": "FAILED", "ts": _utc_now_iso()},
                    last_error=str(e),
                )

    async def execute_one(self, sig: QueuedSignal) -> Stage2Result:
        """
        Execute Stage 2 for a single claimed signal.
        """
        if not getattr(config, "ENABLE_TRADING", True) or getattr(config, "DRY_RUN", False):
            return Stage2Result(
                ssot_id=sig.id,
                status="FAILED",
                details={"stage": "ABORT", "reason": "Trading disabled or DRY_RUN", "ts": _utc_now_iso()},
            )

        # Stage 2.1 inputs
        symbol = sig.symbol
        side_norm = (sig.side or "").upper()  # LONG/SHORT (from Stage 1)
        if side_norm not in {"LONG", "SHORT"}:
            return Stage2Result(
                ssot_id=sig.id,
                status="FAILED",
                details={"stage": "VALIDATION", "reason": f"Invalid side: {sig.side}", "ts": _utc_now_iso()},
            )

        side = "BUY" if side_norm == "LONG" else "SELL"
        Em = Decimal(sig.entry_price)
        SL = Decimal(sig.sl_price)

        symbol_info = await asyncio.to_thread(self.bingx.get_symbol_info, symbol)
        if not symbol_info:
            return Stage2Result(
                ssot_id=sig.id,
                status="FAILED",
                details={"stage": "SYMBOL_INFO", "reason": f"Symbol not found: {symbol}", "ts": _utc_now_iso()},
            )

        lot = symbol_info.get("lotSizeFilter", {}) or {}

        # Guard against malformed exchange metadata (e.g., None/"" -> Decimal ConversionSyntax)
        tick_size = _safe_decimal(symbol_info.get("tickSize"), Decimal("0"))
        qty_step = _safe_decimal(lot.get("qtyStep"), Decimal("0"))
        min_qty = _safe_decimal(lot.get("minQty"), Decimal("0.001"))
        if min_qty <= 0:
            logger.warning(
                "Invalid minQty from exchange metadata (symbol=%s, minQty=%r). Falling back to 0.001",
                symbol,
                lot.get("minQty"),
            )
            min_qty = Decimal("0.001")

        # Stage 1 sizing -> Stage 2 quantities (Q)
        pos = await asyncio.to_thread(self.bingx.calculate_position_size, Em, SL)
        leverage = Decimal(str(pos.get("leverage", "1")))
        Q_raw = Decimal(str(pos.get("quantity", "0")))
        Q = self.bingx._quantize_quantity(Q_raw, qty_step, min_qty)

        # Stage 2 spread Δ: deterministic default for single-price entry
        spread_pct = Decimal(str(getattr(config, "STAGE2_DEFAULT_SPREAD_PCT", Decimal("0.001"))))
        Delta = (Em * spread_pct)
        if tick_size > 0:
            Delta = self.bingx._quantize_price(Delta, tick_size)

        # Stage 2 prices (with maker-safety)
        p1, p2 = self.bingx.calculate_dual_limit_prices(Em, Delta, tick_size)
        ltp = await asyncio.to_thread(self.bingx.get_current_price, symbol)
        p1, p2 = self.bingx.ensure_maker_safe_prices(
            side=side,
            p1=p1,
            p2=p2,
            ltp=ltp,
            tick_size=tick_size,
            max_shifts=int(getattr(config, "STAGE2_MAX_PRICE_SHIFTS", 50)),
        )

        # Stage 2 quantities split
        q1 = self.bingx._quantize_quantity(Q / 2, qty_step, min_qty)
        q2 = self.bingx._quantize_quantity(Q - q1, qty_step, min_qty)

        stage2_state: dict = {
            "stage": "PLACEMENT",
            "ts": _utc_now_iso(),
            "symbol": symbol,
            "side": side,
            "Em": str(Em),
            "Delta": str(Delta),
            "Q": str(Q),
            "q1": str(q1),
            "q2": str(q2),
            "p1": str(p1),
            "p2": str(p2),
            "leverage": str(leverage),
            "orders": {"original": [], "replacement": None},
            "merge": {"done": False, "pr": None},
        }
        await asyncio.to_thread(self.store.update_queue_row, ssot_id=sig.id, status="STAGE2_PLANNED", stage2=stage2_state)

        # Place the 2 post-only GTC orders
        dual = await asyncio.to_thread(
            self.bingx.place_dual_limit_orders,
            symbol=symbol,
            side=side,
            target_entry=Em,
            spread=Delta,
            total_quantity=Q,
            leverage=leverage,
            symbol_info=symbol_info,
        )
        order_ids: List[str] = list(dual.get("order_ids") or [])
        if len(order_ids) != 2:
            return Stage2Result(
                ssot_id=sig.id,
                status="FAILED",
                details={**stage2_state, "stage": "PLACEMENT_FAILED", "ts": _utc_now_iso(), "dual": dual},
            )

        stage2_state["orders"]["original"] = order_ids
        await asyncio.to_thread(self.store.update_queue_row, ssot_id=sig.id, status="WAITING_FOR_FILLS", stage2=stage2_state)

        first_fill_deadline = asyncio.get_event_loop().time() + float(
            getattr(config, "STAGE2_FIRST_FILL_TIMEOUT_SECONDS", 24 * 3600)
        )
        total_fill_deadline = asyncio.get_event_loop().time() + float(
            getattr(config, "STAGE2_TOTAL_FILL_TIMEOUT_SECONDS", 6 * 24 * 3600)
        )

        poll_s = max(int(getattr(config, "STAGE2_POLL_INTERVAL_SECONDS", 3)), 1)

        merged = False
        replacement_id: Optional[str] = None

        # Track all involved order ids for notional accounting
        def all_order_ids() -> List[str]:
            ids = list(order_ids)
            if replacement_id:
                ids.append(replacement_id)
            return ids

        while True:
            now = asyncio.get_event_loop().time()
            if not merged and now > first_fill_deadline:
                # No fills within the first-fill timeout -> expire (leave cleanup policies to global jobs)
                return Stage2Result(
                    ssot_id=sig.id,
                    status="EXPIRED",
                    details={**stage2_state, "stage": "EXPIRED_NO_FILL", "ts": _utc_now_iso()},
                )
            if now > total_fill_deadline:
                return Stage2Result(
                    ssot_id=sig.id,
                    status="EXPIRED",
                    details={**stage2_state, "stage": "EXPIRED_TOTAL_TIMEOUT", "ts": _utc_now_iso()},
                )

            # Poll status for each order and compute f and N from scratch (deterministic reconciliation)
            f = Decimal("0")
            N = Decimal("0")
            statuses: Dict[str, dict] = {}
            for oid in all_order_ids():
                st = await asyncio.to_thread(self.bingx.get_order_status, self.bingx._format_symbol(symbol), oid)
                if st is None:
                    continue
                statuses[oid] = st
                executed = Decimal(str(st.get("executedQty", "0")))
                avg_price = Decimal(str(st.get("avgPrice", "0")))
                if executed > 0 and avg_price > 0:
                    f += executed
                    N += executed * avg_price

            stage2_state["fills"] = {"f": str(f), "N": str(N), "ts": _utc_now_iso()}
            await asyncio.to_thread(self.store.update_queue_row, ssot_id=sig.id, status="WAITING_FOR_FILLS", stage2=stage2_state)

            if f <= 0:
                await asyncio.sleep(poll_s)
                continue

            # Completion check (full filled)
            if f >= Q:
                stage2_state["stage"] = "COMPLETED"
                stage2_state["ts"] = _utc_now_iso()
                return Stage2Result(ssot_id=sig.id, status="COMPLETED", details=stage2_state)

            if not merged:
                # Stage 2.6 merge on first fill
                remaining = Q - f
                if remaining <= 0:
                    stage2_state["stage"] = "COMPLETED"
                    stage2_state["ts"] = _utc_now_iso()
                    return Stage2Result(ssot_id=sig.id, status="COMPLETED", details=stage2_state)

                # Cancel all original orders that are still open/partial
                for oid in order_ids:
                    st = statuses.get(oid) or {}
                    st_status = (st.get("status") or "").upper()
                    if st_status in {"NEW", "PARTIALLY_FILLED"}:
                        await asyncio.to_thread(self.bingx.cancel_order, self.bingx._format_symbol(symbol), oid)

                # Reconcile after cancels (race-safe)
                f = Decimal("0")
                N = Decimal("0")
                for oid in order_ids:
                    st = await asyncio.to_thread(self.bingx.get_order_status, self.bingx._format_symbol(symbol), oid)
                    if st is None:
                        continue
                    executed = Decimal(str(st.get("executedQty", "0")))
                    avg_price = Decimal(str(st.get("avgPrice", "0")))
                    if executed > 0 and avg_price > 0:
                        f += executed
                        N += executed * avg_price

                remaining = Q - f
                if remaining > 0:
                    pr = (Em * Q - N) / remaining
                    if tick_size > 0:
                        pr = self.bingx._quantize_price(pr, tick_size)

                    # Maker-safety for replacement price
                    ltp2 = await asyncio.to_thread(self.bingx.get_current_price, symbol)
                    pr_safe, _ = self.bingx.ensure_maker_safe_prices(
                        side=side,
                        p1=pr,
                        p2=pr,
                        ltp=ltp2,
                        tick_size=tick_size,
                        max_shifts=int(getattr(config, "STAGE2_MAX_PRICE_SHIFTS", 50)),
                    )

                    replacement = await asyncio.to_thread(
                        self.bingx.place_limit_order,
                        symbol=self.bingx._format_symbol(symbol),
                        side=side,
                        price=pr_safe,
                        quantity=remaining,
                        post_only=True,
                        time_in_force="GTC",
                        reduce_only=False,
                    )
                    replacement_id = replacement.get("orderId")
                    if not replacement_id:
                        return Stage2Result(
                            ssot_id=sig.id,
                            status="FAILED",
                            details={
                                **stage2_state,
                                "stage": "REPLACEMENT_FAILED",
                                "ts": _utc_now_iso(),
                                "replacement": replacement,
                                "remaining": str(remaining),
                                "pr": str(pr),
                                "pr_safe": str(pr_safe),
                            },
                        )

                    stage2_state["orders"]["replacement"] = replacement_id
                    stage2_state["merge"]["done"] = True
                    stage2_state["merge"]["pr"] = str(pr_safe)
                    stage2_state["stage"] = "MERGED"
                    stage2_state["ts"] = _utc_now_iso()
                    await asyncio.to_thread(self.store.update_queue_row, ssot_id=sig.id, status="MERGED", stage2=stage2_state)

                merged = True

            await asyncio.sleep(poll_s)


