#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 4 - Lifecycle Store (SQLite)
==================================
Durable, idempotent storage for TP/SL lifecycle management.

This store is intentionally independent from Stage 1/2 SignalStore logic, but it
uses the same SQLite database file for operational simplicity.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Stage2CompletedRow:
    ssot_id: int
    symbol: str
    side: str
    entry_price: str
    sl_price: str
    tp_prices: List[str]
    stage2_json: Optional[str]


class LifecycleStore:
    def __init__(
        self,
        db_path: Path,
        *,
        enable_wal: bool = True,
        busy_timeout_ms: int = 5000,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Stage 4 uses asyncio.to_thread(...) for DB work. SQLite defaults to "same thread only",
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
            self._conn.execute("PRAGMA journal_mode = WAL;")
        self._ensure_schema()

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    def count_positions_not_closed(self) -> int:
        """
        Count positions that are not CLOSED (capacity metric).
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    "SELECT COUNT(1) AS c FROM stage4_positions WHERE UPPER(status) != 'CLOSED';"
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
                CREATE TABLE IF NOT EXISTS stage4_positions (
                    ssot_id                 INTEGER PRIMARY KEY,
                    symbol                  TEXT NOT NULL,
                    side                    TEXT NOT NULL,
                    status                  TEXT NOT NULL,
                    planned_qty             TEXT,
                    remaining_qty           TEXT,
                    avg_entry               TEXT,
                    sl_price                TEXT,
                    sl_order_id             TEXT,
                    tp_levels_json          TEXT NOT NULL,
                    created_at_utc          TEXT NOT NULL,
                    updated_at_utc          TEXT NOT NULL,
                    last_reconcile_at_utc   TEXT
                );

                CREATE TABLE IF NOT EXISTS stage4_order_tracker (
                    order_id                TEXT PRIMARY KEY,
                    ssot_id                 INTEGER NOT NULL,
                    kind                    TEXT NOT NULL,
                    level_index             INTEGER,
                    last_executed_qty       TEXT NOT NULL DEFAULT '0',
                    last_status             TEXT,
                    updated_at_utc          TEXT NOT NULL,
                    FOREIGN KEY(ssot_id) REFERENCES stage4_positions(ssot_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stage5_locks (
                    symbol                  TEXT NOT NULL,
                    side                    TEXT NOT NULL,
                    locked                  INTEGER NOT NULL DEFAULT 1,
                    locked_at_utc           TEXT NOT NULL,
                    locked_by_ssot_id       INTEGER,
                    reason                  TEXT,
                    PRIMARY KEY(symbol, side)
                );

                CREATE INDEX IF NOT EXISTS idx_stage4_positions_symbol
                ON stage4_positions(symbol);
                """
            )
            # Lightweight migrations (restart-safe) for Stage 5.
            # Signal-level immutable values (preferred naming).
            self._ensure_column("stage4_positions", "signal_entry_price", "TEXT")
            self._ensure_column("stage4_positions", "signal_sl_price", "TEXT")
            self._ensure_column("stage4_positions", "signal_leverage", "TEXT")

            # Back-compat names (older Stage 5 implementation).
            self._ensure_column("stage4_positions", "orig_entry_price", "TEXT")
            self._ensure_column("stage4_positions", "orig_sl_price", "TEXT")
            self._ensure_column("stage4_positions", "orig_leverage", "TEXT")

            # Stage 5 state (preferred naming).
            self._ensure_column("stage4_positions", "stage5_is_hedge_armed", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("stage4_positions", "stage5_hedge_state", "TEXT")
            self._ensure_column("stage4_positions", "stage5_reentry_attempt_count", "INTEGER NOT NULL DEFAULT 0")

            # Back-compat names.
            self._ensure_column("stage4_positions", "stage5_hedge_armed", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("stage4_positions", "stage5_hedge_status", "TEXT")
            self._ensure_column("stage4_positions", "stage5_hedge_entry_order_id", "TEXT")
            self._ensure_column("stage4_positions", "stage5_hedge_tp_order_id", "TEXT")
            self._ensure_column("stage4_positions", "stage5_hedge_sl_order_id", "TEXT")
            self._ensure_column("stage4_positions", "stage5_reentry_attempts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("stage4_positions", "closed_reason", "TEXT")
            self._ensure_column("stage4_positions", "closed_at_utc", "TEXT")
            
            # Pyramid/scaling state (Stage 4.5)
            self._ensure_column("stage4_positions", "pyramid_state_json", "TEXT")
            
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cur = self._conn.cursor()
        try:
            rows = cur.execute(f"PRAGMA table_info({table});").fetchall()
            existing = {r["name"] for r in rows}
            if column in existing:
                return
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl};")
        finally:
            cur.close()

    # ---------------------------------------------------------------------
    # Discovery: Stage 2 completed rows (from ssot_queue)
    # ---------------------------------------------------------------------
    def list_new_stage2_completed(self, *, limit: int = 25) -> List[Stage2CompletedRow]:
        """
        Find Stage 2 COMPLETED signals that are not yet initialized in Stage 4.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    """
                    SELECT
                        q.id AS ssot_id,
                        q.symbol,
                        q.side,
                        q.entry_price,
                        q.sl_price,
                        q.tp_prices_json,
                        q.stage2_json
                    FROM ssot_queue q
                    LEFT JOIN stage4_positions p
                           ON p.ssot_id = q.id
                    WHERE q.status = 'COMPLETED'
                      AND p.ssot_id IS NULL
                    ORDER BY q.id ASC
                    LIMIT ?;
                    """,
                    (int(limit),),
                ).fetchall()
                out: List[Stage2CompletedRow] = []
                for r in rows:
                    out.append(
                        Stage2CompletedRow(
                            ssot_id=int(r["ssot_id"]),
                            symbol=r["symbol"],
                            side=r["side"],
                            entry_price=r["entry_price"],
                            sl_price=r["sl_price"],
                            tp_prices=json.loads(r["tp_prices_json"] or "[]"),
                            stage2_json=r["stage2_json"],
                        )
                    )
                return out
            finally:
                cur.close()

    # ---------------------------------------------------------------------
    # Stage 4 positions
    # ---------------------------------------------------------------------
    def create_position_if_absent(
        self,
        *,
        ssot_id: int,
        symbol: str,
        side: str,
        status: str,
        planned_qty: Optional[str],
        remaining_qty: Optional[str],
        avg_entry: Optional[str],
        sl_price: Optional[str],
        signal_entry_price: Optional[str] = None,
        signal_sl_price: Optional[str] = None,
        signal_leverage: Optional[str] = None,
        # Back-compat inputs (older naming).
        orig_entry_price: Optional[str] = None,
        orig_sl_price: Optional[str] = None,
        orig_leverage: Optional[str] = None,
        tp_levels: List[Dict[str, Any]],
    ) -> bool:
        """
        Create a stage4_positions row once; safe to call repeatedly.
        Returns True if inserted, False if already exists.
        """
        now = _utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN;")
                cur.execute(
                    """
                    INSERT OR IGNORE INTO stage4_positions(
                        ssot_id, symbol, side, status,
                        planned_qty, remaining_qty, avg_entry,
                        sl_price, sl_order_id,
                        signal_entry_price, signal_sl_price, signal_leverage,
                        orig_entry_price, orig_sl_price, orig_leverage,
                        stage5_is_hedge_armed, stage5_reentry_attempt_count,
                        stage5_hedge_armed, stage5_reentry_attempts,
                        tp_levels_json,
                        created_at_utc, updated_at_utc, last_reconcile_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1, 0, 1, 0, ?, ?, ?, NULL);
                    """,
                    (
                        int(ssot_id),
                        str(symbol),
                        str(side),
                        str(status),
                        planned_qty,
                        remaining_qty,
                        avg_entry,
                        sl_price,
                        signal_entry_price,
                        signal_sl_price,
                        signal_leverage,
                        orig_entry_price,
                        orig_sl_price,
                        orig_leverage,
                        json.dumps(tp_levels, separators=(",", ":"), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                inserted = cur.rowcount > 0
                self._conn.commit()
                return inserted
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def get_position(self, *, ssot_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute("SELECT * FROM stage4_positions WHERE ssot_id = ?;", (int(ssot_id),)).fetchone()
                if r is None:
                    return None
                d = dict(r)
                d["tp_levels"] = json.loads(d.get("tp_levels_json") or "[]")
                d["pyramid_state"] = json.loads(d.get("pyramid_state_json") or "{}")
                return d
            finally:
                cur.close()

    def list_positions_by_status(self, *, statuses: List[str], limit: int = 200) -> List[Dict[str, Any]]:
        statuses_norm = [str(s).upper() for s in (statuses or []) if s]
        if not statuses_norm:
            return []
        qs = ",".join(["?"] * len(statuses_norm))
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    f"SELECT * FROM stage4_positions WHERE UPPER(status) IN ({qs}) ORDER BY ssot_id ASC LIMIT ?;",
                    (*statuses_norm, int(limit)),
                ).fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    d = dict(r)
                    d["tp_levels"] = json.loads(d.get("tp_levels_json") or "[]")
                    d["pyramid_state"] = json.loads(d.get("pyramid_state_json") or "{}")
                    out.append(d)
                return out
            finally:
                cur.close()

    def list_open_positions(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """
        List positions with status='OPEN' for pyramid monitoring.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    "SELECT * FROM stage4_positions WHERE UPPER(status) = 'OPEN' ORDER BY ssot_id ASC LIMIT ?;",
                    (int(limit),),
                ).fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    d = dict(r)
                    d["tp_levels"] = json.loads(d.get("tp_levels_json") or "[]")
                    d["pyramid_state"] = json.loads(d.get("pyramid_state_json") or "{}")
                    out.append(d)
                return out
            finally:
                cur.close()

    def list_positions_not_closed(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """
        List all Stage 4 positions where status != CLOSED.
        Used by Stage 7 for global reconciliation/cleanup (conservative).
        Also used by Pyramid manager to find scaling opportunities.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    "SELECT * FROM stage4_positions WHERE UPPER(status) != 'CLOSED' ORDER BY ssot_id ASC LIMIT ?;",
                    (int(limit),),
                ).fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    d = dict(r)
                    d["tp_levels"] = json.loads(d.get("tp_levels_json") or "[]")
                    d["pyramid_state"] = json.loads(d.get("pyramid_state_json") or "{}")
                    out.append(d)
                return out
            finally:
                cur.close()
    
    def list_open_positions(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """
        List positions with status='OPEN' for pyramid monitoring.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    "SELECT * FROM stage4_positions WHERE UPPER(status) = 'OPEN' ORDER BY ssot_id ASC LIMIT ?;",
                    (int(limit),),
                ).fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    d = dict(r)
                    d["tp_levels"] = json.loads(d.get("tp_levels_json") or "[]")
                    d["pyramid_state"] = json.loads(d.get("pyramid_state_json") or "{}")
                    out.append(d)
                return out
            finally:
                cur.close()

    def update_position(
        self,
        *,
        ssot_id: int,
        status: Optional[str] = None,
        planned_qty: Optional[str] = None,
        remaining_qty: Optional[str] = None,
        avg_entry: Optional[str] = None,
        sl_order_id: Optional[str] = None,
        sl_price: Optional[str] = None,
        signal_entry_price: Optional[str] = None,
        signal_sl_price: Optional[str] = None,
        signal_leverage: Optional[str] = None,
        orig_entry_price: Optional[str] = None,
        orig_sl_price: Optional[str] = None,
        orig_leverage: Optional[str] = None,
        stage5_hedge_armed: Optional[int] = None,
        stage5_hedge_status: Optional[str] = None,
        stage5_is_hedge_armed: Optional[int] = None,
        stage5_hedge_state: Optional[str] = None,
        stage5_hedge_entry_order_id: Optional[str] = None,
        stage5_hedge_tp_order_id: Optional[str] = None,
        stage5_hedge_sl_order_id: Optional[str] = None,
        stage5_reentry_attempts: Optional[int] = None,
        stage5_reentry_attempt_count: Optional[int] = None,
        closed_reason: Optional[str] = None,
        closed_at_utc: Optional[str] = None,
        tp_levels: Optional[List[Dict[str, Any]]] = None,
        last_reconcile_at_utc: Optional[str] = None,
        pyramid_state: Optional[dict] = None,  # NEW: Pyramid state
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                updates: List[Tuple[str, Any]] = []
                if status is not None:
                    updates.append(("status", status))
                if planned_qty is not None:
                    updates.append(("planned_qty", planned_qty))
                if remaining_qty is not None:
                    updates.append(("remaining_qty", remaining_qty))
                if avg_entry is not None:
                    updates.append(("avg_entry", avg_entry))
                if sl_order_id is not None:
                    updates.append(("sl_order_id", sl_order_id))
                if sl_price is not None:
                    updates.append(("sl_price", sl_price))
                if signal_entry_price is not None:
                    updates.append(("signal_entry_price", signal_entry_price))
                if signal_sl_price is not None:
                    updates.append(("signal_sl_price", signal_sl_price))
                if signal_leverage is not None:
                    updates.append(("signal_leverage", signal_leverage))
                if orig_entry_price is not None:
                    updates.append(("orig_entry_price", orig_entry_price))
                if orig_sl_price is not None:
                    updates.append(("orig_sl_price", orig_sl_price))
                if orig_leverage is not None:
                    updates.append(("orig_leverage", orig_leverage))
                if stage5_hedge_armed is not None:
                    updates.append(("stage5_hedge_armed", int(stage5_hedge_armed)))
                if stage5_hedge_status is not None:
                    updates.append(("stage5_hedge_status", stage5_hedge_status))
                if stage5_is_hedge_armed is not None:
                    updates.append(("stage5_is_hedge_armed", int(stage5_is_hedge_armed)))
                if stage5_hedge_state is not None:
                    updates.append(("stage5_hedge_state", stage5_hedge_state))
                if stage5_hedge_entry_order_id is not None:
                    updates.append(("stage5_hedge_entry_order_id", stage5_hedge_entry_order_id))
                if stage5_hedge_tp_order_id is not None:
                    updates.append(("stage5_hedge_tp_order_id", stage5_hedge_tp_order_id))
                if stage5_hedge_sl_order_id is not None:
                    updates.append(("stage5_hedge_sl_order_id", stage5_hedge_sl_order_id))
                if stage5_reentry_attempts is not None:
                    updates.append(("stage5_reentry_attempts", int(stage5_reentry_attempts)))
                if stage5_reentry_attempt_count is not None:
                    updates.append(("stage5_reentry_attempt_count", int(stage5_reentry_attempt_count)))
                if closed_reason is not None:
                    updates.append(("closed_reason", closed_reason))
                if closed_at_utc is not None:
                    updates.append(("closed_at_utc", closed_at_utc))
                if tp_levels is not None:
                    updates.append(("tp_levels_json", json.dumps(tp_levels, separators=(",", ":"), ensure_ascii=False)))
                if last_reconcile_at_utc is not None:
                    updates.append(("last_reconcile_at_utc", last_reconcile_at_utc))
                if pyramid_state is not None:
                    updates.append(("pyramid_state_json", json.dumps(pyramid_state, separators=(",", ":"), ensure_ascii=False)))

                # Always update updated_at_utc
                updates.append(("updated_at_utc", _utc_now_iso()))

                set_clause = ", ".join([f"{k} = ?" for k, _ in updates])
                params = [v for _, v in updates] + [int(ssot_id)]
                cur.execute(f"UPDATE stage4_positions SET {set_clause} WHERE ssot_id = ?;", params)
                self._conn.commit()
            finally:
                cur.close()

    def clear_position_fields(self, *, ssot_id: int, fields: List[str]) -> None:
        """
        Explicitly set selected nullable fields to NULL.
        """
        allowed = {
            "sl_order_id",
            "stage5_hedge_status",
            "stage5_hedge_state",
            "stage5_hedge_entry_order_id",
            "stage5_hedge_tp_order_id",
            "stage5_hedge_sl_order_id",
            "closed_reason",
            "closed_at_utc",
        }
        f = [str(x) for x in (fields or []) if x]
        f = [x for x in f if x in allowed]
        if not f:
            return
        set_clause = ", ".join([f"{col} = NULL" for col in f] + ["updated_at_utc = ?"])
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    f"UPDATE stage4_positions SET {set_clause} WHERE ssot_id = ?;",
                    (_utc_now_iso(), int(ssot_id)),
                )
                self._conn.commit()
            finally:
                cur.close()

    # ---------------------------------------------------------------------
    # Stage 5 locks (per symbol + side, unlocked only by new external signal)
    # ---------------------------------------------------------------------
    def get_stage5_lock(self, *, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                r = cur.execute(
                    "SELECT symbol, side, locked, locked_at_utc, locked_by_ssot_id, reason FROM stage5_locks WHERE symbol = ? AND side = ?;",
                    (str(symbol), str(side).upper()),
                ).fetchone()
                return dict(r) if r else None
            finally:
                cur.close()

    def set_stage5_lock(self, *, symbol: str, side: str, ssot_id: Optional[int], reason: str) -> None:
        now = _utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO stage5_locks(symbol, side, locked, locked_at_utc, locked_by_ssot_id, reason)
                    VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(symbol, side) DO UPDATE SET
                        locked = 1,
                        locked_at_utc = excluded.locked_at_utc,
                        locked_by_ssot_id = excluded.locked_by_ssot_id,
                        reason = excluded.reason;
                    """,
                    (str(symbol), str(side).upper(), now, int(ssot_id) if ssot_id is not None else None, str(reason)),
                )
                self._conn.commit()
            finally:
                cur.close()

    def clear_stage5_lock(self, *, symbol: str, side: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("DELETE FROM stage5_locks WHERE symbol = ? AND side = ?;", (str(symbol), str(side).upper()))
                self._conn.commit()
            finally:
                cur.close()
    # ---------------------------------------------------------------------
    # Order tracking (idempotency for polling-based fills)
    # ---------------------------------------------------------------------
    def upsert_order_tracker(
        self,
        *,
        ssot_id: int,
        order_id: str,
        kind: str,
        level_index: Optional[int],
        last_executed_qty: str = "0",
        last_status: Optional[str] = None,
    ) -> None:
        now = _utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO stage4_order_tracker(order_id, ssot_id, kind, level_index, last_executed_qty, last_status, updated_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        ssot_id = excluded.ssot_id,
                        kind = excluded.kind,
                        level_index = excluded.level_index,
                        last_executed_qty = COALESCE(stage4_order_tracker.last_executed_qty, excluded.last_executed_qty),
                        last_status = COALESCE(stage4_order_tracker.last_status, excluded.last_status),
                        updated_at_utc = excluded.updated_at_utc;
                    """,
                    (str(order_id), int(ssot_id), str(kind), level_index, str(last_executed_qty), last_status, now),
                )
                self._conn.commit()
            finally:
                cur.close()

    def list_tracked_orders(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    """
                    SELECT order_id, ssot_id, kind, level_index, last_executed_qty, last_status
                    FROM stage4_order_tracker
                    ORDER BY updated_at_utc ASC
                    LIMIT ?;
                    """,
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                cur.close()

    def list_tracked_orders_for_ssot_id(self, *, ssot_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        List tracked orders for a specific ssot_id.
        Used by Stage 7 cleanup to cancel open orphan orders.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    """
                    SELECT order_id, ssot_id, kind, level_index, last_executed_qty, last_status
                    FROM stage4_order_tracker
                    WHERE ssot_id = ?
                    ORDER BY updated_at_utc ASC
                    LIMIT ?;
                    """,
                    (int(ssot_id), int(limit)),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                cur.close()

    def delete_tracked_orders_for_ssot_id(self, *, ssot_id: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("DELETE FROM stage4_order_tracker WHERE ssot_id = ?;", (int(ssot_id),))
                self._conn.commit()
            finally:
                cur.close()

    def update_order_tracker(self, *, order_id: str, last_executed_qty: str, last_status: Optional[str]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    UPDATE stage4_order_tracker
                    SET last_executed_qty = ?,
                        last_status = ?,
                        updated_at_utc = ?
                    WHERE order_id = ?;
                    """,
                    (str(last_executed_qty), last_status, _utc_now_iso(), str(order_id)),
                )
                self._conn.commit()
            finally:
                cur.close()


