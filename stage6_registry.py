#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 6 - Registry (Single Wiring Point)
========================================
Creates and wires Stage 6 services in one place so the bot has a structured layout.

Naming conventions:
- stage6_*.py for Stage 6 modules
- Registry exposes a small dataclass of created services

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import config
from lifecycle_store import LifecycleStore
from ssot_store import SignalStore
from stage6_telemetry import TelemetryLogger
from stage6_watchdog import Stage6Watchdog, Stage6WatchdogState, Stage6CapacityGuard
from stage6_reporting import Stage6Reporter, Stage6ReportScheduler


@dataclass
class Stage6Services:
    telemetry: TelemetryLogger
    watchdog_state: Stage6WatchdogState
    capacity_guard: Stage6CapacityGuard
    watchdog: Stage6Watchdog
    watchdog_task: Optional[asyncio.Task]
    reporter: Stage6Reporter
    report_scheduler: Stage6ReportScheduler
    report_task: Optional[asyncio.Task]


def create_stage6_services(
    *,
    ssot_store: Optional[SignalStore],
    lifecycle_store: Optional[LifecycleStore],
    telegram_client=None,
    telegram_chat_id: Optional[str] = None,
) -> Stage6Services:
    telemetry = TelemetryLogger(
        jsonl_path=getattr(config, "STAGE6_TELEMETRY_JSONL_PATH", config.LOG_DIR / "telemetry.jsonl"),
        bot_name="trading_bot",
        env="prod",
    )
    state = Stage6WatchdogState(
        capacity_blocked=False,
        capacity_reason=None,
        active_trades=0,
        max_active_trades=int(getattr(config, "STAGE6_MAX_ACTIVE_TRADES", 100)),
        last_tick_utc=None,
    )
    guard = Stage6CapacityGuard(state)
    watchdog = Stage6Watchdog(
        telemetry=telemetry,
        ssot_store=ssot_store,
        lifecycle_store=lifecycle_store,
        state=state,
        worker_id="stage6-watchdog",
    )

    reporter = Stage6Reporter(
        telemetry=telemetry,
        telemetry_jsonl_path=getattr(config, "STAGE6_TELEMETRY_JSONL_PATH", config.LOG_DIR / "telemetry.jsonl"),
        ssot_store=ssot_store,
        lifecycle_store=lifecycle_store,
    )
    scheduler = Stage6ReportScheduler(
        telemetry=telemetry,
        reporter=reporter,
        telegram_client=telegram_client,
        telegram_chat_id=telegram_chat_id,
    )

    task = None
    if getattr(config, "STAGE6_ENABLE", True):
        task = asyncio.create_task(watchdog.run_forever())
    report_task = None
    if getattr(config, "STAGE6_ENABLE", True) and getattr(config, "STAGE6_REPORTS_ENABLE", True):
        report_task = asyncio.create_task(scheduler.run_forever())
    return Stage6Services(
        telemetry=telemetry,
        watchdog_state=state,
        capacity_guard=guard,
        watchdog=watchdog,
        watchdog_task=task,
        reporter=reporter,
        report_scheduler=scheduler,
        report_task=report_task,
    )


