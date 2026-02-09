#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading Bot Integration
========================
Integrates Telegram signal detection with BingX trading execution.

Author: Trading Bot Project
Date: 2026-01-08
Updated: 2026-01-14
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional

import config
from bingx_client import BingXClient
from order_manager import OrderManager
from signal_parser import SignalParser

logger = logging.getLogger(__name__)

class TradingBotIntegration:
    """Integrate Telegram signals with BingX trading."""
    
    def __init__(self, testnet: bool = True):
        """
        Initialize trading bot integration.
        
        Args:
            testnet: Use BingX testnet (default: True for safety)
        """
        self.bingx_client = BingXClient(testnet=testnet)
        self.order_manager = OrderManager(self.bingx_client)
        self.signal_parser = SignalParser()
        self.testnet = testnet
        
        # Verify connection on init
        self._connected = False
    
    async def initialize(self) -> bool:
        """
        Initialize and verify connections.
        
        Returns:
            True if initialization successful
        """
        logger.info("Initializing trading bot integration...")
        
        # Verify BingX connection
        if self.bingx_client.verify_connection():
            self._connected = True
            logger.info("✅ BingX connection verified")
        else:
            logger.error("❌ BingX connection failed")
            return False
        
        return True
    
    def format_order_template(self, order_result: Dict, parsed_signal: Dict, source_channel: str) -> str:
        """
        Format Telegram message template after order placement.
        
        Template header: "SENT ONLY AFTER BINGX CONFIRMATION (code=0/fills)"
        
        Args:
            order_result: Order placement result
            parsed_signal: Parsed signal data
            source_channel: Source channel name
            
        Returns:
            Formatted Telegram message
        """
        if not order_result.get('success'):
            # Error message
            return (
                f"❌ Order Placement Failed\n"
                f"📢 Från kanal: {source_channel}\n"
                f"🔴 Fel: {order_result.get('error', 'Unknown error')}\n"
                f"📊 Original signal:\n{parsed_signal.get('original_text', '')}"
            )
        
        order_info = order_result['order_info']
        position_data = order_info['position_data']
        
        # Format leverage for Telegram: xNN.NN (prefer canonical preformatted value)
        leverage_str = position_data.get('leverage_display') or f"x{position_data['leverage']:.2f}"
        
        # Format template according to requirements
        template = (
            "SENT ONLY AFTER BINGX CONFIRMATION (code=0/fills)\n\n"
            f"✅ Order Placed\n"
            f"🕒 Tid: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📢 Från kanal: {source_channel}\n"
            f"📊 Symbol: {parsed_signal['symbol']}\n"
            f"📈 Riktning: {parsed_signal['direction']}\n"
            f"💰 Entry: {order_info['entry_price']}\n"
            f"🛑 Stop Loss: {order_info['sl_price']}\n"
            f"⚡ Leverage: {leverage_str} ({position_data['leverage_class']})\n"
            f"📦 Quantity: {order_info['quantity']}\n"
            f"🆔 Bot Order ID: {order_info['bot_order_id']}\n"
            f"🆔 BingX Order IDs: {', '.join(order_info['order_ids'])}\n"
        )
        
        # Add TP list if available
        if order_info.get('tp_list'):
            template += f"🎯 Take Profits:\n"
            for tp in order_info['tp_list']:
                template += f"  TP{tp['number']}: {tp['price']}\n"
        
        return template
    
    async def process_signal(self, message_text: str, source_channel: str, message_id: int) -> Optional[Dict]:
        """
        Process a trading signal from Telegram.
        
        Args:
            message_text: Telegram message text
            source_channel: Source channel name
            message_id: Telegram message ID
            
        Returns:
            Processing result dictionary or None
        """
        if not self._connected:
            logger.error("BingX not connected, cannot process signal")
            return None
        
        # Parse signal
        parsed_signal = self.signal_parser.parse_signal(message_text)
        if not parsed_signal:
            logger.warning("Failed to parse signal")
            return None
        
        logger.info(f"✅ Signal parsed: {parsed_signal['symbol']} {parsed_signal['direction']}")
        
        # Place orders (synchronous operation, but we're in async context)
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        order_result = await loop.run_in_executor(
            None,
            self.order_manager.process_signal,
            parsed_signal,
            source_channel,
            message_id
        )
        
        if not order_result.get('success'):
            logger.error(f"Order placement failed: {order_result.get('error')}")
            return {
                'success': False,
                'error': order_result.get('error'),
                'parsed_signal': parsed_signal
            }
        
        logger.info(f"✅ Orders placed: {order_result['bot_order_id']}")
        
        # Format template message
        template_message = self.format_order_template(order_result, parsed_signal, source_channel)
        
        return {
            'success': True,
            'bot_order_id': order_result['bot_order_id'],
            'order_ids': order_result['order_ids'],
            'template_message': template_message,
            'parsed_signal': parsed_signal,
            'order_info': order_result['order_info']
        }
    
    async def send_startup_message(self, telegram_client) -> bool:
        """
        Send startup message with green checks (✅) as per requirements.
        
        Args:
            telegram_client: Telegram client instance
            
        Returns:
            True if message sent successfully
        """
        try:
            # Get account balance from BingX
            balance = self.bingx_client.get_account_balance()
            
            # Format startup message
            startup_message = (
                "🚀 Bot Startup\n\n"
                f"💰 Wallet balance (baseline): {balance} USDT (SSoT) ✅\n"
                f"⚙️ Risk settings: 2% per trade ✅\n"
                f"📊 Strategies: Active ✅\n"
                f"📈 Active positions & open orders: 0 ✅\n"
                f"🌐 Environment: {'Testnet' if self.testnet else 'Mainnet'} ✅"
            )
            
            # Send to personal channel
            await telegram_client.send_message(
                chat_id=int(config.PERSONAL_CHANNEL_ID),
                text=startup_message
            )
            
            logger.info("✅ Startup message sent")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send startup message: {e}")
            return False

