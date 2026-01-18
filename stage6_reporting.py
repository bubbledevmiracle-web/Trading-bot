#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 6 - Reporting (Daily / Weekly)
====================================
Aggregates performance and error stats from SSOT sources:
- SQLite (ssot_queue / stage4_positions)
- Telemetry JSONL (TP/SL fills, hedge events, telegram send errors, API errors)

All values are derived from stored data (logs/DB), never inferred.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Dict, Any, Optional, Iterable, Tuple

import config
from lifecycle_store import LifecycleStore
from ssot_store import SignalStore
from stage6_telemetry import TelemetryLogger, TelemetryCorrelation
from stage6_telegram import send_telegram_with_telemetry

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _local_tz():
    tz_name = str(getattr(config, "TIMEZONE", "UTC") or "UTC")
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _start_of_day_local(d: date) -> datetime:
    tz = _local_tz()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)


def _end_of_day_local(d: date) -> datetime:
    return _start_of_day_local(d) + timedelta(days=1)


def _week_bounds_local(d: date) -> Tuple[datetime, datetime]:
    # Week starts Monday, ends next Monday.
    monday = d - timedelta(days=int(d.weekday()))
    start = _start_of_day_local(monday)
    end = start + timedelta(days=7)
    return start, end


def _read_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return []
    def _iter():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                s = (line or "").strip()
                if not s:
                    continue
                try:
                    yield json.loads(s)
                except Exception:
                    continue
    return _iter()


@dataclass(frozen=True)
class Stage6ReportWindow:
    name: str  # DAILY / WEEKLY
    start_utc: datetime
    end_utc: datetime


