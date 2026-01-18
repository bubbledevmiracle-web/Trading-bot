#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSoT Signal Store (SQLite)
=========================
Persistent internal Signal Store (SSoT) for extracted Telegram signals.

This module provides a durable queue-like store for *normalized* signals:
- ssot_queue: accepted normalized signals (the SSoT queue)
- recent_signals: recent accepted signals used for deterministic deduplication (TTL window)

Author: Trading Bot Project
Date: 2026-01-14
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat naive as UTC to keep consistency in storage
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedup_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_decimal_list(values: List[str]) -> List[Decimal]:
    return [Decimal(str(v)) for v in values]


@dataclass(frozen=True)
class StoredSignal:
    source_channel_name: str
    chat_id: str
    message_id: int
    message_ts_utc: Optional[str]
    received_at_utc: str
    raw_text: str
    symbol: str
    side: str
    entry_price: str
    sl_price: str
    tp_prices: List[str]
    signal_type: str
    tick_size: str
    qty_step: str


@dataclass(frozen=True)
class QueuedSignal:
    id: int
    source_channel_name: str
    chat_id: str
    message_id: int
    message_ts_utc: Optional[str]
    received_at_utc: str
    raw_text: str
    symbol: str
    side: str
    entry_price: str
    sl_price: str
    tp_prices: List[str]
    signal_type: str
    tick_size: str
    qty_step: str
    status: str
    locked_by: Optional[str]
    locked_at_utc: Optional[str]
    stage2_json: Optional[str]
    last_error: Optional[str]


