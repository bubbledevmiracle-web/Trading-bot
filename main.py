#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading Bot - Main Entry Point
===============================
Stage 0: Initialization & Safety checks before signal extraction
Stage 1+: Signal extraction and logging (no forwarding during testing)

Author: Trading Bot Project
Date: 2026-01-14
"""

import asyncio
import hashlib
import logging
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

# Fix for Windows Python 3.8+ event loop issue
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure event loop exists
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client
from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelPrivate
from pyrogram.types import Message

# Import configuration and startup checker
import config
from startup_checker import StartupChecker
from ssot_store import SignalStore
from stage1_processor import Stage1SignalProcessor

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Suppress Pyrogram dispatcher errors
logging.getLogger("pyrogram.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

# ============================================================================
# DUPLICATE DETECTION (REMOVED)
# ============================================================================
# Duplicate prevention is now handled by the persistent SSoT store via a UNIQUE
# constraint on (chat_id, message_id). This is restart-safe and idempotent.

# ============================================================================
# SIGNAL DETECTION ALGORITHM (from original main.py)
# ============================================================================

def contains_trading_keywords(text: str) -> bool:
    """Check if text contains any trading-related keywords."""
    keywords = ['entry', 'target', 'tp', 'stop', 'loss', 'leverage', 'symbol', 'trade', 'long', 'short']
    return any(keyword in text.lower() for keyword in keywords)

def should_exclude_message(text: str) -> bool:
    """Quick rejection of obvious non-signals."""
    if not text or len(text.strip()) < 10:
        return True
    
    exclusion_patterns = [
        r"\bpartial\s+close\b",
        r"\bpartial\s+take[- ]?profit\b",
        r"\bfirst\s+target\s+reached\b",
        r"\btarget\s+reached\b",
        r"\bp&l\s*:\s*[\d.]+%\b",
        r"all\s+(entry\s+)?targets?\s+achieved",
        r"all\s+take[- ]?profit\s+targets?\s+achieved",
        r"(entry|take[- ]?profit)\s+targets?\s+achieved",
        r"take[- ]?profit\s+target\s+\d+\s*✅",
        r"target\s+\d+\s*✅",
        r"tp\d*\s*✅",
        r"profit:\s*[\d.]+%\s*period:",
        r"profit:.*period:",
        r"achieved\s*(😎|✅|✔)",
        r"^(news|update|announcement|important|notice|maintenance)\s*:",
        r"system\s+update|bug\s+fix",
    ]
    
    for pattern in exclusion_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    if re.match(r'^I[\'m\s]*(ve|am|want|decided|motivated)\s+', text, re.IGNORECASE):
        if not contains_trading_keywords(text):
            return True
    
    return False

def detect_symbol(text: str) -> Tuple[bool, Optional[str]]:
    """Detect cryptocurrency symbol in message."""
    symbol_patterns = [
        (r'#([A-Z]{2,10})(?:USDT|/USDT)?\b', 'hashtag'),
        (r'#([A-Z]{2,10})\b', 'hashtag_simple'),
        (r'\b([A-Z]{2,10})USDT\b', 'usdt_suffix'),
        (r'\b([A-Z]{2,10})/USDT\b', 'slash'),
        (r'\b([A-Z]{2,10})\(USDT\)', 'parentheses'),
        (r'(?:Symbol|COIN NAME|Asset)[:\s]+([A-Z]{2,10})(?:USDT|/USDT)?', 'labeled'),
    ]
    
    for pattern, format_type in symbol_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            symbol = match.group(1) if match.groups() else match.group(0)
            if symbol and 2 <= len(symbol) <= 10 and symbol.isalpha():
                return True, format_type
    
    return False, None

def detect_direction(text: str) -> Tuple[bool, Optional[str]]:
    """Detect trading direction (LONG/SHORT)."""
    direction_keywords = {
        'LONG': r'\bLONG\b',
        'SHORT': r'\bSHORT\b',
        'BUY': r'\bBUY\b',
        'SELL': r'\bSELL\b',
    }
    
    for direction, pattern in direction_keywords.items():
        if re.search(pattern, text, re.IGNORECASE):
            return True, direction
    
    labeled_patterns = [
        r'(?:Trade Type|Signal Type|Type|Direction)[:\-]\s*(Long|Short)',
        r'Type\s*-\s*(LONG|SHORT)',
    ]
    for pattern in labeled_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            direction = match.group(1).upper()
            if direction in ['LONG', 'SHORT']:
                return True, direction
    
    return False, None

def detect_trading_data(text: str) -> Dict:
    """Detect trading data components (Entry, Targets, Stop Loss)."""
    result = {
        'has_entry': False,
        'has_targets': False,
        'has_stop_loss': False,
        'entry_patterns': [],
        'target_patterns': [],
        'sl_patterns': [],
    }
    
    # Entry patterns
    entry_patterns = [
        r'(?:➡️\s*)?Entry\s*(?:zone|Price|Targets?|Orders?)?\s*[:\-]?\s*\$?[\d.]+',
        r'Entry\s*[:\-]\s*\$?[\d.]+',
        r'Entries?\s*[:\-]?\s*\$?[\d.]+',
        # Common variants where Buy/Sell acts as entry label
        r'\bBuy\b\s*[:\-]?\s*\$?[\d.]+',
        r'\bSell\b\s*[:\-]?\s*\$?[\d.]+',
        # Multiline: "Entry :" followed by numbered lines
        r'Entry\s*:\s*(?:\s*\n\s*)*\d+[)\-]?\s*\$?[\d.]+',
    ]
    
    for pattern in entry_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_entry'] = True
            result['entry_patterns'].extend(matches)
    
    # Target patterns
    target_patterns = [
        # Require an actual price near TP/Target, to avoid "TP1 reached" updates
        r'(?:TP|Target)\s*\d*[:\-]?\s*\$?[\d.]+',
        # Multiline: "Targets:" followed by numbered/emoji-numbered lines with prices
        r'Targets?\s*:\s*(?:[\s\S]{0,120})\b\d+[)\-]\s*\$?[\d.]+',
    ]
    
    for pattern in target_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_targets'] = True
            result['target_patterns'].extend(matches)
    
    # Stop Loss patterns
    sl_patterns = [
        # Require a price near SL/Stop Loss, to avoid "move stop loss..." updates
        r'Stop[- ]?Loss\s*[:\-]?\s*\$?[\d.]+',
        r'\bSL\b\s*[:\-]?\s*\$?[\d.]+',
        r'\bSTOP\b\s*[:\-]?\s*\$?[\d.]+',
    ]
    
    for pattern in sl_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_stop_loss'] = True
            result['sl_patterns'].extend(matches)
    
    return result

def validate_signal(text: str, symbol_found: bool, direction_found: bool, trading_data: Dict) -> Tuple[bool, int, str]:
    """Validate if message is a trading signal with confidence scoring."""
    score = 0
    reasons = []
    
    if not symbol_found:
        return False, 0, "Missing symbol"
    
    if not direction_found:
        return False, 0, "Missing direction"
    
    score += 4  # Symbol found
    reasons.append("has_symbol")
    
    score += 3  # Direction found
    reasons.append("has_direction")
    
    # To avoid false positives (e.g. "Partial Close", "TP1 reached", "move stop loss"),
    # require Entry + at least one TP price. SL is optional (FAST fallback exists).
    if not trading_data['has_entry']:
        return False, score, "Missing entry"
    score += 3
    reasons.append("has_entry")

    if not trading_data['has_targets']:
        return False, score, "Missing targets"
    score += 2
    reasons.append("has_targets")

    if trading_data['has_stop_loss']:
        score += 2
        reasons.append("has_stop_loss")

    return True, score, f"Signal detected ({', '.join(reasons)})"

def is_trading_signal(message_text: str) -> Tuple[bool, str]:
    """Main algorithm: Determine if message is a trading signal."""
    if should_exclude_message(message_text):
        return False, "Excluded by hard exclusion rules"
    
    symbol_found, symbol_format = detect_symbol(message_text)
    direction_found, direction = detect_direction(message_text)
    trading_data = detect_trading_data(message_text)
    
    is_signal, confidence_score, reason = validate_signal(
        message_text, 
        symbol_found, 
        direction_found, 
        trading_data
    )
    
    return is_signal, f"{reason} (confidence: {confidence_score})"

# ============================================================================
# TELEGRAM MESSAGE FORWARDER
# ============================================================================

class TelegramForwarder:
    """Main Telegram message forwarder class."""
    
    def __init__(self):
        self.app = Client(
            config.TELEGRAM_SESSION_FILE,
            api_id=config.TELEGRAM_API_ID,
            api_hash=config.TELEGRAM_API_HASH,
            phone_number=config.TELEGRAM_PHONE_NUMBER
        )
        self.personal_channel_id = config.PERSONAL_CHANNEL_ID
        self.source_channels = config.SOURCE_CHANNELS
        self.startup_checker = None
        self.stage0_passed = False

        # Persistent internal Signal Store (SSoT)
        self.ssot_store: Optional[SignalStore] = None
        self.stage1: Optional[Stage1SignalProcessor] = None
    
    async def start(self):
        """Start the Telegram client and run Stage 0 checks."""
        logger.info("Starting Telegram client...")
        await self.app.start()
        logger.info("✅ Telegram client started successfully")

        # Initialize SSoT store (SQLite) early so ingestion can persist immediately
        if config.SSOT_ENABLE:
            self.ssot_store = SignalStore(
                config.SSOT_DB_PATH,
                enable_wal=config.SSOT_SQLITE_WAL,
                busy_timeout_ms=config.SSOT_SQLITE_BUSY_TIMEOUT_MS,
            )
            logger.info(f"✅ SSoT SQLite ready: {config.SSOT_DB_PATH}")
            self.stage1 = Stage1SignalProcessor(self.ssot_store)
        
        # ====================================================================
        # STAGE 0 - INITIALIZATION & SAFETY
        # ====================================================================
        logger.info("\n" + "="*60)
        logger.info("RUNNING STAGE 0 - INITIALIZATION & SAFETY")
        logger.info("="*60 + "\n")
        
        self.startup_checker = StartupChecker()
        stage0_success, stage0_report = await self.startup_checker.verify_all(self.app)
        
        # Send startup notification to private channel
        await self.startup_checker.send_startup_notification(self.app, stage0_success)
        
        if not stage0_success and not config.DEMO_MODE:
            logger.error("❌ Stage 0 failed and DEMO mode not allowed - exiting")
            return False
        
        self.stage0_passed = True
        
        if config.DEMO_MODE:
            logger.warning("\n⚠️  DEMO MODE ACTIVE - Extracting signals only, no trading\n")
        
        logger.info("\n" + "="*60)
        logger.info("🚀 Bot is running and monitoring channels...")
        logger.info(f"📊 Mode: {'DEMO (Extract Only)' if config.DEMO_MODE else 'PRODUCTION'}")
        logger.info(f"📂 SSoT DB: {config.SSOT_DB_PATH if config.SSOT_ENABLE else 'DISABLED'}")
        logger.info("="*60 + "\n")
        
        return True
    
    async def stop(self):
        """Stop the Telegram client."""
        logger.info("Stopping Telegram client...")
        await self.app.stop()
        if self.ssot_store is not None:
            self.ssot_store.close()
            self.ssot_store = None
            self.stage1 = None
        logger.info("✅ Telegram client stopped")
    
    async def handle_new_message(self, client: Client, message: Message):
        """Handle new message from source channels."""
        try:
            # Skip if Stage 0 didn't pass
            if not self.stage0_passed:
                return
            
            # Skip if message is None or invalid
            if not message or not hasattr(message, 'chat'):
                return
            
            # Skip non-text messages
            if not message.text:
                return
            
            # Get channel information
            try:
                chat_id = str(message.chat.id)
                chat_username = message.chat.username
            except AttributeError:
                return
            
            # Check if message is from a monitored channel
            channel_name = None
            for name, id_or_username in self.source_channels.items():
                if chat_id == id_or_username or (chat_username and f"@{chat_username}" == id_or_username):
                    channel_name = name
                    break
            
            if not channel_name:
                return  # Not from a monitored channel
            
            message_text = message.text
            
            # Signal Detection: Check if message is a trading signal
            is_signal, signal_reason = is_trading_signal(message_text)
            if not is_signal:
                logger.debug(f"⏭️  Non-signal message from {channel_name}: {signal_reason}")
                return
            
            logger.info(f"✅ Signal detected from {channel_name}: {signal_reason}")
            
            # Stage 1 – Signal Ingestion & Normalization
            # Receive -> Validate -> Normalize -> Deduplicate -> Accepted? -> Add to SSoT Queue
            if not config.SSOT_ENABLE or self.ssot_store is None or self.stage1 is None:
                logger.error("❌ SSoT is disabled or not initialized - cannot process Stage 1")
                return

            msg_dt = getattr(message, "date", None)

            # Log raw extracted data BEFORE processing (traceability)
            logger.info("\n" + "=" * 80)
            logger.info("STAGE 1 - RAW SIGNAL (PRE-PROCESS)")
            logger.info(f"Channel: {channel_name}")
            logger.info(f"Chat ID: {chat_id}")
            logger.info(f"Message ID: {message.id}")
            logger.info(f"Message Date: {msg_dt.isoformat() if msg_dt else None}")
            logger.info(f"Reason: {signal_reason}")
            logger.info("-" * 80)
            logger.info(message_text.strip())
            logger.info("=" * 80)

            decision = self.stage1.process(
                channel_name=channel_name,
                chat_id=chat_id,
                message_id=message.id,
                message_dt=msg_dt,
                raw_text=message_text,
            )

            if decision.status == "ACCEPTED":
                logger.info(f"✅ Stage 1 ACCEPTED -> stored in SSoT queue (ssot_id={decision.stored_signal_id})")
            elif decision.status == "BLOCKED":
                logger.warning(f"⛔ SIGNAL BLOCKERAD: {decision.reason}")
                try:
                    dedup = (decision.details or {}).get("dedup", {})
                    norm = (decision.details or {}).get("normalized", {})
                    ttl = getattr(config, "DUPLICATE_TTL_HOURS", 2)
                    min_diff = dedup.get("min_diff")
                    await self.app.send_message(
                        chat_id=config.PERSONAL_CHANNEL_ID,
                        text=(
                            "SIGNAL BLOCKERAD\n"
                            f"Orsak: {decision.reason}\n"
                            f"Tid: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"Gäller i: {ttl} h\n"
                            f"Avvikelse (min): {min_diff}\n"
                            f"Källa: {channel_name}\n"
                            f"Symbol: {norm.get('symbol')}\n"
                            f"Side: {norm.get('side')}\n"
                            f"Entry: {norm.get('entry_price')}\n"
                            f"SL: {norm.get('sl_price')}\n"
                            f"TP: {norm.get('tp_prices')}\n"
                            f"Message ID: {message.id}\n"
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to send SIGNAL BLOCKERAD notification: {e}")
            else:
                logger.warning(f"❌ SIGNAL OGILTIG: {decision.reason}")
                try:
                    await self.app.send_message(
                        chat_id=config.PERSONAL_CHANNEL_ID,
                        text=(
                            "SIGNAL OGILTIG\n"
                            f"Orsak: {decision.reason}\n"
                            f"Tid: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"Källa: {channel_name}\n"
                            f"Message ID: {message.id}\n"
                        ),
                    )
                except Exception as e:
                    logger.error(f"Failed to send SIGNAL OGILTIG notification: {e}")
            
        except Exception as e:
            logger.error(f"❌ Error handling message: {e}", exc_info=True)
    
    async def run(self):
        """Main run loop."""
        try:
            # Start and run Stage 0
            started = await self.start()
            
            if not started:
                logger.error("Failed to start bot - Stage 0 checks failed")
                return
            
            # Register message handler for all channels
            @self.app.on_message()
            async def message_handler(client: Client, message: Message):
                try:
                    await self.handle_new_message(client, message)
                except (PeerIdInvalid, ValueError, AttributeError):
                    pass  # Silently ignore
                except Exception as e:
                    logger.debug(f"Error processing message update: {e}")
            
            # Keep running
            logger.info("Monitoring for messages... Press Ctrl+C to stop")
            await asyncio.Event().wait()  # Wait indefinitely
            
        except KeyboardInterrupt:
            logger.info("\n🛑 Shutdown requested by user")
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}", exc_info=True)
            raise
        finally:
            await self.stop()

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

async def main():
    """Main entry point."""
    logger.info("="*60)
    logger.info("TRADING BOT - STAGE 0 IMPLEMENTATION")
    logger.info("="*60)
    logger.info(f"Telegram API ID: {config.TELEGRAM_API_ID}")
    logger.info(f"Phone: {config.TELEGRAM_PHONE_NUMBER}")
    logger.info(f"Source Channels: {len(config.SOURCE_CHANNELS)}")
    logger.info(f"Personal Channel: {config.PERSONAL_CHANNEL_ID}")
    logger.info(f"Trading Enabled: {config.ENABLE_TRADING}")
    logger.info(f"Dry Run: {config.DRY_RUN}")
    logger.info(f"Extract Signals Only: {config.EXTRACT_SIGNALS_ONLY}")
    logger.info(f"SSoT Enabled: {config.SSOT_ENABLE}")
    logger.info(f"SSoT DB Path: {config.SSOT_DB_PATH}")
    logger.info("="*60)
    
    forwarder = TelegramForwarder()
    
    try:
        await forwarder.run()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Goodbye!")
        sys.exit(0)