class Stage6Reporter:
    def __init__(
        self,
        *,
        telemetry: TelemetryLogger,
        telemetry_jsonl_path: Path,
        ssot_store: Optional[SignalStore],
        lifecycle_store: Optional[LifecycleStore],
    ):
        self.telemetry = telemetry
        self.telemetry_jsonl_path = Path(telemetry_jsonl_path)
        self.ssot_store = ssot_store
        self.lifecycle_store = lifecycle_store

    def build_report(self, *, window: Stage6ReportWindow) -> Dict[str, Any]:
        """
        Build an aggregated report dict. Pure computation from persisted sources.
        """
        start = window.start_utc
        end = window.end_utc

        out: Dict[str, Any] = {
            "window": {
                "name": window.name,
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
            },
            "trade_performance": {},
            "strategy_usage": {},
            "error_statistics": {},
        }

        # ------------------------------------------------------------------
        # Signals / execution counts from SQLite (SSoT queue is authoritative)
        # ------------------------------------------------------------------
        total_signals = 0
        total_executed = 0
        if self.ssot_store is not None:
            total_signals = self.ssot_store.count_signals_received_between(start_utc=start, end_utc=end)
            total_executed = self.ssot_store.count_signals_with_status_between(
                statuses=["COMPLETED"],
                start_utc=start,
                end_utc=end,
            )

        # ------------------------------------------------------------------
        # Trade outcomes + TP/SL stats from telemetry (SSOT)
        # ------------------------------------------------------------------
        pnl_usdt = 0.0
        tp_hits_by_index: Dict[int, int] = {}
        tp_fill_qty_by_index: Dict[int, float] = {}
        sl_fill_count = 0
        hedge_count = 0
        reentry_attempt_count = 0
        reentry_success_count = 0

        # Outcomes by ssot_id from POSITION_CLOSED
        closed_reason_by_ssot: Dict[int, str] = {}

        # Error counters
        error_by_type: Dict[str, int] = {}

        seen_event_keys: set[str] = set()

        for evt in _read_jsonl(self.telemetry_jsonl_path):
            k = str(evt.get("event_key") or "")
            if k and k in seen_event_keys:
                continue
            if k:
                seen_event_keys.add(k)

            ts = _parse_iso(evt.get("ts_utc") or "")
            if ts is None:
                continue
            if not (start <= ts < end):
                continue

            et = str(evt.get("event_type") or "")
            lvl = str(evt.get("level") or "").upper()
            payload = evt.get("payload") or {}
            corr = evt.get("correlation") or {}

            if lvl == "ERROR":
                error_by_type[et] = int(error_by_type.get(et, 0)) + 1

            if et in {"TP_FILL", "SL_FILL"}:
                p = payload.get("pnl_usdt")
                try:
                    if p is not None:
                        pnl_usdt += float(p)
                except Exception:
                    pass

            if et == "TP_FILL":
                try:
                    tp_index = int(payload.get("tp_index") or 0)
                except Exception:
                    tp_index = 0
                if tp_index > 0:
                    tp_hits_by_index[tp_index] = int(tp_hits_by_index.get(tp_index, 0)) + 1
                    try:
                        q = float(payload.get("fill_qty") or 0)
                    except Exception:
                        q = 0.0
                    tp_fill_qty_by_index[tp_index] = float(tp_fill_qty_by_index.get(tp_index, 0.0)) + q

            if et == "SL_FILL":
                sl_fill_count += 1

            if et == "HEDGE_OPENED":
                hedge_count += 1

            if et == "REENTRY_ATTEMPT":
                reentry_attempt_count += 1
            if et == "REENTRY_COMPLETED":
                if str(payload.get("status") or "").upper() == "COMPLETED":
                    reentry_success_count += 1

            if et == "POSITION_CLOSED":
                try:
                    ssot_id = int((corr or {}).get("ssot_id") or 0)
                except Exception:
                    ssot_id = 0
                if ssot_id > 0:
                    closed_reason_by_ssot[ssot_id] = str(payload.get("reason") or "")

        closed_total = len(closed_reason_by_ssot)
        wins = 0
        losses = 0
        for _, reason in closed_reason_by_ssot.items():
            if "sl filled" in (reason or "").lower():
                losses += 1
            else:
                wins += 1

        win_rate = (wins / closed_total) if closed_total > 0 else 0.0

        out["trade_performance"] = {
            "total_signals": int(total_signals),
            "total_executed_trades": int(total_executed),
            "closed_trades": int(closed_total),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": float(win_rate),
            "pnl_usdt": float(pnl_usdt),
        }

        out["strategy_usage"] = {
            "tp_hits_by_index": {str(k): int(v) for k, v in sorted(tp_hits_by_index.items())},
            "tp_fill_qty_by_index": {str(k): float(v) for k, v in sorted(tp_fill_qty_by_index.items())},
            "sl_fill_count": int(sl_fill_count),
            "hedge_count": int(hedge_count),
            "reentry_attempt_count": int(reentry_attempt_count),
            "reentry_success_count": int(reentry_success_count),
            "pyramid_count": 0,
            "trailing_stop_activations": 0,
        }

        error_total = int(sum(error_by_type.values()))
        out["error_statistics"] = {
            "error_total": error_total,
            "error_by_event_type": {k: int(v) for k, v in sorted(error_by_type.items())},
            "error_rate_per_signal": (error_total / float(total_signals)) if total_signals > 0 else 0.0,
        }

        return out

    def format_report_text(self, report: Dict[str, Any]) -> str:
        w = report.get("window") or {}
        tp = report.get("trade_performance") or {}
        su = report.get("strategy_usage") or {}
        es = report.get("error_statistics") or {}

        lines = []
        lines.append(f"📊 {w.get('name','REPORT')} REPORT")
        lines.append(f"🕒 Window (UTC): {w.get('start_utc')} → {w.get('end_utc')}")
        lines.append("")
        lines.append("━━ Trade Performance ━━")
        lines.append(f"Signals: {tp.get('total_signals',0)}")
        lines.append(f"Executed: {tp.get('total_executed_trades',0)}")
        lines.append(f"Closed: {tp.get('closed_trades',0)}")
        lines.append(f"Wins / Losses: {tp.get('wins',0)} / {tp.get('losses',0)}")
        lines.append(f"Win rate: {float(tp.get('win_rate',0.0))*100:.2f}%")
        lines.append(f"PnL (USDT): {float(tp.get('pnl_usdt',0.0)):.4f}")
        lines.append("")
        lines.append("━━ Strategy Usage ━━")
        lines.append(f"TP hits: {json.dumps(su.get('tp_hits_by_index',{}), ensure_ascii=False)}")
        lines.append(f"SL hits: {su.get('sl_fill_count',0)}")
        lines.append(f"Hedge count: {su.get('hedge_count',0)}")
        lines.append(f"Re-entry attempts/success: {su.get('reentry_attempt_count',0)}/{su.get('reentry_success_count',0)}")
        lines.append("")
        lines.append("━━ Errors ━━")
        lines.append(f"Total errors: {es.get('error_total',0)}")
        lines.append(f"Error rate per signal: {float(es.get('error_rate_per_signal',0.0))*100:.2f}%")
        return "\n".join(lines)


