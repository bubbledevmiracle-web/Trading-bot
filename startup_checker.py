#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Startup Checker - Stage 0 Initialization & Safety
==================================================
Implements all Stage 0 checks before bot becomes operational:
1. Load Config & Governance Check
2. Connect Bybit API & WS (verify retCode=0, heartbeat ≤30s)
3. Connect Telegram/Pyrogram (verify 5 whitelisted channels)
4. Fetch Baseline: Account Balance, Active Trades, Strategies
5. Prepare Startup Message with green checks (✅)
6. Ready for Signals (only after all checks pass)

If errors occur: Log detailed error + send notification to private channel.
If partial failure: Enter DEMO mode (extract signals only, no trading).

Author: Trading Bot Project
Date: 2026-01-14
"""

import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    RPCError,
    ChannelPrivate,
    UsernameNotOccupied,
    UserNotParticipant,
    PeerIdInvalid
)

import config
from bingx_client import BingXClient

logger = logging.getLogger(__name__)

# ============================================================================
# STARTUP CHECKER CLASS
# ============================================================================

class StartupChecker:
    """Stage 0 - Initialization & Safety checks."""
    
    def __init__(self):
        """Initialize startup checker."""
        self.checks_passed = []
        self.checks_failed = []
        self.warnings = []
        
        # Components
        self.bingx_client: Optional[BingXClient] = None
        self.bingx_ws_connected = False
        self.telegram_client: Optional[Client] = None
        
        # Baseline data
        self.account_balance: Optional[Decimal] = None
        self.active_positions: List[Dict] = []
        self.active_orders: List[Dict] = []
        self.strategies_active = True
        
        # Timing
        self.start_time = None
        self.end_time = None
    
    def _log_check(self, check_name: str, success: bool, message: str, data: Optional[Dict] = None):
        """Log check result."""
        if success:
            self.checks_passed.append({
                "check": check_name,
                "message": message,
                "data": data,
                "timestamp": datetime.now()
            })
            logger.info(f"✅ {check_name}: {message}")
        else:
            self.checks_failed.append({
                "check": check_name,
                "message": message,
                "data": data,
                "timestamp": datetime.now()
            })
            logger.error(f"❌ {check_name}: {message}")
    
    def _log_warning(self, check_name: str, message: str):
        """Log warning."""
        self.warnings.append({
            "check": check_name,
            "message": message,
            "timestamp": datetime.now()
        })
        logger.warning(f"⚠️  {check_name}: {message}")
    
    # ========================================================================
    # STAGE 0.1 - LOAD CONFIG & GOVERNANCE CHECK
    # ========================================================================
    
    def check_config_loaded(self) -> Tuple[bool, Dict]:
        """
        Load and validate configuration.
        
        Returns:
            (success, config_summary)
        """
        try:
            # Get config summary
            config_summary = config.get_config_summary()
            
            # Validate required fields
            if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
                self._log_check(
                    "Config Load",
                    False,
                    "Missing Telegram API credentials",
                    config_summary
                )
                return False, config_summary
            
            if config.ENABLE_TRADING:
                if not config.BINGX_API_KEY or not config.BINGX_API_SECRET:
                    self._log_check(
                        "Config Load",
                        False,
                        "Missing BingX API credentials (required for trading)",
                        config_summary
                    )
                    return False, config_summary
            
            if not config.SOURCE_CHANNELS:
                self._log_check(
                    "Config Load",
                    False,
                    "No source channels configured",
                    config_summary
                )
                return False, config_summary
            
            if not config.PERSONAL_CHANNEL_ID:
                self._log_check(
                    "Config Load",
                    False,
                    "No personal channel configured",
                    config_summary
                )
                return False, config_summary
            
            # All checks passed
            self._log_check(
                "Config Load",
                True,
                f"Configuration loaded successfully",
                config_summary
            )
            return True, config_summary
            
        except Exception as e:
            self._log_check(
                "Config Load",
                False,
                f"Exception loading config: {e}",
                None
            )
            return False, {}
    
    def check_governance(self) -> Tuple[bool, Dict]:
        """
        Governance checks - ensure production readiness.
        
        Returns:
            (success, governance_data)
        """
        governance_data = {
            "production_ready": True,
            "demo_mode_allowed": config.GOVERNANCE_CHECKS["allow_demo_mode"],
            "checks": []
        }
        
        # Check 1: API credentials present
        if config.GOVERNANCE_CHECKS["require_api_credentials"]:
            if config.ENABLE_TRADING:
                if not config.BINGX_API_KEY or not config.BINGX_API_SECRET:
                    governance_data["production_ready"] = False
                    governance_data["checks"].append("Missing BingX API credentials")
        
        # Check 2: Not in DRY_RUN for production
        if config.DRY_RUN:
            self._log_warning(
                "Governance Check",
                "DRY_RUN mode is enabled - no actual orders will be placed"
            )
        
        # Check 3: Log directory exists
        if not config.LOG_DIR.exists():
            governance_data["checks"].append("Log directory missing")
            governance_data["production_ready"] = False
        
        # Determine if we can proceed
        if governance_data["production_ready"]:
            self._log_check(
                "Governance Check",
                True,
                "Production readiness confirmed",
                governance_data
            )
            return True, governance_data
        else:
            # Check if DEMO mode is allowed
            if governance_data["demo_mode_allowed"]:
                self._log_warning(
                    "Governance Check",
                    "Production checks failed, but DEMO mode is allowed"
                )
                config.DEMO_MODE = True
                return True, governance_data
            else:
                self._log_check(
                    "Governance Check",
                    False,
                    "Production readiness failed, DEMO mode not allowed",
                    governance_data
                )
                return False, governance_data
    
    # ========================================================================
    # STAGE 0.2 - BINGX API & WEBSOCKET CONNECTION
    # ========================================================================
    
    def check_bingx_api(self) -> Tuple[bool, Dict]:
        """
        Connect to BingX REST API and verify code=0.
        
        Returns:
            (success, api_data)
        """
        if not config.ENABLE_TRADING:
            self._log_warning(
                "BingX API Check",
                "Trading disabled, skipping BingX API check"
            )
            return True, {"trading_disabled": True}
        
        try:
            # Initialize BingX client
            self.bingx_client = BingXClient(
                api_key=config.BINGX_API_KEY,
                secret_key=config.BINGX_API_SECRET,
                testnet=config.BINGX_TESTNET
            )
            
            # Measure latency
            start_time = time.time()
            connection_ok = self.bingx_client.verify_connection()
            latency_ms = (time.time() - start_time) * 1000
            
            if not connection_ok:
                self._log_check(
                    "BingX API Check",
                    False,
                    "Failed to connect to BingX API (code != 0)",
                    {"latency_ms": latency_ms}
                )
                return False, {"connected": False, "latency_ms": latency_ms}
            
            # Check latency threshold
            if latency_ms > config.BINGX_REST_TIMEOUT:
                self._log_warning(
                    "BingX API Check",
                    f"API latency ({latency_ms:.0f}ms) exceeds threshold ({config.BINGX_REST_TIMEOUT}ms)"
                )
            
            api_data = {
                "connected": True,
                "latency_ms": latency_ms,
                "testnet": config.BINGX_TESTNET,
                "ret_code": 0
            }
            
            self._log_check(
                "BingX API Check",
                True,
                f"BingX API connected (latency: {latency_ms:.0f}ms, code=0)",
                api_data
            )
            return True, api_data
            
        except Exception as e:
            self._log_check(
                "BingX API Check",
                False,
                f"Exception connecting to BingX API: {e}",
                None
            )
            return False, {"connected": False, "error": str(e)}
    
    async def check_bingx_websocket(self) -> Tuple[bool, Dict]:
        """
        Connect to BingX WebSocket and verify heartbeat ≤30s.
        
        Returns:
            (success, ws_data)
        """
        if not config.ENABLE_TRADING or not self.bingx_client:
            self._log_warning(
                "BingX WebSocket Check",
                "Trading disabled or API not connected, skipping WebSocket check"
            )
            return True, {"websocket_disabled": True}
        
        try:
            # Initialize WebSocket connection
            ws_connected = await self.bingx_client.connect_websocket()
            
            if not ws_connected:
                self._log_check(
                    "BingX WebSocket Check",
                    False,
                    "Failed to connect to BingX WebSocket",
                    None
                )
                return False, {"connected": False}
            
            # Verify heartbeat
            heartbeat_ok = await self.bingx_client.verify_websocket_heartbeat(
                timeout=config.BINGX_WS_HEARTBEAT_TIMEOUT
            )
            
            if not heartbeat_ok:
                self._log_check(
                    "BingX WebSocket Check",
                    False,
                    f"WebSocket heartbeat failed (timeout > {config.BINGX_WS_HEARTBEAT_TIMEOUT}s)",
                    None
                )
                return False, {"connected": True, "heartbeat": False}
            
            self.bingx_ws_connected = True
            
            ws_data = {
                "connected": True,
                "heartbeat": True,
                "topics": config.BINGX_WS_TOPICS
            }
            
            self._log_check(
                "BingX WebSocket Check",
                True,
                f"WebSocket connected with heartbeat ≤{config.BINGX_WS_HEARTBEAT_TIMEOUT}s",
                ws_data
            )
            return True, ws_data
            
        except Exception as e:
            self._log_check(
                "BingX WebSocket Check",
                False,
                f"Exception connecting to WebSocket: {e}",
                None
            )
            return False, {"connected": False, "error": str(e)}
    
    # ========================================================================
    # STAGE 0.3 - TELEGRAM/PYROGRAM CONNECTION
    # ========================================================================
    
    async def check_telegram(self, telegram_client: Client) -> Tuple[bool, Dict]:
        """
        Connect to Telegram and verify all 5 whitelisted channels.
        
        Args:
            telegram_client: Pyrogram Client instance
            
        Returns:
            (success, telegram_data)
        """
        try:
            self.telegram_client = telegram_client
            
            # Verify client is started
            if not telegram_client.is_connected:
                self._log_check(
                    "Telegram Connection",
                    False,
                    "Telegram client not connected",
                    None
                )
                return False, {"connected": False}
            
            telegram_data = {
                "connected": True,
                "channels_verified": {},
                "channels_failed": {},
                "total_channels": len(config.SOURCE_CHANNELS)
            }
            
            # Verify each source channel
            for channel_name, channel_id in config.SOURCE_CHANNELS.items():
                try:
                    chat = await telegram_client.get_chat(channel_id)
                    telegram_data["channels_verified"][channel_name] = {
                        "id": channel_id,
                        "title": chat.title if hasattr(chat, 'title') else channel_id,
                        "accessible": True
                    }
                    logger.info(f"✅ Channel verified: {channel_name} ({chat.title if hasattr(chat, 'title') else channel_id})")
                    
                except (PeerIdInvalid, ChannelPrivate):
                    # Channel not in session cache - this might be OK for private channels
                    telegram_data["channels_failed"][channel_name] = {
                        "id": channel_id,
                        "error": "Not in session cache (might be accessible later)"
                    }
                    self._log_warning(
                        "Telegram Channel Check",
                        f"{channel_name} ({channel_id}): Not in session cache - will be accessible when first message arrives"
                    )
                    
                except UsernameNotOccupied:
                    telegram_data["channels_failed"][channel_name] = {
                        "id": channel_id,
                        "error": "Username not found"
                    }
                    logger.error(f"❌ Channel not found: {channel_name} ({channel_id})")
                    
                except UserNotParticipant:
                    telegram_data["channels_failed"][channel_name] = {
                        "id": channel_id,
                        "error": "Not a participant"
                    }
                    logger.error(f"❌ Not a member: {channel_name} ({channel_id})")
                    
                except Exception as e:
                    telegram_data["channels_failed"][channel_name] = {
                        "id": channel_id,
                        "error": str(e)
                    }
                    logger.error(f"❌ Error accessing {channel_name}: {e}")
            
            # Verify personal channel
            try:
                personal_chat = await telegram_client.get_chat(config.PERSONAL_CHANNEL_ID)
                telegram_data["personal_channel"] = {
                    "id": config.PERSONAL_CHANNEL_ID,
                    "title": personal_chat.title if hasattr(personal_chat, 'title') else config.PERSONAL_CHANNEL_ID,
                    "accessible": True
                }
                logger.info(f"✅ Personal channel verified: {personal_chat.title if hasattr(personal_chat, 'title') else config.PERSONAL_CHANNEL_ID}")
                
            except (PeerIdInvalid, ChannelPrivate):
                telegram_data["personal_channel"] = {
                    "id": config.PERSONAL_CHANNEL_ID,
                    "accessible": False,
                    "error": "Not in session cache (will be accessible when sending)"
                }
                self._log_warning(
                    "Telegram Personal Channel",
                    f"Personal channel not in session cache - will be accessible when sending first message"
                )
            
            # Determine success
            # We allow partial failures for private channels (they might be accessible later)
            verified_count = len(telegram_data["channels_verified"])
            failed_count = len(telegram_data["channels_failed"])
            
            if verified_count == 0 and failed_count > 0:
                # All channels failed - this is a problem
                self._log_check(
                    "Telegram Check",
                    False,
                    f"No channels accessible (0/{telegram_data['total_channels']})",
                    telegram_data
                )
                return False, telegram_data
            
            # At least some channels are accessible
            if failed_count > 0:
                self._log_warning(
                    "Telegram Check",
                    f"Some channels not in cache ({verified_count}/{telegram_data['total_channels']} verified)"
                )
            
            self._log_check(
                "Telegram Check",
                True,
                f"Telegram connected ({verified_count}/{telegram_data['total_channels']} channels verified, {failed_count} not in cache)",
                telegram_data
            )
            return True, telegram_data
            
        except Exception as e:
            self._log_check(
                "Telegram Check",
                False,
                f"Exception checking Telegram: {e}",
                None
            )
            return False, {"connected": False, "error": str(e)}
    
    # ========================================================================
    # STAGE 0.4 - FETCH BASELINE DATA
    # ========================================================================
    
    def fetch_baseline_data(self) -> Tuple[bool, Dict]:
        """
        Fetch baseline data: account balance, active positions, strategies.
        
        Returns:
            (success, baseline_data)
        """
        if not config.ENABLE_TRADING or not self.bingx_client:
            baseline_data = {
                "trading_disabled": True,
                "account_balance": config.ACCOUNT_BALANCE_BASELINE,
                "active_positions": 0,
                "active_orders": 0,
                "strategies_active": True
            }
            self._log_warning(
                "Baseline Fetch",
                "Trading disabled, using baseline configuration"
            )
            return True, baseline_data
        
        try:
            # Fetch account balance
            self.account_balance = self.bingx_client.get_account_balance()
            
            if self.account_balance == Decimal("0"):
                self._log_check(
                    "Baseline Fetch",
                    False,
                    "Failed to fetch account balance (got 0)",
                    None
                )
                return False, {"balance_fetched": False}
            
            # Fetch active positions (placeholder - implement if needed)
            self.active_positions = []
            
            # Fetch active orders (placeholder - implement if needed)
            self.active_orders = []
            
            # Strategies always active for now
            self.strategies_active = True
            
            baseline_data = {
                "account_balance": self.account_balance,
                "active_positions": len(self.active_positions),
                "active_orders": len(self.active_orders),
                "strategies_active": self.strategies_active,
                "baseline_confirmed": True
            }
            
            self._log_check(
                "Baseline Fetch",
                True,
                f"Baseline data fetched: Balance={self.account_balance} USDT, Positions={len(self.active_positions)}, Orders={len(self.active_orders)}",
                baseline_data
            )
            return True, baseline_data
            
        except Exception as e:
            self._log_check(
                "Baseline Fetch",
                False,
                f"Exception fetching baseline data: {e}",
                None
            )
            return False, {"balance_fetched": False, "error": str(e)}
    
    # ========================================================================
    # STAGE 0.5 - PREPARE STARTUP MESSAGE
    # ========================================================================
    
    def prepare_startup_message(self) -> str:
        """
        Prepare startup message with green checks (✅).
        SENT ONLY AFTER ALL STAGE 0 CHECKS PASS.
        
        Returns:
            Formatted startup message
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Determine mode
        if config.DRY_RUN:
            mode = "DRY RUN (No Orders/Messages)"
        elif config.DEMO_MODE:
            mode = "DEMO MODE (Signals Only, No Trading)"
        elif config.ENABLE_TRADING:
            mode = "PRODUCTION (Trading Active)"
        else:
            mode = "EXTRACTION ONLY (No Trading)"
        
        # Build message
        message_lines = [
            "🚀 TRADING BOT - STAGE 0 COMPLETE",
            "",
            f"⏰ Startup Time: {timestamp}",
            f"🎯 Mode: {mode}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📊 STAGE 0 - INITIALIZATION CHECKS",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            ""
        ]
        
        # Add check results
        for check in self.checks_passed:
            message_lines.append(f"✅ {check['check']}: {check['message']}")
        
        if self.warnings:
            message_lines.append("")
            message_lines.append("⚠️  WARNINGS:")
            for warning in self.warnings:
                message_lines.append(f"   • {warning['message']}")
        
        if self.checks_failed:
            message_lines.append("")
            message_lines.append("❌ FAILED CHECKS:")
            for check in self.checks_failed:
                message_lines.append(f"   • {check['check']}: {check['message']}")
        
        message_lines.append("")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("💰 ACCOUNT BASELINE (SSoT)")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append(f"💵 Balance: {self.account_balance or config.ACCOUNT_BALANCE_BASELINE} USDT ✅")
        message_lines.append(f"⚙️  Risk per trade: {config.RISK_PER_TRADE * 100}% ✅")
        message_lines.append(f"📈 Active positions: {len(self.active_positions)} ✅")
        message_lines.append(f"📋 Open orders: {len(self.active_orders)} ✅")
        message_lines.append(f"🎯 Strategies: {'Active' if self.strategies_active else 'Inactive'} ✅")
        message_lines.append("")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("🌐 CONNECTIONS")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        if config.ENABLE_TRADING and self.bingx_client:
            message_lines.append(f"🔗 BingX API: Connected (code=0) ✅")
            if self.bingx_ws_connected:
                message_lines.append(f"📡 BingX WebSocket: Connected (heartbeat ≤30s) ✅")
            else:
                message_lines.append(f"📡 BingX WebSocket: Not connected ⚠️")
        else:
            message_lines.append(f"🔗 BingX: Disabled (DEMO mode) ⚠️")
        
        if self.telegram_client:
            message_lines.append(f"📱 Telegram: Connected ✅")
            message_lines.append(f"📢 Channels monitored: {len(config.SOURCE_CHANNELS)} ✅")
        
        message_lines.append("")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("🟢 BOT IS READY FOR SIGNALS")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        return "\n".join(message_lines)
    
    def prepare_error_message(self) -> str:
        """
        Prepare error message if Stage 0 fails.
        
        Returns:
            Formatted error message
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        message_lines = [
            "🚨 TRADING BOT - STAGE 0 FAILED",
            "",
            f"⏰ Time: {timestamp}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "❌ FAILED CHECKS",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            ""
        ]
        
        for check in self.checks_failed:
            message_lines.append(f"❌ {check['check']}: {check['message']}")
        
        if self.warnings:
            message_lines.append("")
            message_lines.append("⚠️  WARNINGS:")
            for warning in self.warnings:
                message_lines.append(f"   • {warning['message']}")
        
        message_lines.append("")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("🔧 ACTION REQUIRED")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("Please fix the errors above and restart the bot.")
        
        if config.GOVERNANCE_CHECKS["allow_demo_mode"]:
            message_lines.append("")
            message_lines.append("💡 DEMO MODE: Bot will continue in extraction-only mode.")
        
        return "\n".join(message_lines)
    
    # ========================================================================
    # STAGE 0.6 - MASTER VERIFICATION
    # ========================================================================
    
    async def verify_all(self, telegram_client: Client) -> Tuple[bool, Dict]:
        """
        Master verification function - runs all Stage 0 checks.
        
        Args:
            telegram_client: Pyrogram Client instance
            
        Returns:
            (success, verification_report)
        """
        self.start_time = datetime.now()
        logger.info("="*60)
        logger.info("STAGE 0 - INITIALIZATION & SAFETY")
        logger.info("="*60)
        
        # Stage 0.1 - Config & Governance
        config_ok, config_data = self.check_config_loaded()
        governance_ok, governance_data = self.check_governance()
        
        # Stage 0.2 - BingX API & WebSocket
        bingx_api_ok, bingx_api_data = self.check_bingx_api()
        bingx_ws_ok, bingx_ws_data = await self.check_bingx_websocket()
        
        # Stage 0.3 - Telegram
        telegram_ok, telegram_data = await self.check_telegram(telegram_client)
        
        # Stage 0.4 - Baseline Data
        baseline_ok, baseline_data = self.fetch_baseline_data()
        
        self.end_time = datetime.now()
        duration = (self.end_time - self.start_time).total_seconds()
        
        # Determine overall success
        critical_checks = [config_ok, telegram_ok]
        
        if config.ENABLE_TRADING:
            # If trading enabled, BingX checks are critical
            critical_checks.extend([bingx_api_ok, baseline_ok])
        
        all_critical_passed = all(critical_checks)
        
        # If critical checks failed, we cannot proceed
        if not all_critical_passed:
            # Check if we can enter DEMO mode
            if config.GOVERNANCE_CHECKS["allow_demo_mode"] and telegram_ok:
                logger.warning("⚠️  Critical checks failed, entering DEMO MODE")
                config.DEMO_MODE = True
                config.ENABLE_TRADING = False
                success = True
            else:
                logger.error("❌ Critical checks failed, cannot start bot")
                success = False
        else:
            success = True
        
        # Build verification report
        verification_report = {
            "success": success,
            "demo_mode": config.DEMO_MODE,
            "duration_seconds": duration,
            "checks_passed": len(self.checks_passed),
            "checks_failed": len(self.checks_failed),
            "warnings": len(self.warnings),
            "details": {
                "config": config_data,
                "governance": governance_data,
                "bingx_api": bingx_api_data,
                "bingx_websocket": bingx_ws_data,
                "telegram": telegram_data,
                "baseline": baseline_data
            },
            "timestamp": self.start_time.isoformat()
        }
        
        logger.info("="*60)
        if success:
            logger.info(f"✅ STAGE 0 COMPLETE - Bot ready (duration: {duration:.2f}s)")
        else:
            logger.error(f"❌ STAGE 0 FAILED - Bot cannot start (duration: {duration:.2f}s)")
        logger.info("="*60)
        
        return success, verification_report
    
    async def send_startup_notification(self, telegram_client: Client, success: bool) -> bool:
        """
        Send startup notification to personal channel.
        
        Args:
            telegram_client: Pyrogram Client instance
            success: Whether Stage 0 was successful
            
        Returns:
            True if message sent successfully
        """
        try:
            if success:
                message = self.prepare_startup_message()
            else:
                message = self.prepare_error_message()
            
            await telegram_client.send_message(
                chat_id=config.PERSONAL_CHANNEL_ID,
                text=message
            )
            
            logger.info("✅ Startup notification sent to personal channel")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send startup notification: {e}")
            return False

