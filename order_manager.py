#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Order Manager
=============
Manages order lifecycle:
- Dual-limit entry with merging
- TP/SL placement
- Order cleanup with timeouts
- Position tracking

Author: Trading Bot Project
Date: 2026-01-08
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import uuid4

from bingx_client import BingXClient

logger = logging.getLogger(__name__)

class OrderManager:
    """Manage trading orders and positions."""
    
    def __init__(self, bingx_client: BingXClient):
        """
        Initialize order manager.
        
        Args:
            bingx_client: BingX client instance
        """
        self.client = bingx_client
        self.active_orders: Dict[str, Dict] = {}
        self.active_positions: Dict[str, Dict] = {}
        
        # Order cleanup timeouts
        self.timeout_short = timedelta(hours=24)
        self.timeout_long = timedelta(days=6)
    
    def process_signal(self, parsed_signal: Dict, source_channel: str, message_id: int) -> Dict:
        """
        Process a trading signal and place orders.
        
        Args:
            parsed_signal: Parsed signal dictionary
            source_channel: Source channel name
            message_id: Telegram message ID
            
        Returns:
            Order placement result dictionary
        """
        bot_order_id = str(uuid4())
        
        # Get symbol info
        symbol = parsed_signal['symbol']
        symbol_info = self.client.get_symbol_info(symbol)
        if not symbol_info:
            return {
                'success': False,
                'error': f'Symbol {symbol} not found on BingX',
                'bot_order_id': bot_order_id
            }
        
        # Calculate entry
        entry_data = parsed_signal['entry']
        if entry_data['type'] == 'zone':
            target_entry = entry_data['midpoint']
            spread = (entry_data['price2'] - entry_data['price1']) / 2
        elif entry_data['type'] == 'price':
            target_entry = entry_data['price']
            # Default spread: 0.1% of entry price
            spread = target_entry * Decimal('0.001')
        else:
            return {
                'success': False,
                'error': 'No entry price found in signal',
                'bot_order_id': bot_order_id
            }
        
        # Handle stop loss
        sl_price = parsed_signal.get('sl_price')
        direction = parsed_signal.get('direction')  # LONG | SHORT (already normalized upstream)
        if not sl_price:
            # FAST fallback (deterministic): side-correct SL + fixed x10 leverage
            position_data = self.client.calculate_fast_fallback(target_entry, direction)
            sl_price = position_data['sl_price']
        else:
            # Dynamic position sizing & leverage
            position_data = self.client.calculate_position_size(target_entry, sl_price)

        leverage = position_data['leverage']
        leverage_class = position_data['leverage_class']
        total_quantity = position_data['quantity']
        
        # Get symbol info for quantization
        tick_size = Decimal(str(symbol_info.get('tickSize', '0.0001')))
        qty_step = Decimal(str(symbol_info.get('lotSizeFilter', {}).get('qtyStep', '0.001')))
        min_qty = Decimal(str(symbol_info.get('lotSizeFilter', {}).get('minQty', '0.001')))
        
        # Quantize quantity
        quantity = self.client._quantize_quantity(total_quantity, qty_step, min_qty)
        # Keep SSoT-consistent: store the exact quantity we actually place.
        position_data['quantity'] = quantity
        
        # Place dual-limit orders
        side = "BUY" if parsed_signal['direction'] == "LONG" else "SELL"
        
        dual_limit_result = self.client.place_dual_limit_orders(
            symbol=symbol,
            side=side,
            target_entry=target_entry,
            spread=spread,
            total_quantity=quantity,
            leverage=leverage,
            symbol_info=symbol_info
        )
        
        if not dual_limit_result.get('order_ids'):
            return {
                'success': False,
                'error': 'Failed to place dual-limit orders',
                'bot_order_id': bot_order_id
            }
        
        # Store order information
        order_info = {
            'bot_order_id': bot_order_id,
            'symbol': symbol,
            'side': side,
            'entry_price': target_entry,
            'sl_price': sl_price,
            'leverage': leverage,
            'leverage_class': leverage_class,
            'quantity': quantity,
            'order_ids': dual_limit_result['order_ids'],
            'source_channel': source_channel,
            'message_id': message_id,
            'created_at': datetime.now(),
            'position_data': position_data,
            'tp_list': parsed_signal.get('tp_list', [])
        }
        
        self.active_orders[bot_order_id] = order_info
        
        return {
            'success': True,
            'bot_order_id': bot_order_id,
            'order_ids': dual_limit_result['order_ids'],
            'order_info': order_info
        }
    
    def check_order_fills(self, bot_order_id: str) -> Dict:
        """
        Check if orders have been filled and handle merging.
        
        Args:
            bot_order_id: Bot order ID
            
        Returns:
            Fill status dictionary
        """
        if bot_order_id not in self.active_orders:
            return {'status': 'not_found'}
        
        order_info = self.active_orders[bot_order_id]
        symbol = order_info['symbol']
        
        # Check status of both orders
        fills = []
        total_filled = Decimal('0')
        total_filled_value = Decimal('0')
        
        for order_id in order_info['order_ids']:
            order_status = self.client.get_order_status(symbol, order_id)
            if order_status:
                executed_qty = Decimal(str(order_status.get('executedQty', '0')))
                avg_price = Decimal(str(order_status.get('avgPrice', '0')))
                
                if executed_qty > 0:
                    fills.append({
                        'order_id': order_id,
                        'executed_qty': executed_qty,
                        'avg_price': avg_price
                    })
                    total_filled += executed_qty
                    total_filled_value += executed_qty * avg_price
        
        if total_filled == 0:
            return {'status': 'pending', 'filled': False}
        
        # Check if we need to merge
        target_quantity = order_info['quantity']
        if total_filled < target_quantity:
            # Partial fill - need to merge
            remaining_qty = target_quantity - total_filled
            target_entry = order_info['entry_price']
            
            # Calculate replacement price
            replacement_price = (target_entry * target_quantity - total_filled_value) / remaining_qty
            
            # Cancel unfilled orders and place replacement
            # (Implementation details for merging logic)
            return {
                'status': 'partial_fill',
                'filled_qty': total_filled,
                'remaining_qty': remaining_qty,
                'replacement_price': replacement_price
            }
        else:
            # Fully filled
            return {
                'status': 'filled',
                'filled_qty': total_filled,
                'avg_price': total_filled_value / total_filled
            }
    
    def cleanup_old_orders(self):
        """Clean up orders based on timeout rules."""
        now = datetime.now()
        
        for bot_order_id, order_info in list(self.active_orders.items()):
            age = now - order_info['created_at']
            
            # Short timeout: 24h for hanging orders
            if age >= self.timeout_short:
                # Check if orders are still pending
                symbol = order_info['symbol']
                all_cancelled = True
                
                for order_id in order_info['order_ids']:
                    order_status = self.client.get_order_status(symbol, order_id)
                    if order_status and order_status.get('status') in ['NEW', 'PARTIALLY_FILLED']:
                        # Cancel the order
                        if self.client.cancel_order(symbol, order_id):
                            all_cancelled = True
                        else:
                            all_cancelled = False
                
                if all_cancelled:
                    logger.info(f"Cleaned up order {bot_order_id} (24h timeout)")
                    del self.active_orders[bot_order_id]
            
            # Long timeout: 6d for unfilled orders
            elif age >= self.timeout_long:
                # Cancel all orders
                symbol = order_info['symbol']
                for order_id in order_info['order_ids']:
                    self.client.cancel_order(symbol, order_id)
                
                logger.info(f"Cleaned up order {bot_order_id} (6d timeout)")
                del self.active_orders[bot_order_id]

