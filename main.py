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
# SIGNAL EXTRACTION LOGGER
# ============================================================================

# Create separate logger for extracted signals
signal_logger = logging.getLogger("signal_extractor")
signal_logger.setLevel(logging.INFO)
signal_handler = logging.FileHandler(config.EXTRACT_SIGNALS_LOG, encoding='utf-8')
signal_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
signal_logger.addHandler(signal_handler)

# ============================================================================
# DUPLICATE DETECTION
# ============================================================================

class DuplicateTracker:
    """Track processed messages to prevent duplicates."""
    
    def __init__(self, ttl_hours: int = 2):
        self.ttl_hours = ttl_hours
        self.processed_messages: Dict[str, datetime] = {}
    
    def _calculate_hash(self, channel_id: str, message_id: int, text: str) -> str:
        """Calculate hash for message duplicate detection."""
        content = f"{channel_id}:{message_id}:{text[:100]}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def is_duplicate(self, channel_id: str, message_id: int, text: str) -> bool:
        """Check if message is duplicate."""
        msg_hash = self._calculate_hash(channel_id, message_id, text)
        now = datetime.now()
        
        # Clean old entries
        self.processed_messages = {
            k: v for k, v in self.processed_messages.items()
            if now - v < timedelta(hours=self.ttl_hours)
        }
        
        if msg_hash in self.processed_messages:
            logger.debug(f"Duplicate detected: {msg_hash}")
            return True
        
        # Mark as processed
        self.processed_messages[msg_hash] = now
        return False

# Global duplicate tracker
duplicate_tracker = DuplicateTracker(ttl_hours=config.DUPLICATE_TTL_HOURS)

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
        r'Entry\s*(?:zone|Price|Targets?|Orders?)?\s*[:\-]?\s*\$?[\d.]+',
        r'Entry\s*[:\-]\s*\$?[\d.]+',
        r'Entries?\s*[:\-]?\s*\$?[\d.]+',
    ]
    
    for pattern in entry_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_entry'] = True
            result['entry_patterns'].extend(matches)
    
    # Target patterns
    target_patterns = [
        r'Target\s*\d*[:\-]?\s*\$?[\d.]+',
        r'Targets?\s*[:\-]?\s*\$?[\d.]+',
        r'Take[- ]?Profit\s*(?:Targets?)?',
        r'\bTP\d*\b',
    ]
    
    for pattern in target_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_targets'] = True
            result['target_patterns'].extend(matches)
    
    # Stop Loss patterns
    sl_patterns = [
        r'Stop[- ]?Loss',
        r'\bSL\b(?!\w)',
        r'\bSTOP\b(?!\w)',
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
    
    if trading_data['has_entry']:
        score += 3
        reasons.append("has_entry")
    
    if trading_data['has_targets']:
        score += 2
        reasons.append("has_targets")
    
    if trading_data['has_stop_loss']:
        score += 2
        reasons.append("has_stop_loss")
    
    has_trading_data = trading_data['has_entry'] or trading_data['has_targets'] or trading_data['has_stop_loss']
    if not has_trading_data:
        return False, score, "Missing trading data (Entry/TP/SL)"
    
    if score >= 3:
        return True, score, f"Signal detected ({', '.join(reasons)})"
    else:
        return False, score, "Insufficient signal components"

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
    
    async def start(self):
        """Start the Telegram client and run Stage 0 checks."""
        logger.info("Starting Telegram client...")
        await self.app.start()
        logger.info("✅ Telegram client started successfully")
        
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
        logger.info(f"📂 Signal Log: {config.EXTRACT_SIGNALS_LOG}")
        logger.info("="*60 + "\n")
        
        return True
    
    async def stop(self):
        """Stop the Telegram client."""
        logger.info("Stopping Telegram client...")
        await self.app.stop()
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
            
            # Check for duplicates
            if duplicate_tracker.is_duplicate(chat_id, message.id, message_text):
                logger.debug(f"Duplicate signal from {channel_name}, skipping")
                return
            
            # Log extracted signal to file
            signal_logger.info(f"\n{'='*80}")
            signal_logger.info(f"SIGNAL EXTRACTED")
            signal_logger.info(f"Channel: {channel_name}")
            signal_logger.info(f"Message ID: {message.id}")
            signal_logger.info(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            signal_logger.info(f"Reason: {signal_reason}")
            signal_logger.info(f"{'='*80}")
            signal_logger.info(f"{message_text}")
            signal_logger.info(f"{'='*80}\n")
            
            logger.info(f"📝 Signal logged to: {config.EXTRACT_SIGNALS_LOG}")
            
            # During testing phase, we only extract and log signals
            # We do NOT forward to private channel or place trades
            # This allows signal processing logic to be verified first
            
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
