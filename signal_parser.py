#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Parser
============
Parses trading signals from Telegram messages and extracts:
- Symbol
- Direction (LONG/SHORT)
- Entry price/zone
- Take-Profit targets
- Stop Loss

Author: Trading Bot Project
Date: 2026-01-08
"""

import re
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

class SignalParser:
    """Parse trading signals from Telegram messages."""
    
    def __init__(self):
        """Initialize signal parser."""
        pass
    
    def parse_signal(self, message_text: str) -> Optional[Dict]:
        """
        Parse trading signal from message text.
        
        Args:
            message_text: Telegram message text
            
        Returns:
            Parsed signal dictionary or None
        """
        # Extract symbol
        symbol = self._extract_symbol(message_text)
        if not symbol:
            logger.warning("No symbol found in signal")
            return None
        
        # Extract direction
        direction = self._extract_direction(message_text)
        if not direction:
            logger.warning("No direction found in signal")
            return None
        
        # Extract entry
        entry_data = self._extract_entry(message_text)
        
        # Extract TP targets
        tp_list = self._extract_take_profits(message_text)
        
        # Extract stop loss
        sl_price = self._extract_stop_loss(message_text)
        
        # Extract leverage (if present)
        leverage = self._extract_leverage(message_text)
        
        return {
            'symbol': symbol,
            'direction': direction,
            'entry': entry_data,
            'tp_list': tp_list,
            'sl_price': sl_price,
            'leverage': leverage,
            'original_text': message_text
        }
    
    def _extract_symbol(self, text: str) -> Optional[str]:
        """Extract trading symbol from text."""
        patterns = [
            r'#([A-Z]{2,10})(?:USDT|/USDT)?\b',
            r'\b([A-Z]{2,10})USDT\b',
            r'\b([A-Z]{2,10})/USDT\b',
            r'\b([A-Z]{2,10})\(USDT\)',
            r'(?:Symbol|COIN NAME|Asset)[:\s]+([A-Z]{2,10})(?:USDT|/USDT)?',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                symbol = match.group(1)
                if 2 <= len(symbol) <= 10 and symbol.isalpha():
                    # Normalize to USDT format
                    return f"{symbol}USDT"
        
        return None
    
    def _extract_direction(self, text: str) -> Optional[str]:
        """Extract trading direction (LONG/SHORT)."""
        # Check for explicit direction
        if re.search(r'\bLONG\b', text, re.IGNORECASE):
            return "LONG"
        elif re.search(r'\bSHORT\b', text, re.IGNORECASE):
            return "SHORT"
        elif re.search(r'\bBUY\b', text, re.IGNORECASE):
            return "LONG"
        elif re.search(r'\bSELL\b', text, re.IGNORECASE):
            return "SHORT"
        
        return None
    
    def _extract_entry(self, text: str) -> Dict:
        """Extract entry price or zone."""
        entry_patterns = [
            # Entry label variants
            r'Entry\s*(?:zone|Price|Targets?|Orders?)?\s*[:\-]?\s*\$?([\d.]+)',
            r'Entry\s*[:\-]\s*\$?([\d.]+)',
            r'Entries?\s*[:\-]?\s*\$?([\d.]+)',
            r'Entry\s+price\s*[:\-]?\s*\$?([\d.]+)',
            r'Entry\s+Orders?\s*[:\-]?\s*\$?([\d.]+)',
            # Common channel variants: Buy/Sell used as entry label
            r'\bBuy\b\s*[:\-]?\s*\$?([\d.]+)',
            r'\bSell\b\s*[:\-]?\s*\$?([\d.]+)',
        ]
        
        # Try to find entry zone (two prices)
        # Support common formats:
        # - Entry: 0.03056 - 0.03168
        # - Buy: 0.03056 - 0.03168
        # - Sell: 0.03056 - 0.03168
        zone_pattern = r'(?:Entry|Buy|Sell)\s*(?:zone|price)?\s*[:\-]?\s*\$?([\d.]+)\s*[-–]\s*\$?([\d.]+)'
        zone_match = re.search(zone_pattern, text, re.IGNORECASE)
        if zone_match:
            price1 = Decimal(zone_match.group(1))
            price2 = Decimal(zone_match.group(2))
            return {
                'type': 'zone',
                'price1': min(price1, price2),
                'price2': max(price1, price2),
                'midpoint': (price1 + price2) / 2
            }
        
        # Try to find single entry price
        for pattern in entry_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                price = Decimal(match.group(1))
                return {
                    'type': 'price',
                    'price': price
                }
        
        return {'type': 'none'}
    
    def _extract_take_profits(self, text: str) -> List[Dict]:
        """Extract take-profit targets."""
        tp_list = []
        
        # Pattern for numbered targets: TP1, TP2, etc.
        tp_pattern = r'(?:TP|Target)\s*(\d*)[:\-]?\s*\$?([\d.]+)'
        matches = re.finditer(tp_pattern, text, re.IGNORECASE)
        
        for match in matches:
            tp_num = match.group(1) or "1"
            price = Decimal(match.group(2))
            tp_list.append({
                'number': int(tp_num) if tp_num.isdigit() else len(tp_list) + 1,
                'price': price
            })
        
        # Pattern for emoji numbered targets: 1️⃣ 0.02765
        emoji_pattern = r'(\d+)[️⃣)\-]\s*\$?([\d.]+)'
        emoji_matches = re.finditer(emoji_pattern, text)
        
        for match in emoji_matches:
            tp_num = match.group(1)
            price = Decimal(match.group(2))
            tp_list.append({
                'number': int(tp_num),
                'price': price
            })
        
        # Sort by number
        tp_list.sort(key=lambda x: x['number'])
        
        return tp_list
    
    def _extract_stop_loss(self, text: str) -> Optional[Decimal]:
        """Extract stop loss price."""
        sl_patterns = [
            r'Stop[- ]?Loss\s*[:\-]?\s*\$?([\d.]+)',
            r'\bSL\b[:\-]?\s*\$?([\d.]+)',
            r'STOP\s*[:\-]?\s*\$?([\d.]+)',
            r'Stoploss\s*[:\-]?\s*\$?([\d.]+)',
        ]
        
        for pattern in sl_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return Decimal(match.group(1))
        
        return None
    
    def _extract_leverage(self, text: str) -> Optional[Decimal]:
        """Extract leverage from text."""
        leverage_patterns = [
            r'Leverage[:\-]?\s*(\d+(?:\.\d+)?)x?',
            r'(\d+(?:\.\d+)?)x\s*Leverage',
            r'LEVERAGE[:\-]?\s*(\d+(?:\.\d+)?)x?',
        ]
        
        for pattern in leverage_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return Decimal(match.group(1))
        
        return None

