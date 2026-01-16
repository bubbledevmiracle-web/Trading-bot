#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration Manager
====================
Centralized configuration for the trading bot.
All API keys, channels, risk parameters, and settings in one place.

Author: Trading Bot Project
Date: 2026-01-14
"""

from decimal import Decimal
from pathlib import Path
from datetime import timedelta

# ============================================================================
# TELEGRAM CONFIGURATION
# ============================================================================

# Telegram API Credentials
TELEGRAM_API_ID = 27590479
TELEGRAM_API_HASH = "6e60321cbb996b499b6a370af62342de"
TELEGRAM_PHONE_NUMBER = "+46 70 368 9310"
TELEGRAM_SESSION_FILE = "telegram_session"

# Source Channels to Monitor (5 channels - all must be verified)
SOURCE_CHANNELS = {
    "CRYPTORAKETEN": "-1002290339976",
    "SMART_CRYPTO": "-1002339729195",
    "Ramos Crypto": "-1002972812097",
    "SWE Crypto": "-1002234181057",
    "Hassan tahnon": "-1003598458712"
}

# Personal Channel (destination for startup messages and errors)
PERSONAL_CHANNEL_ID = "-1003179263982"

# Telegram Connection Timeout
TELEGRAM_CONNECTION_TIMEOUT = 30  # seconds

# ============================================================================
# BINGX CONFIGURATION
# ============================================================================

# BingX API Credentials
BINGX_API_KEY = "Z3w6CaFqcLhk05UfB58enOYrvULTCtaSnGcye7CtWpbERiNfDXsDT9x79IDVw77atzAxeLA4tjZ03lpFerGWCA"
BINGX_API_SECRET = "vjQfaT0l3kXooWHLLBQT1yV8J6GXHNgPLO3y0x760kdT8piEaIZ51168J57SoGX8FV8dXCrNBU8FHMzM3w"

# BingX Environment
BINGX_TESTNET = True  # False = Mainnet, True = Testnet

# BingX Connection Thresholds
BINGX_REST_TIMEOUT = 500  # milliseconds (p95)
BINGX_WS_HEARTBEAT_TIMEOUT = 30  # seconds

# ============================================================================
# TRADING PARAMETERS (SSoT - Single Source of Truth)
# ============================================================================

# Account Baseline
ACCOUNT_BALANCE_BASELINE = Decimal("402.10")  # USDT

# Risk Management
RISK_PER_TRADE = Decimal("0.02")  # 2% per trade
INITIAL_MARGIN_PLAN = Decimal("20.00")  # USDT per trade
MAX_LEVERAGE = Decimal("50.00")
MIN_LEVERAGE = Decimal("1.00")

# Order Cleanup Timeouts
TIMEOUT_SHORT = timedelta(hours=24)  # Hanging opening orders
TIMEOUT_LONG = timedelta(days=6)  # Unfilled orders

# ============================================================================
# BOT OPERATION MODES
# ============================================================================

# Feature Flags
ENABLE_TRADING = True  # Set to False to disable trading (only extract signals)
DRY_RUN = False  # True = No actual orders/messages, False = Production

# Demo Mode (set automatically if Stage 0 fails partially)
DEMO_MODE = False  # Will be set by startup_checker

# ============================================================================
# SIGNAL EXTRACTION CONFIGURATION
# ============================================================================

# Duplicate Detection TTL (hours)
DUPLICATE_TTL_HOURS = 2

# Stage 1 requires a canonical signal_type ∈ {SWING, DYNAMISK, FAST}.
# Some sources don't explicitly include either keywords or leverage in the message text.
# These defaults ensure deterministic behavior instead of rejecting otherwise valid signals.
DEFAULT_SIGNAL_TYPE_WHEN_MISSING = "SWING"
DEFAULT_SIGNAL_TYPE_BY_CHANNEL = {
    # SMART_CRYPTO "OPPORTUNITY DETECTED" templates are typically quick-move oriented.
    "SMART_CRYPTO": "FAST",
}

# Signal Extraction Logging
EXTRACT_SIGNALS_LOG = Path("logs/extracted_signals.log")
EXTRACT_SIGNALS_ONLY = False  # True = Extract but don't forward to private channel

# ============================================================================
# SSoT (SIGNAL STORE) - SQLITE CONFIGURATION
# ============================================================================

# Persistent internal Signal Store (SSoT) queue (SQLite)
SSOT_ENABLE = True
SSOT_DB_PATH = Path("data/ssot.sqlite3")
SSOT_SQLITE_WAL = True
SSOT_SQLITE_BUSY_TIMEOUT_MS = 5000

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

# Log Directory and Files
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "telegram_forwarder.log"
STARTUP_LOG_FILE = LOG_DIR / "startup_checks.log"
ERROR_LOG_FILE = LOG_DIR / "errors.log"

# Log Format
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL

# ============================================================================
# TIMEZONE CONFIGURATION
# ============================================================================

TIMEZONE = "Europe/Stockholm"  # Use "UTC" for UTC time

# ============================================================================
# GOVERNANCE & VALIDATION RULES
# ============================================================================

# Required Checks for Production
GOVERNANCE_CHECKS = {
    "require_api_credentials": True,  # Fail if API keys missing
    "require_channel_access": True,  # Fail if can't access channels
    "require_balance_fetch": True,  # Fail if can't fetch balance
    "allow_demo_mode": True,  # Allow demo mode if trading fails but Telegram works
}

# ============================================================================
# WEBSOCKET CONFIGURATION
# ============================================================================

# WebSocket Topics
BINGX_WS_TOPICS = [
    "order",  # Order updates
    "execution",  # Trade executions
    "position",  # Position updates
    "wallet",  # Wallet updates
]

# WebSocket Reconnection
WS_RECONNECT_ATTEMPTS = 5
WS_RECONNECT_DELAY = 5  # seconds

# ============================================================================
# STAGE 2 - DUAL-LIMIT ENTRY EXECUTION
# ============================================================================

# Enable Stage 2 executor (dequeues from SSoT queue and places orders)
STAGE_DUAL_LIMIT_ENTRY_EXECUTION = True

# If Stage 1 stores only a single entry price (not a zone), Stage 2 needs a deterministic Δ.
# Default: 0.10% of Em.
STAGE2_DEFAULT_SPREAD_PCT = Decimal("0.001")

# Maker-safety tick shifting (bounded)
STAGE2_MAX_PRICE_SHIFTS = 50

# Polling (since WS execution stream is not implemented yet)
STAGE2_POLL_INTERVAL_SECONDS = 3

# Hard timeout for waiting for *any* fill before considering the execution stale.
# This is separate from global cleanup jobs; keep it conservative.
STAGE2_FIRST_FILL_TIMEOUT_SECONDS = int(timedelta(hours=24).total_seconds())

# Hard timeout for waiting for full fill after merge/replacement.
STAGE2_TOTAL_FILL_TIMEOUT_SECONDS = int(timedelta(days=6).total_seconds())

# ============================================================================
# STAGE 4 - TP/SL & LIFECYCLE MANAGEMENT (BINGX)
# ============================================================================

# Enable Stage 4 lifecycle manager (places TP/SL after Stage 2 completed; monitors fills)
STAGE4_ENABLE = True

# Polling interval for Stage 4 REST-based monitoring
STAGE4_POLL_INTERVAL_SECONDS = 3

# How many newly completed Stage 2 rows to initialize per loop
STAGE4_INIT_BATCH_LIMIT = 10

# TP allocation policy (currently implemented: EQUAL)
STAGE4_TP_SPLIT_MODE = "EQUAL"

# After TP1 gets any confirmed fill, move SL to Break-Even (avg_entry)
STAGE4_MOVE_SL_TO_BE_AFTER_TP1 = True

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def ensure_directories():
    """Ensure all required directories exist."""
    LOG_DIR.mkdir(exist_ok=True)
    EXTRACT_SIGNALS_LOG.parent.mkdir(exist_ok=True)
    SSOT_DB_PATH.parent.mkdir(exist_ok=True)

def get_config_summary() -> dict:
    """Get configuration summary for logging."""
    return {
        "telegram": {
            "api_id": TELEGRAM_API_ID,
            "phone": TELEGRAM_PHONE_NUMBER,
            "source_channels": len(SOURCE_CHANNELS),
            "personal_channel": PERSONAL_CHANNEL_ID,
        },
        "bingx": {
            "testnet": BINGX_TESTNET,
            "api_key_set": bool(BINGX_API_KEY),
            "api_secret_set": bool(BINGX_API_SECRET),
        },
        "trading": {
            "enable_trading": ENABLE_TRADING,
            "dry_run": DRY_RUN,
            "demo_mode": DEMO_MODE,
            "account_balance_baseline": str(ACCOUNT_BALANCE_BASELINE),
            "risk_per_trade": str(RISK_PER_TRADE),
        },
        "operation": {
            "extract_signals_only": EXTRACT_SIGNALS_ONLY,
            "duplicate_ttl_hours": DUPLICATE_TTL_HOURS,
            "ssot_enable": SSOT_ENABLE,
            "ssot_db_path": str(SSOT_DB_PATH),
        }
    }

# Initialize directories on import
ensure_directories()