class Stage6ReportScheduler:
    def __init__(
        self,
        *,
        telemetry: TelemetryLogger,
        reporter: Stage6Reporter,
        telegram_client=None,
        telegram_chat_id: Optional[str] = None,
        state_path: Optional[Path] = None,
    ):
        self.telemetry = telemetry
        self.reporter = reporter
        self.telegram_client = telegram_client
        self.telegram_chat_id = telegram_chat_id or getattr(config, "PERSONAL_CHANNEL_ID", None)
        self.state_path = Path(state_path or (config.LOG_DIR / "stage6_report_state.json"))

    def _load_state(self) -> dict:
        try:
            if not self.state_path.exists():
                return {}
            return json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            return

    async def run_forever(self) -> None:
        if not getattr(config, "STAGE6_REPORTS_ENABLE", True):
            return
        poll_s = 30
        while True:
            try:
                await self._tick_once()
            except Exception as e:
                self.telemetry.emit(
                    event_type="STAGE6_REPORT_SCHEDULER_ERROR",
                    level="ERROR",
                    subsystem="REPORTING",
                    message=str(e),
                )
            await asyncio.sleep(poll_s)

    async def _tick_once(self) -> None:
        if self.telegram_client is None or self.telegram_chat_id is None:
            return

        tz = _local_tz()
        now_local = datetime.now(tz)
        state = self._load_state()

        # Daily
        daily_at = str(getattr(config, "STAGE6_REPORT_DAILY_AT_LOCAL_TIME", "23:59"))
        hh, mm = [int(x) for x in daily_at.split(":")]
        if now_local.hour == hh and now_local.minute == mm:
            day_key = now_local.date().isoformat()
            if state.get("daily_last_sent") != day_key:
                d = now_local.date()
                window = Stage6ReportWindow(
                    name="DAILY",
                    start_utc=_start_of_day_local(d),
                    end_utc=_end_of_day_local(d),
                )
                await self._send_report(window=window)
                state["daily_last_sent"] = day_key
                self._save_state(state)

        # Weekly
        weekly_day = str(getattr(config, "STAGE6_REPORT_WEEKLY_DAY", "SUN")).upper()
        weekly_at = str(getattr(config, "STAGE6_REPORT_WEEKLY_AT_LOCAL_TIME", "23:59"))
        hh2, mm2 = [int(x) for x in weekly_at.split(":")]
        if now_local.strftime("%a").upper().startswith(weekly_day[:3]) and now_local.hour == hh2 and now_local.minute == mm2:
            start, end = _week_bounds_local(now_local.date())
            week_key = start.date().isoformat()
            if state.get("weekly_last_sent") != week_key:
                window = Stage6ReportWindow(name="WEEKLY", start_utc=start, end_utc=end)
                await self._send_report(window=window)
                state["weekly_last_sent"] = week_key
                self._save_state(state)

    async def _send_report(self, *, window: Stage6ReportWindow) -> None:
        report = await asyncio.to_thread(self.reporter.build_report, window=window)
        text = self.reporter.format_report_text(report)

        self.telemetry.emit(
            event_type="REPORT_GENERATED",
            level="INFO",
            subsystem="REPORTING",
            message="Report generated",
            correlation=TelemetryCorrelation(bot_order_id=f"report-{window.name.lower()}"),
            payload={"window": report.get("window"), "trade_performance": report.get("trade_performance")},
        )

        if not getattr(config, "STAGE6_REPORT_SEND_TO_TELEGRAM", True):
            return

        await send_telegram_with_telemetry(
            telegram_client=self.telegram_client,
            chat_id=self.telegram_chat_id,
            text=text,
            telemetry=self.telemetry,
            correlation=TelemetryCorrelation(bot_order_id=f"report-{window.name.lower()}"),
        )


