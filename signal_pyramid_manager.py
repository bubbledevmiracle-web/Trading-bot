#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 4.5 - Pyramid/Scaling Manager
===================================
Adds to winning positions when profit thresholds are reached.

Strategy:
- When position profit > 3% → Add 50% more
- When position profit > 6% → Add another 25%
- Maximum total: 2x original position size
- Each addition uses same leverage
- Updates average entry price

Author: Trading Bot Project
Date: 2026-01-17
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import config
from bingx_client import BingXClient
from lifecycle_store import LifecycleStore

logger = logging.getLogger(__name__)


def _d(val, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if val is None:
            return default
        return Decimal(str(val))
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PyramidManager:
    """
    Manages position scaling/pyramiding for winning trades.
    """

    def __init__(
        self,
        *,
        bingx: BingXClient,
        lifecycle_store: LifecycleStore,
        worker_id: str = "pyramid-manager",
    ):
        self.bingx = bingx
        self.store = lifecycle_store
        self.worker_id = worker_id

        # Pyramid configuration
        self.enabled = getattr(config, "ENABLE_PYRAMID", True)
        self.threshold_1 = Decimal(str(getattr(config, "PYRAMID_PROFIT_THRESHOLD_1", 3.0)))  # 3%
        self.threshold_2 = Decimal(str(getattr(config, "PYRAMID_PROFIT_THRESHOLD_2", 6.0)))  # 6%
        self.add_size_1 = Decimal(str(getattr(config, "PYRAMID_ADD_SIZE_1", 0.5)))  # 50%
        self.add_size_2 = Decimal(str(getattr(config, "PYRAMID_ADD_SIZE_2", 0.25)))  # 25%
        self.max_multiplier = Decimal(str(getattr(config, "PYRAMID_MAX_SIZE_MULTIPLIER", 2.0)))  # 2x max
        self.poll_interval = max(int(getattr(config, "PYRAMID_POLL_INTERVAL_SECONDS", 30)), 5)

    async def run_forever(self) -> None:
        """
        Background loop: monitor positions and add to winners.
        """
        if not self.enabled:
            logger.info("Pyramid manager disabled in config")
            return

        logger.info("Pyramid manager started (poll=%ds)", self.poll_interval)
        while True:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error("Pyramid manager error: %s", e, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _check_all_positions(self) -> None:
        """
        Check all open positions for pyramid opportunities.
        """
        positions = await asyncio.to_thread(self.store.list_open_positions)
        for pos in positions:
            try:
                await self._check_one_position(pos)
            except Exception as e:
                logger.error("Pyramid check error (ssot_id=%s): %s", pos.get("ssot_id"), e)

    async def _check_one_position(self, pos: dict) -> None:
        """
        Check if position is profitable enough to pyramid.
        """
        ssot_id = int(pos["ssot_id"])
        symbol = pos["symbol"]
        side_norm = (pos["side"] or "").upper()

        # Skip if not OPEN or in hedge mode
        status = (pos.get("status") or "").upper()
        if status not in {"OPEN"}:
            return

        # Get current position from exchange
        formatted_symbol = self.bingx._format_symbol(symbol)
        exchange_positions = await asyncio.to_thread(self.bingx.get_positions, formatted_symbol)
        if not exchange_positions:
            return

        # Find matching position
        exchange_pos = None
        for p in exchange_positions:
            p_side = (p.get("positionSide") or "").upper()
            if p_side == side_norm:
                exchange_pos = p
                break

        if not exchange_pos:
            return

        # Calculate unrealized PnL %
        unrealized_pnl = _d(exchange_pos.get("unrealizedProfit") or exchange_pos.get("unRealizedProfit"), Decimal("0"))
        position_margin = _d(exchange_pos.get("positionInitialMargin") or exchange_pos.get("initialMargin"), Decimal("20"))
        if position_margin <= 0:
            return

        pnl_pct = (unrealized_pnl / position_margin) * Decimal("100")

        # Get pyramid state
        pyramid_state = pos.get("pyramid_state") or {}
        if not isinstance(pyramid_state, dict):
            pyramid_state = {}

        scale_1_done = pyramid_state.get("scale_1_done", False)
        scale_2_done = pyramid_state.get("scale_2_done", False)

        # Check thresholds
        original_qty = _d(pos.get("planned_qty"), Decimal("0"))
        if original_qty <= 0:
            return

        # Scale 1: +50% at 3% profit
        if not scale_1_done and pnl_pct >= self.threshold_1:
            add_qty = original_qty * self.add_size_1
            success = await self._add_to_position(
                ssot_id=ssot_id,
                symbol=formatted_symbol,
                side_norm=side_norm,
                add_qty=add_qty,
                original_qty=original_qty,
                scale_label="1",
            )
            if success:
                pyramid_state["scale_1_done"] = True
                pyramid_state["scale_1_time"] = _utc_now_iso()
                await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, pyramid_state=pyramid_state)
                logger.info("Pyramid scale 1 added (ssot_id=%s, symbol=%s, qty=%s)", ssot_id, symbol, add_qty)

        # Scale 2: +25% at 6% profit
        elif scale_1_done and not scale_2_done and pnl_pct >= self.threshold_2:
            add_qty = original_qty * self.add_size_2
            success = await self._add_to_position(
                ssot_id=ssot_id,
                symbol=formatted_symbol,
                side_norm=side_norm,
                add_qty=add_qty,
                original_qty=original_qty,
                scale_label="2",
            )
            if success:
                pyramid_state["scale_2_done"] = True
                pyramid_state["scale_2_time"] = _utc_now_iso()
                await asyncio.to_thread(self.store.update_position, ssot_id=ssot_id, pyramid_state=pyramid_state)
                logger.info("Pyramid scale 2 added (ssot_id=%s, symbol=%s, qty=%s)", ssot_id, symbol, add_qty)

    async def _add_to_position(
        self,
        *,
        ssot_id: int,
        symbol: str,
        side_norm: str,
        add_qty: Decimal,
        original_qty: Decimal,
        scale_label: str,
    ) -> bool:
        """
        Add to existing position with a market order.
        """
        # Check max multiplier
        current_remaining = _d(await asyncio.to_thread(
            lambda: self.store.get_position(ssot_id=ssot_id).get("remaining_qty")
        ), Decimal("0"))
        
        if (current_remaining + add_qty) > (original_qty * self.max_multiplier):
            logger.warning(
                "Pyramid scale %s would exceed max multiplier (ssot_id=%s, max=%s)",
                scale_label, ssot_id, self.max_multiplier
            )
            return False

        # Place market order
        open_side = "BUY" if side_norm == "LONG" else "SELL"
        
        resp = await asyncio.to_thread(
            self.bingx.place_market_order,
            symbol=symbol,
            side=open_side,
            quantity=add_qty,
            reduce_only=False,
            position_side=side_norm,
        )

        order_id = resp.get("orderId")
        if not order_id:
            logger.error("Pyramid scale %s failed (ssot_id=%s): %s", scale_label, ssot_id, resp.get("error"))
            return False

        # Track the order
        await asyncio.to_thread(
            self.store.upsert_order_tracker,
            ssot_id=ssot_id,
            order_id=str(order_id),
            kind=f"PYRAMID_{scale_label}",
            level_index=None,
        )

        return True

