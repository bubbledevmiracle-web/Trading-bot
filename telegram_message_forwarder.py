#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Message Forwarder
==========================
First Goal: Monitor Telegram group channels and forward messages to personal channel
with template transformation.

Author: Trading Bot Project
Date: 2026-01-08
"""

import asyncio
import hashlib
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Set, Optional, Tuple

# Fix for Windows Python 3.8+ event loop issue
# Must be done BEFORE importing pyrogram
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure event loop exists (for Python 3.8+)
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    RPCError,
    ChannelPrivate,
    UsernameNotOccupied,
    UserNotParticipant,
    PeerIdInvalid
)
from pyrogram.types import Message

# ============================================================================
# CONFIGURATION
# ============================================================================

# Telegram API Credentials
API_ID = 27590479
API_HASH = "6e60321cbb996b499b6a370af62342de"
PHONE_NUMBER = "+46 70 368 9310"

# Channels to Monitor (5 channels)
SOURCE_CHANNELS = {
    "CRYPTORAKETEN": "-1002290339976",
    "SMART_CRYPTO": "-1002339729195",
    "Ramos Crypto": "-1002972812097",
    "SWE Crypto": "-1002234181057",
    "Hassan tahnon": "-1003598458712"
}

# Personal Channel (destination)
PERSONAL_CHANNEL_ID = "-1003179263982"

# Dry Run Mode (set to False to actually send messages)
DRY_RUN = False   # Production: False, Test: True 

# Timezone for timestamps (Europe/Stockholm or UTC)
TIMEZONE = "Europe/Stockholm"  # Use "UTC" for UTC time

# Duplicate Detection TTL (hours)
DUPLICATE_TTL_HOURS = 2

# Logging Configuration
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "telegram_forwarder.log"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Session file
SESSION_FILE = "telegram_session"

# ============================================================================
# LOGGING SETUP
# ============================================================================

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Suppress Pyrogram dispatcher errors for unknown channels (harmless errors)
# These occur when Telegram sends updates for channels not in session cache
logging.getLogger("pyrogram.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

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
duplicate_tracker = DuplicateTracker(ttl_hours=DUPLICATE_TTL_HOURS)

# ============================================================================
# SIGNAL DETECTION ALGORITHM
# ============================================================================

def contains_trading_keywords(text: str) -> bool:
    """Check if text contains any trading-related keywords."""
    keywords = ['entry', 'target', 'tp', 'stop', 'loss', 'leverage', 'symbol', 'trade', 'long', 'short']
    return any(keyword in text.lower() for keyword in keywords)
    """Check if text contains any trading-related keywords."""
    keywords = ['entry', 'target', 'tp', 'stop', 'loss', 'leverage', 'symbol', 'trade', 'long', 'short']
    return any(keyword in text.lower() for keyword in keywords)

def should_exclude_message(text: str) -> bool:
    """
    Stage 1: Quick rejection of obvious non-signals.
    Returns True if message should be excluded.
    """
    if not text or len(text.strip()) < 10:
        return True
    
    # Hard exclusion patterns (high confidence non-signals)
    exclusion_patterns = [
        # Status/Completion indicators
        r"all\s+(entry\s+)?targets?\s+achieved",
        r"all\s+take[- ]?profit\s+targets?\s+achieved",
        r"(entry|take[- ]?profit)\s+targets?\s+achieved",
        
        # Completed trade notifications
        r"take[- ]?profit\s+target\s+\d+\s*✅",
        r"target\s+\d+\s*✅",
        r"tp\d*\s*✅",
        r"profit:\s*[\d.]+%\s*period:",
        r"profit:.*period:",
        r"achieved\s*(😎|✅|✔)",
        
        # News/Announcements
        r"^(news|update|announcement|important|notice|maintenance)\s*:",
        r"system\s+update|bug\s+fix",
    ]
    
    # Check exclusion patterns
    for pattern in exclusion_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    # Personal messages (only exclude if no trading data)
    if re.match(r'^I[\'m\s]*(ve|am|want|decided|motivated)\s+', text, re.IGNORECASE):
        # Check if it contains trading keywords - don't exclude if it has trading data
        if not contains_trading_keywords(text):
            return True
    
    return False

def detect_symbol(text: str) -> Tuple[bool, Optional[str]]:
    """
    Detect cryptocurrency symbol in message.
    Returns: (found: bool, symbol_format: str)
    """
    symbol_patterns = [
        # Hashtag formats
        (r'#([A-Z]{2,10})(?:USDT|/USDT)?\b', 'hashtag'),
        (r'#([A-Z]{2,10})\b', 'hashtag_simple'),
        
        # USDT suffix
        (r'\b([A-Z]{2,10})USDT\b', 'usdt_suffix'),
        
        # Slash notation
        (r'\b([A-Z]{2,10})/USDT\b', 'slash'),
        
        # Parentheses
        (r'\b([A-Z]{2,10})\(USDT\)', 'parentheses'),
        
        # Explicit labels
        (r'(?:Symbol|COIN NAME|Asset)[:\s]+([A-Z]{2,10})(?:USDT|/USDT)?', 'labeled'),
    ]
    
    for pattern, format_type in symbol_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            symbol = match.group(1) if match.groups() else match.group(0)
            # Validate: symbol should be 2-10 characters, alphabetic
            if symbol and 2 <= len(symbol) <= 10 and symbol.isalpha():
                return True, format_type
    
    return False, None

def detect_direction(text: str) -> Tuple[bool, Optional[str]]:
    """
    Detect trading direction (LONG/SHORT).
    Returns: (found: bool, direction: str)
    """
    # Standalone direction keywords
    direction_keywords = {
        'LONG': r'\bLONG\b',
        'SHORT': r'\bSHORT\b',
        'BUY': r'\bBUY\b',
        'SELL': r'\bSELL\b',
    }
    
    # Check standalone keywords
    for direction, pattern in direction_keywords.items():
        if re.search(pattern, text, re.IGNORECASE):
            return True, direction
    
    # Check labeled formats
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
    
    # Check context-based
    context_patterns = [
        r'Opening\s+(LONG|SHORT)',
        r'(LONG|SHORT)\s+SETUP',
        r'#(LONG|SHORT)\b',
        r'Futures.*(LONG|SHORT)',
    ]
    for pattern in context_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            direction = match.group(1).upper()
            if direction in ['LONG', 'SHORT']:
                return True, direction
    
    # Check emoji + direction pattern
    emoji_direction = [
        (r'🔴\s*SHORT', 'SHORT'),
        (r'🟢\s*LONG', 'LONG'),
        (r'📉\s*SHORT', 'SHORT'),
        (r'📈\s*LONG', 'LONG'),
    ]
    for pattern, direction in emoji_direction:
        if re.search(pattern, text, re.IGNORECASE):
            return True, direction
    
    return False, None

def detect_trading_data(text: str) -> Dict:
    """
    Detect trading data components (Entry, Targets, Stop Loss).
    Returns: {
        'has_entry': bool,
        'has_targets': bool,
        'has_stop_loss': bool,
        'entry_patterns': list,
        'target_patterns': list,
        'sl_patterns': list
    }
    """
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
        r'Entry\s+price\s*[:\-]?\s*\$?[\d.]+',
        r'ENTRY\s+PRICE\s*\([^)]+\)',
        r'Entry\s+Orders?\s*[:\-]?\s*\$?[\d.]+',
        r'Entry\s+zone\s*[:\-]?\s*[\d.]+\s*[-–]\s*[\d.]+',
    ]
    
    for pattern in entry_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_entry'] = True
            result['entry_patterns'].extend(matches)
    
    # Targets/Take-Profit patterns
    target_patterns = [
        r'Target\s*\d*[:\-]?\s*\$?[\d.]+',
        r'Targets?\s*[:\-]?\s*\$?[\d.]+',
        r'Take[- ]?Profit\s*(?:Targets?)?',
        r'\bTP\d*\b',
        r'TP\d*[:\-]?\s*[\d.]+',
        r'\d+[️⃣)\-]\s*[\d.]+',  # 1️⃣ 0.02765, 2) 0.02880
        r'target\s*\d+[:\-]?\s*[\d.$]+',
    ]
    
    for pattern in target_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_targets'] = True
            result['target_patterns'].extend(matches)
    
    # Stop Loss patterns
    sl_patterns = [
        r'Stop[- ]?Loss',
        r'\bSL\b(?!\w)',  # SL not part of other word
        r'\bSTOP\b(?!\w)',  # STOP not part of other word (in trading context)
        r'Stoploss',
        r'Stop\s+loss\s*[:\-]?\s*[\d.$]+',
        r'SL[:\-]\s*[\d.]+',
        r'STOP\s*[:\-]\s*[\d.$]+',
        r'Stop[- ]?Loss\s*[:\-]?\s*[\d.$-]+',
        r'Stop[- ]?Loss\s*[:\-]?\s*[\d.]+%',
        r'Stop\s+Targets?',  # Stop Targets
    ]
    
    for pattern in sl_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result['has_stop_loss'] = True
            result['sl_patterns'].extend(matches)
    
    return result

def validate_signal(text: str, symbol_found: bool, direction_found: bool, trading_data: Dict) -> Tuple[bool, int, str]:
    """
    Validate if message is a trading signal with confidence scoring.
    Returns: (is_signal: bool, confidence_score: int, reason: str)
    """
    score = 0
    reasons = []
    
    # Required components check
    if not symbol_found:
        return False, 0, "Missing symbol"
    
    if not direction_found:
        return False, 0, "Missing direction"
    
    # Score required components
    score += 4  # Symbol found
    reasons.append("has_symbol")
    
    score += 3  # Direction found
    reasons.append("has_direction")
    
    # Score trading data
    if trading_data['has_entry']:
        score += 3
        reasons.append("has_entry")
    
    if trading_data['has_targets']:
        score += 2
        reasons.append("has_targets")
        # Bonus for multiple targets
        if len(trading_data['target_patterns']) > 1:
            score += 1
            reasons.append("multiple_targets")
    
    if trading_data['has_stop_loss']:
        score += 2
        reasons.append("has_stop_loss")
    
    # Check for leverage
    if re.search(r'Leverage|X\d+|x\d+|\d+x', text, re.IGNORECASE):
        score += 1
        reasons.append("has_leverage")
    
    # Check for price numbers (validation)
    price_numbers = re.findall(r'\b\d+\.\d+\b|\b\d{4,}\b', text)  # Prices or large numbers
    if len(price_numbers) >= 3:  # At least 3 price-like numbers
        score += 1
        reasons.append("has_price_data")
    
    # Negative scoring (double-check exclusion - shouldn't happen if exclusion worked)
    if re.search(r'achieved|target \d+ ✅|profit:.*period:', text, re.IGNORECASE):
        score -= 10
        reasons.append("exclusion_triggered")
        return False, score, "Contains exclusion keywords"
    
    # Minimum trading data check
    has_trading_data = trading_data['has_entry'] or trading_data['has_targets'] or trading_data['has_stop_loss']
    if not has_trading_data:
        return False, score, "Missing trading data (Entry/TP/SL)"
    
    # Decision based on confidence score
    if score >= 8:
        return True, score, f"High confidence ({', '.join(reasons)})"
    elif score >= 5:
        return True, score, f"Medium confidence ({', '.join(reasons)})"
    elif score >= 3:
        # Low confidence - still forward but log
        return True, score, f"Low confidence ({', '.join(reasons)})"
    else:
        return False, score, "Insufficient signal components"

def is_trading_signal(message_text: str) -> Tuple[bool, str]:
    """
    Main algorithm: Determine if message is a trading signal.
    Implements three-stage pipeline: Exclusion -> Detection -> Validation
    
    Returns: (is_signal: bool, reason: str)
    """
    # Stage 1: Pre-Processing & Quick Rejection
    if should_exclude_message(message_text):
        return False, "Excluded by hard exclusion rules"
    
    # Stage 2: Core Signal Detection
    symbol_found, symbol_format = detect_symbol(message_text)
    direction_found, direction = detect_direction(message_text)
    trading_data = detect_trading_data(message_text)
    
    # Stage 3: Validation & Confidence Scoring
    is_signal, confidence_score, reason = validate_signal(
        message_text, 
        symbol_found, 
        direction_found, 
        trading_data
    )
    
    return is_signal, f"{reason} (confidence: {confidence_score})"

# ============================================================================
# TEMPLATE FORMATTING
# ============================================================================

def format_timestamp(timezone: str = "Europe/Stockholm") -> str:
    """Format current timestamp for template."""
    now = datetime.now()
    
    if timezone == "Europe/Stockholm":
        # Note: For production, use pytz for proper timezone handling
        # This is a simplified version
        try:
            from datetime import timezone, timedelta
            stockholm_tz = timezone(timedelta(hours=1))  # CET (simplified)
            now = now.astimezone(stockholm_tz)
        except Exception:
            pass
    
    return now.strftime("%Y-%m-%d %H:%M:%S")

def format_template_message(
    channel_name: str,
    original_text: str,
    timestamp: Optional[str] = None
) -> str:
    """
    Format message according to 'Signal received & copied' template.
    
    Template format:
    ✅ Signal mottagen & kopierad
    🕒 Tid: {{tid}}
    📢 Från kanal: {{källa}}
    📊 Meddelande:
    {{original_message_text}}
    """
    if timestamp is None:
        timestamp = format_timestamp(TIMEZONE)
    
    template = (
        "✅ Signal mottagen & kopierad\n"
        f"🕒 Tid: {timestamp}\n"
        f"📢 Från kanal: {channel_name}\n"
        f"📊 Meddelande:\n"
        f"{original_text}"
    )
    
    return template

# ============================================================================
# CHANNEL NAME RESOLUTION
# ============================================================================

def get_channel_display_name(channel_id: str, channel_username: Optional[str]) -> str:
    """Get display name for channel."""
    # Check if we have a predefined name
    for name, id_or_username in SOURCE_CHANNELS.items():
        if channel_id == id_or_username or channel_username == id_or_username:
            return name
    
    # Fallback to username or ID
    if channel_username:
        return channel_username.replace("@", "")
    return f"Channel {channel_id}"

# ============================================================================
# TELEGRAM CLIENT & HANDLERS
# ============================================================================

class TelegramForwarder:
    """Main Telegram message forwarder class."""
    
    def __init__(self):
        self.app = Client(
            SESSION_FILE,
            api_id=API_ID,
            api_hash=API_HASH,
            phone_number=PHONE_NUMBER
        )
        self.personal_channel_id = PERSONAL_CHANNEL_ID
        self.source_channels = SOURCE_CHANNELS
        
    async def start(self):
        """Start the Telegram client."""
        logger.info("Starting Telegram client...")
        await self.app.start()
        logger.info("✅ Telegram client started successfully")
        
        # Verify personal channel access
        # Note: Channel may not be in session cache yet, so we'll verify it's accessible
        # by trying to send a test message (in dry-run) or just logging a warning
        personal_chat = None
        channel_id_to_try = self.personal_channel_id
        
        # Try to get channel info - if it fails, we'll continue anyway and try when sending
        try:
            # Try as string (with negative sign if present)
            personal_chat = await self.app.get_chat(channel_id_to_try)
            logger.info(f"✅ Personal channel verified: {personal_chat.title or channel_id_to_try}")
        except (PeerIdInvalid, ChannelPrivate) as e:
            # Channel not in session cache yet - this is OK, we'll verify when sending
            logger.warning(f"⚠️  Personal channel {channel_id_to_try} not found in session cache")
            logger.warning("   This is normal if you haven't accessed the channel recently.")
            logger.warning("   The channel will be accessed automatically when sending first message.")
            logger.warning("   If you get errors when sending, try:")
            logger.warning("   1. Open Telegram app and go to your personal channel")
            logger.warning("   2. Send a test message in the channel")
            logger.warning("   3. Make sure you are an admin/member of the channel")
            # Don't raise - continue anyway, we'll verify when actually sending
        except Exception as e:
            logger.warning(f"⚠️  Could not verify personal channel {channel_id_to_try}: {e}")
            logger.warning("   Will attempt to access when sending first message.")
            # Don't raise - continue anyway
        
        # Verify source channels access
        logger.info("Verifying source channels...")
        for name, channel_id in self.source_channels.items():
            try:
                chat = await self.app.get_chat(channel_id)
                logger.info(f"✅ {name}: {chat.title or channel_id}")
            except (PeerIdInvalid, ChannelPrivate):
                # Channel not in session cache yet - this is OK for private channels
                logger.warning(f"⚠️  {name} ({channel_id}): Not in session cache yet")
                logger.warning(f"   This is normal for private channels. Channel will be accessed when first message arrives.")
                logger.warning(f"   To verify access: Open Telegram app and visit this channel, then restart the script.")
            except UsernameNotOccupied:
                logger.warning(f"⚠️  {name} ({channel_id}): Username not found")
            except UserNotParticipant:
                logger.warning(f"⚠️  {name} ({channel_id}): Not a participant - ensure you're a member")
            except Exception as e:
                logger.warning(f"⚠️  {name} ({channel_id}): {e}")
        
        logger.info("\n" + "="*60)
        logger.info("🚀 Bot is running and monitoring channels...")
        logger.info(f"📊 Dry Run Mode: {'ENABLED' if DRY_RUN else 'DISABLED'}")
        logger.info("="*60 + "\n")
    
    async def stop(self):
        """Stop the Telegram client."""
        logger.info("Stopping Telegram client...")
        await self.app.stop()
        logger.info("✅ Telegram client stopped")
    
    async def handle_new_message(self, client: Client, message: Message):
        """Handle new message from source channels."""
        try:
            # Skip if message is None or invalid
            if not message or not hasattr(message, 'chat'):
                return
            
            # Skip non-text messages for now
            if not message.text:
                return
            
            # Get channel information - handle cases where chat might not be accessible
            try:
                chat_id = str(message.chat.id)
                chat_username = message.chat.username
            except AttributeError:
                # Chat info not available, skip
                return
            
            # Check if message is from a monitored channel
            channel_name = None
            for name, id_or_username in self.source_channels.items():
                if chat_id == id_or_username or (chat_username and f"@{chat_username}" == id_or_username):
                    channel_name = name
                    break
            
            if not channel_name:
                # Try to match by username
                if chat_username:
                    for name, id_or_username in self.source_channels.items():
                        if id_or_username.startswith("@") and id_or_username == f"@{chat_username}":
                            channel_name = name
                            break
            
            if not channel_name:
                return  # Not from a monitored channel
            
            message_text = message.text
            
            # Signal Detection: Check if message is a trading signal
            is_signal, signal_reason = is_trading_signal(message_text)
            if not is_signal:
                logger.debug(f"⏭️  Non-signal message from {channel_name}: {signal_reason}")
                return  # Skip non-signal messages (news, updates, personal messages, etc.)
            
            logger.info(f"✅ Signal detected from {channel_name}: {signal_reason}")
            
            # Check for duplicates
            if duplicate_tracker.is_duplicate(chat_id, message.id, message_text):
                logger.debug(f"Duplicate signal from {channel_name}, skipping")
                return
            
            logger.info(f"📨 New signal from {channel_name}")
            logger.debug(f"Signal text: {message_text[:200]}...")
            
            # Format message with template
            formatted_message = format_template_message(
                channel_name=channel_name,
                original_text=message_text,
                timestamp=format_timestamp(TIMEZONE)
            )
            
            # Send to personal channel
            if DRY_RUN:
                logger.info("🔍 [DRY RUN] Would send message:")
                logger.info(f"\n{formatted_message}\n")
            else:
                try:
                    # Try sending - this will also "warm up" the channel if not in cache
                    await client.send_message(
                        chat_id=self.personal_channel_id,
                        text=formatted_message
                    )
                    logger.info(f"✅ Message forwarded to personal channel")
                except PeerIdInvalid:
                    logger.error(f"❌ Personal channel {self.personal_channel_id} is invalid or not accessible")
                    logger.error("   SOLUTION:")
                    logger.error("   1. Open Telegram app and go to your personal channel")
                    logger.error("   2. Send a test message in the channel")
                    logger.error("   3. Make sure you are an admin/member of the channel")
                    logger.error("   4. Verify the channel ID is correct")
                    logger.error("   Message NOT forwarded - please fix channel access and try again")
                except ChannelPrivate:
                    logger.error(f"❌ Personal channel {self.personal_channel_id} is private")
                    logger.error("   Make sure you are a member/admin of the channel")
                    logger.error("   Message NOT forwarded")
                except FloodWait as e:
                    logger.warning(f"⏳ Rate limit: waiting {e.value} seconds")
                    await asyncio.sleep(e.value)
                    # Retry after wait
                    try:
                        await client.send_message(
                            chat_id=self.personal_channel_id,
                            text=formatted_message
                        )
                        logger.info(f"✅ Message forwarded after rate limit wait")
                    except Exception as retry_e:
                        logger.error(f"❌ Failed to send message after retry: {retry_e}")
                except Exception as e:
                    logger.error(f"❌ Failed to send message: {e}")
                    logger.error("   Message NOT forwarded - check channel access and permissions")
        
        except Exception as e:
            logger.error(f"❌ Error handling message: {e}", exc_info=True)
    
    async def run(self):
        """Main run loop."""
        try:
            await self.start()
            
            # Register message handler for all channels
            # Filter to only process messages from channels we monitor
            @self.app.on_message()
            async def message_handler(client: Client, message: Message):
                try:
                    await self.handle_new_message(client, message)
                except (PeerIdInvalid, ValueError, AttributeError) as e:
                    # Suppress errors from channels we don't monitor
                    # These are internal Pyrogram update handling errors from unknown channels
                    # Telegram sends updates for all channels user is subscribed to, not just monitored ones
                    pass  # Silently ignore - these are expected
                except Exception as e:
                    # Log other errors but don't crash
                    logger.debug(f"Error processing message update: {e}")
            
            # Suppress asyncio task exception warnings for unknown channels
            # These are expected when Telegram sends updates for channels not in session cache
            import warnings
            warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*Task exception was never retrieved.*")
            
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
    logger.info("Telegram Message Forwarder - First Goal")
    logger.info("="*60)
    logger.info(f"API ID: {API_ID}")
    logger.info(f"Phone: {PHONE_NUMBER}")
    logger.info(f"Source Channels: {len(SOURCE_CHANNELS)}")
    logger.info(f"Personal Channel: {PERSONAL_CHANNEL_ID}")
    logger.info(f"Dry Run: {DRY_RUN}")
    logger.info(f"Timezone: {TIMEZONE}")
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