class SignalStore:
    """
    SQLite-backed persistent internal Signal Store (SSoT).
    """

    def __init__(
        self,
        db_path: Path,
        *,
        enable_wal: bool = True,
        busy_timeout_ms: int = 5000,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Stage 2/6/7 use asyncio.to_thread(...) for DB work. SQLite defaults to "same thread only",
        # so we must allow cross-thread usage and protect access with a lock.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=max(busy_timeout_ms / 1000.0, 1.0),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")
        if enable_wal:
            # WAL improves concurrent read/write and crash safety on Windows.
            self._conn.execute("PRAGMA journal_mode = WAL;")

        self._ensure_schema()

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Stage 6 - Reporting helpers (counts)
    # ------------------------------------------------------------------
    def count_signals_received_between(self, *, start_utc: datetime, end_utc: datetime) -> int:
        """
        Count accepted signals in ssot_queue by received_at_utc window.
        """
        start = _safe_iso(start_utc)
        end = _safe_iso(end_utc)
        if not start or not end:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    """
                    SELECT COUNT(1) AS c
                    FROM ssot_queue
                    WHERE received_at_utc >= ?
                      AND received_at_utc < ?;
                    """,
                    (start, end),
                ).fetchone()
                if r is None:
                    return 0
                return int(r["c"] or 0)
            finally:
                cur.close()

    def count_signals_with_status_between(
        self,
        *,
        statuses: List[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> int:
        """
        Count ssot_queue rows that match any of the given statuses within a received_at_utc window.
        """
        st = [str(s).upper() for s in (statuses or []) if s]
        if not st:
            return 0
        start = _safe_iso(start_utc)
        end = _safe_iso(end_utc)
        if not start or not end:
            return 0
        qs = ",".join(["?"] * len(st))
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    f"""
                    SELECT COUNT(1) AS c
                    FROM ssot_queue
                    WHERE UPPER(status) IN ({qs})
                      AND received_at_utc >= ?
                      AND received_at_utc < ?;
                    """,
                    (*st, start, end),
                ).fetchone()
                if r is None:
                    return 0
                return int(r["c"] or 0)
            finally:
                cur.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ssot_queue (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_channel_name TEXT NOT NULL,
                    chat_id             TEXT NOT NULL,
                    message_id          INTEGER NOT NULL,
                    message_ts_utc      TEXT,
                    received_at_utc     TEXT NOT NULL,
                    symbol              TEXT NOT NULL,
                    side                TEXT NOT NULL,
                    entry_price         TEXT NOT NULL,
                    sl_price            TEXT NOT NULL,
                    tp_prices_json      TEXT NOT NULL,
                    signal_type         TEXT NOT NULL,
                    tick_size           TEXT NOT NULL,
                    qty_step            TEXT NOT NULL,
                    dedup_hash          TEXT NOT NULL,
                    raw_text            TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_ssot_queue_received_at
                ON ssot_queue(received_at_utc);

                CREATE INDEX IF NOT EXISTS idx_ssot_queue_status
                ON ssot_queue(received_at_utc);

                CREATE TABLE IF NOT EXISTS recent_signals (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_utc      TEXT NOT NULL,
                    source_channel_name TEXT NOT NULL,
                    symbol              TEXT NOT NULL,
                    side                TEXT NOT NULL,
                    entry_price         TEXT NOT NULL,
                    sl_price            TEXT NOT NULL,
                    tp_prices_json      TEXT NOT NULL,
                    dedup_hash          TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recent_signals_lookup
                ON recent_signals(source_channel_name, symbol, side, created_at_utc);

                CREATE TABLE IF NOT EXISTS stage5_locks (
                    symbol                  TEXT NOT NULL,
                    side                    TEXT NOT NULL,
                    locked                  INTEGER NOT NULL DEFAULT 1,
                    locked_at_utc           TEXT NOT NULL,
                    locked_by_ssot_id       INTEGER,
                    reason                  TEXT,
                    PRIMARY KEY(symbol, side)
                );
                """
            )
        # Lightweight migrations for new Stage 2 execution columns.
        self._ensure_column("ssot_queue", "status", "TEXT NOT NULL DEFAULT 'QUEUED'")
        self._ensure_column("ssot_queue", "locked_by", "TEXT")
        self._ensure_column("ssot_queue", "locked_at_utc", "TEXT")
        self._ensure_column("ssot_queue", "stage2_json", "TEXT")
        self._ensure_column("ssot_queue", "last_error", "TEXT")
        with self._lock:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Stage 5 locks (per symbol + side)
    # ------------------------------------------------------------------
    def clear_stage5_lock(self, *, symbol: str, side: str) -> None:
        """
        Unlock trading for (symbol, side). Used when a new external signal arrives.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("DELETE FROM stage5_locks WHERE symbol = ? AND side = ?;", (str(symbol), str(side).upper()))
                self._conn.commit()
            finally:
                cur.close()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """
        Add a column to a table if it doesn't exist.
        Safe for repeated runs.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(f"PRAGMA table_info({table});").fetchall()
                existing = {r["name"] for r in rows}
                if column in existing:
                    return
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl};")
                self._conn.commit()
            finally:
                cur.close()

    def insert_accepted_signal(
        self,
        *,
        normalized: StoredSignal,
        dedup_hash: str,
    ) -> int:
        tp_json = json.dumps(normalized.tp_prices, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN;")
            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO ssot_queue (
                        source_channel_name, chat_id, message_id, message_ts_utc, received_at_utc,
                        symbol, side, entry_price, sl_price, tp_prices_json, signal_type,
                        tick_size, qty_step, dedup_hash, raw_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        normalized.source_channel_name,
                        normalized.chat_id,
                        int(normalized.message_id),
                        normalized.message_ts_utc,
                        normalized.received_at_utc,
                        normalized.symbol,
                        normalized.side,
                        normalized.entry_price,
                        normalized.sl_price,
                        tp_json,
                        normalized.signal_type,
                        normalized.tick_size,
                        normalized.qty_step,
                        dedup_hash,
                        normalized.raw_text,
                    ),
                )
                cur.execute(
                    "SELECT id FROM ssot_queue WHERE chat_id = ? AND message_id = ?;",
                    (normalized.chat_id, int(normalized.message_id)),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("Failed to read ssot_queue row after insert/ignore")
                ssot_id = int(row["id"])

                # Track recent accepted signal for dedup comparisons
                cur.execute(
                    """
                    INSERT INTO recent_signals (
                        created_at_utc, source_channel_name, symbol, side, entry_price, sl_price, tp_prices_json, dedup_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        normalized.received_at_utc,
                        normalized.source_channel_name,
                        normalized.symbol,
                        normalized.side,
                        normalized.entry_price,
                        normalized.sl_price,
                        tp_json,
                        dedup_hash,
                    ),
                )

                self._conn.commit()
                return ssot_id
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def claim_next_signal(self, *, worker_id: str, lock_ttl_seconds: int = 600) -> Optional[QueuedSignal]:
        """
        Atomically claim the next QUEUED signal for Stage 2 execution.

        If a row is CLAIMED but older than lock_ttl_seconds, it becomes eligible again.
        """
        now_iso = _utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE;")
                row = cur.execute(
                    """
                    SELECT id
                    FROM ssot_queue
                    WHERE status IN ('QUEUED', 'RETRY')
                       OR (
                            status = 'CLAIMED'
                            AND locked_at_utc IS NOT NULL
                            AND (strftime('%s','now') - strftime('%s', locked_at_utc)) >= ?
                       )
                    ORDER BY id ASC
                    LIMIT 1;
                    """,
                    (int(lock_ttl_seconds),),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None

                ssot_id = int(row["id"])
                cur.execute(
                    """
                    UPDATE ssot_queue
                    SET status = 'CLAIMED',
                        locked_by = ?,
                        locked_at_utc = ?
                    WHERE id = ?;
                    """,
                    (worker_id, now_iso, ssot_id),
                )

                full = cur.execute(
                    """
                    SELECT
                        id, source_channel_name, chat_id, message_id, message_ts_utc, received_at_utc,
                        raw_text, symbol, side, entry_price, sl_price, tp_prices_json, signal_type,
                        tick_size, qty_step,
                        status, locked_by, locked_at_utc, stage2_json, last_error
                    FROM ssot_queue
                    WHERE id = ?;
                    """,
                    (ssot_id,),
                ).fetchone()
                self._conn.commit()

                if full is None:
                    return None

                return QueuedSignal(
                    id=int(full["id"]),
                    source_channel_name=full["source_channel_name"],
                    chat_id=full["chat_id"],
                    message_id=int(full["message_id"]),
                    message_ts_utc=full["message_ts_utc"],
                    received_at_utc=full["received_at_utc"],
                    raw_text=full["raw_text"],
                    symbol=full["symbol"],
                    side=full["side"],
                    entry_price=full["entry_price"],
                    sl_price=full["sl_price"],
                    tp_prices=json.loads(full["tp_prices_json"]),
                    signal_type=full["signal_type"],
                    tick_size=full["tick_size"],
                    qty_step=full["qty_step"],
                    status=full["status"],
                    locked_by=full["locked_by"],
                    locked_at_utc=full["locked_at_utc"],
                    stage2_json=full["stage2_json"],
                    last_error=full["last_error"],
                )
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def update_queue_row(
        self,
        *,
        ssot_id: int,
        status: str,
        stage2: Optional[dict] = None,
        last_error: Optional[str] = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                stage2_json = json.dumps(stage2, separators=(",", ":"), ensure_ascii=False) if stage2 is not None else None
                cur.execute(
                    """
                    UPDATE ssot_queue
                    SET status = ?,
                        stage2_json = COALESCE(?, stage2_json),
                        last_error = ?
                    WHERE id = ?;
                    """,
                    (status, stage2_json, last_error, int(ssot_id)),
                )
                self._conn.commit()
            finally:
                cur.close()

    # ------------------------------------------------------------------
    # Stage 7 - Maintenance helpers (cleanup/reconcile/capacity)
    # ------------------------------------------------------------------
    def count_stage2_inflight(self) -> int:
        """
        Count Stage 2 in-flight rows that can represent live entry orders / reserved capacity.

        NOTE: This is intentionally conservative (over-count is safer than under-count).
        """
        inflight = [
            "CLAIMED",
            "STAGE2_RUNNING",
            "STAGE2_PLANNED",
            "WAITING_FOR_FILLS",
        ]
        qs = ",".join(["?"] * len(inflight))
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    f"SELECT COUNT(1) AS c FROM ssot_queue WHERE UPPER(status) IN ({qs});",
                    tuple(inflight),
                ).fetchone()
                return int((r["c"] if r else 0) or 0)
            finally:
                cur.close()

    def list_stage2_rows_older_than(
        self,
        *,
        min_age_seconds: int,
        statuses: List[str],
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        List ssot_queue rows with given statuses where received_at_utc is older than min_age_seconds.

        Returns dict rows containing: id, symbol, side, received_at_utc, entry_price, sl_price,
        tp_prices_json, status, stage2_json.
        """
        st = [str(s).upper() for s in (statuses or []) if s]
        if not st:
            return []
        qs = ",".join(["?"] * len(st))
        # Use SQLite time arithmetic (seconds since epoch) for deterministic filtering.
        # received_at_utc is stored as ISO; strftime('%s', ...) works for common ISO formats.
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    f"""
                    SELECT
                        id, symbol, side, received_at_utc,
                        entry_price, sl_price, tp_prices_json,
                        status, stage2_json, last_error
                    FROM ssot_queue
                    WHERE UPPER(status) IN ({qs})
                      AND received_at_utc IS NOT NULL
                      AND (strftime('%s','now') - strftime('%s', received_at_utc)) >= ?
                    ORDER BY id ASC
                    LIMIT ?;
                    """,
                    (*st, int(min_age_seconds), int(limit)),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                cur.close()

    def mark_queue_row(
        self,
        *,
        ssot_id: int,
        status: str,
        last_error: Optional[str] = None,
    ) -> None:
        """
        Update ssot_queue.status + last_error (Stage 7 cleanup/reconcile markers).
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    UPDATE ssot_queue
                    SET status = ?,
                        last_error = COALESCE(?, last_error)
                    WHERE id = ?;
                    """,
                    (str(status), last_error, int(ssot_id)),
                )
                self._conn.commit()
            finally:
                cur.close()

    def find_latest_ssot_id_for_symbol_side(self, *, symbol: str, side: str) -> Optional[int]:
        """
        Find most recent ssot_queue.id for a given (symbol, side).
        Used by Stage 7 restore to map an exchange position back to a signal.
        """
        sym = (symbol or "").upper().replace("-", "")
        sd = (side or "").upper()
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    """
                    SELECT id
                    FROM ssot_queue
                    WHERE UPPER(REPLACE(symbol,'-','')) = ?
                      AND UPPER(side) = ?
                    ORDER BY id DESC
                    LIMIT 1;
                    """,
                    (sym, sd),
                ).fetchone()
                return int(r["id"]) if r else None
            finally:
                cur.close()

    def get_queue_row(self, *, ssot_id: int) -> Optional[Dict[str, Any]]:
        """
        Load a single ssot_queue row by id.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute("SELECT * FROM ssot_queue WHERE id = ?;", (int(ssot_id),)).fetchone()
                return dict(r) if r else None
            finally:
                cur.close()

    def check_and_record_dedup(self, normalized: StoredSignal, *, ttl_hours: int) -> Dict[str, Any]:
        """
        Deterministic deduplication:
        - HASH(source, symbol, side, entry, TP[], SL)
        - TTL window (2h default)
        - % diff rules: ≤5% block, ≥10% accept, 5–10% deterministic via entry bucket
        - Opposite side always accepted (handled by lookup filter)
        """
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - (ttl_hours * 3600)

        entry = Decimal(normalized.entry_price)
        sl = Decimal(normalized.sl_price)
        tps = _to_decimal_list(normalized.tp_prices)

        payload = {
            "source": normalized.source_channel_name,
            "symbol": normalized.symbol,
            "side": normalized.side,
            "entry": normalized.entry_price,
            "tp": normalized.tp_prices,
            "sl": normalized.sl_price,
        }
        h = _dedup_hash(payload)

        with self._lock:
            cur = self._conn.cursor()
            try:
                # Load recent accepted signals within TTL for same (source,symbol,side)
                rows = cur.execute(
                    """
                    SELECT created_at_utc, entry_price, sl_price, tp_prices_json, dedup_hash
                    FROM recent_signals
                    WHERE source_channel_name = ?
                      AND symbol = ?
                      AND side = ?
                    ORDER BY id DESC
                    LIMIT 50;
                    """,
                    (normalized.source_channel_name, normalized.symbol, normalized.side),
                ).fetchall()

                recent: List[dict] = []
                for r in rows:
                    created_at = r["created_at_utc"]
                    try:
                        ts = datetime.fromisoformat(created_at).timestamp()
                    except Exception:
                        ts = now.timestamp()
                    if ts >= cutoff:
                        recent.append(
                            {
                                "entry": Decimal(str(r["entry_price"])),
                                "sl": Decimal(str(r["sl_price"])),
                                "tp": [Decimal(str(x)) for x in json.loads(r["tp_prices_json"])],
                                "dedup_hash": r["dedup_hash"],
                            }
                        )

                # No recent -> accept
                if not recent:
                    return {"decision": "ACCEPT", "reason": "No recent signals in TTL window", "dedup_hash": h}

                # Compute diffs
                diffs = []
                for old in recent:
                    d = {
                        "dedup_hash": old["dedup_hash"],
                        "diff_max": None,
                    }
                    diff_max = self._max_component_diff(
                        entry_a=entry,
                        sl_a=sl,
                        tps_a=tps,
                        entry_b=old["entry"],
                        sl_b=old["sl"],
                        tps_b=old["tp"],
                    )
                    d["diff_max"] = str(diff_max)
                    diffs.append((diff_max, d))

                # Rule: ≤5% -> block if any
                if any(dm <= Decimal("0.05") for dm, _ in diffs):
                    best = min(diffs, key=lambda x: x[0])
                    return {
                        "decision": "BLOCK",
                        "reason": f"Duplicate detected (≤5% diff). TTL={ttl_hours}h",
                        "dedup_hash": h,
                        "min_diff": str(best[0]),
                    }

                # Rule: ≥10% -> accept if all are ≥10%
                if all(dm >= Decimal("0.10") for dm, _ in diffs):
                    best = min(diffs, key=lambda x: x[0])
                    return {
                        "decision": "ACCEPT",
                        "reason": "All recent signals differ by ≥10% (accept)",
                        "dedup_hash": h,
                        "min_diff": str(best[0]),
                    }

                # Rule: 5–10% -> deterministic fixed split (no heuristics)
                # If min diff is closer to 5% than 10%, block; otherwise accept.
                # Deterministic threshold: 7.5%.
                best = min(diffs, key=lambda x: x[0])
                if best[0] < Decimal("0.075"):
                    return {
                        "decision": "BLOCK",
                        "reason": f"Deterministic block in 5–10% range (min_diff<{Decimal('0.075')}). TTL={ttl_hours}h",
                        "dedup_hash": h,
                        "min_diff": str(best[0]),
                    }

                return {
                    "decision": "ACCEPT",
                    "reason": "Deterministic accept in 5–10% range (min_diff>=7.5%)",
                    "dedup_hash": h,
                    "min_diff": str(best[0]),
                }
            finally:
                cur.close()

    @staticmethod
    def _max_component_diff(
        *,
        entry_a: Decimal,
        sl_a: Decimal,
        tps_a: List[Decimal],
        entry_b: Decimal,
        sl_b: Decimal,
        tps_b: List[Decimal],
    ) -> Decimal:
        # If TP count differs, treat as not "in principle identical" -> accept path (high diff)
        if len(tps_a) != len(tps_b):
            return Decimal("1.00")

        def pd(a: Decimal, b: Decimal) -> Decimal:
            if a == 0:
                return Decimal("1.00")
            return (abs(a - b) / abs(a)).copy_abs()

        diffs: List[Decimal] = [pd(entry_a, entry_b), pd(sl_a, sl_b)]
        for tp_a, tp_b in zip(tps_a, tps_b):
            diffs.append(pd(tp_a, tp_b))
        return max(diffs) if diffs else Decimal("1.00")

