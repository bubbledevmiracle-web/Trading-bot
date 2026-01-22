# Copilot instructions for trading_bot

## Big picture architecture
- Entry point is [main.py](main.py), which wires the staged pipeline and background tasks.
- Stage 0 (startup safety): governance + Telegram/BingX/SSoT checks in [startup_checker.py](startup_checker.py).
- Stage 1 (ingestion/normalization): parse + normalize + dedup + persist to SSoT SQLite in [signal_ingestion.py](signal_ingestion.py) and [ssot_store.py](ssot_store.py).
- Stage 2 (execution): dual-limit entry with merge-on-first-fill in [signal_dual_limit_entry.py](signal_dual_limit_entry.py), using [bingx_client.py](bingx_client.py).
- Stage 4 (lifecycle): TP/SL placement + polling in [signal_lifecycle_manager.py](signal_lifecycle_manager.py) with [lifecycle_store.py](lifecycle_store.py).
- Stage 5 (hedge/re-entry): -2% adverse move hedge, then re-entry with lockout until new signal in [signal_hedge_reentry_manager.py](signal_hedge_reentry_manager.py).
- Stage 4.5 (pyramiding): adds to winning positions in [signal_pyramid_manager.py](signal_pyramid_manager.py).
- Stage 6 (telemetry/reporting/watchdog): JSONL telemetry + capacity guard + reporting in [stage6_telemetry.py](stage6_telemetry.py), [stage6_watchdog.py](stage6_watchdog.py), [stage6_reporting.py](stage6_reporting.py), wired by [stage6_registry.py](stage6_registry.py). Use [stage6_telegram.py](stage6_telegram.py) for Telegram sends with telemetry.
- Stage 7 (maintenance): reconciliation and stale cleanup in [stage7_maintenance.py](stage7_maintenance.py).

## Data flow and state
- Accepted signals are persisted to SQLite SSoT queue (unique on chat_id/message_id) and used by all downstream stages; do not reintroduce in-memory dedup logic.
- Stage 4 uses a separate lifecycle table inside the same SQLite file; treat it as authoritative for open positions.
- Telemetry is append-only JSONL (single source for reporting and audit) and is correlated by ssot_id/order ids.

## Project-specific conventions
- Signal type is strictly one of SWING/DYNAMISK/FAST; missing SL forces FAST with auto SL at -2% (Stage 1 logic in [signal_ingestion.py](signal_ingestion.py)).
- Normalize symbols to USDT (e.g., BTCUSDT) and sides to LONG/SHORT before persistence.
- Exchange-confirmed state changes drive transitions (“BingX-first”); lifecycle actions are based on REST polling, not assumptions.
- Use SignalStore/LifecycleStore APIs (thread-safe + WAL). DB access is done via `asyncio.to_thread` in background loops.
- For Telegram messages, prefer `send_telegram_with_telemetry()` to keep auditability and error classification.

## Config and workflows
- Central configuration lives in [config.py](config.py). Key flags: ENABLE_TRADING, DRY_RUN, EXTRACT_SIGNALS_ONLY, STAGE*_ENABLE, SSOT_ENABLE.
- SSoT database is `data/ssot.sqlite3`; telemetry lives in `logs/telemetry.jsonl`.
- README is legacy for a prior forwarder; the current operational flow is the staged bot in [main.py](main.py).
- Dependencies are listed in [requirements.txt](requirements.txt).

## Integration points
- Telegram integration uses Pyrogram (see [main.py](main.py) and [startup_checker.py](startup_checker.py)).
- BingX integration uses REST + HMAC signing in [bingx_client.py](bingx_client.py); logging of signature details is gated by BINGX_LOG_SIGNATURE_DETAILS.
- Capacity guard can block new signals when active trades exceed limits (Stage 6 watchdog).
