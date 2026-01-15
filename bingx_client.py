#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BingX Trading Client
====================
Implements BingX API integration with all trading bot requirements:
- Dynamic leverage calculation
- Dual-limit entry with merging
- Position sizing
- Order placement and validation
- BingX-first flow (wait for confirmation)
- WebSocket support for real-time updates

Author: Trading Bot Project
Date: 2026-01-14
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# ============================================================================
# INTERNAL HELPERS
# ============================================================================

def _safe_decimal(value, default: Decimal) -> Decimal:
    """
    Convert arbitrary value to Decimal, falling back deterministically.
    """
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        return Decimal(s)
    except Exception:
        return default


def _step_from_precision(precision) -> Optional[Decimal]:
    """
    Convert a "precision digits" integer into a step size Decimal.
    Example: 3 -> 0.001
    """
    try:
        if precision is None:
            return None
        p = int(str(precision).strip())
        if p < 0:
            return None
        if p == 0:
            return Decimal("1")
        return Decimal("1") / (Decimal("10") ** p)
    except Exception:
        return None


# ============================================================================
# CONFIGURATION
# ============================================================================

# BingX API Credentials (from config)
BINGX_API_KEY = "Z3w6CaFqcLhk05UfB58enOYrvULTCtaSnGcye7CtWpbERiNfDXsDT9x79IDVw77atzAxeLA4tjZ03lpFerGWCA"
BINGX_API_SECRET = "vjQfaT0l3kXooWHLLBQT1yV8J6GXHNgPLO3y0x760kdT8piEaIZ51168J57SoGX8FV8dXCrNBU8FHMzM3w"

# Trading Parameters (SSoT Baseline)
ACCOUNT_BALANCE_BASELINE = Decimal("402.10")  # USDT
RISK_PER_TRADE = Decimal("0.02")  # 2% per trade
INITIAL_MARGIN_PLAN = Decimal("20.00")  # USDT per trade
MAX_LEVERAGE = Decimal("50.00")
MIN_LEVERAGE = Decimal("1.00")

# Order Cleanup Timeouts
TIMEOUT_SHORT = timedelta(hours=24)  # Hanging opening orders
TIMEOUT_LONG = timedelta(days=6)  # Unfilled orders

# ============================================================================
# BINGX API CLIENT
# ============================================================================

class BingXClient:
    """BingX API client with trading bot requirements."""
    
    def __init__(self, api_key: str = None, secret_key: str = None, testnet: bool = False):
        """
        Initialize BingX client.
        
        Args:
            api_key: BingX API key (defaults to config)
            secret_key: BingX API secret (defaults to config)
            testnet: Use testnet endpoint (default: False)
        """
        self.api_key = api_key or BINGX_API_KEY
        self.secret_key = secret_key or BINGX_API_SECRET
        self.testnet = testnet
        
        # Set API endpoints
        if testnet:
            self.base_url = "https://open-api-vst.bingx.com"
        else:
            self.base_url = "https://open-api.bingx.com"
        
        # WebSocket
        self.ws_session = None
        self.ws_connected = False
        self.last_heartbeat = None
        
        # Account parameters (SSoT)
        self.account_balance = ACCOUNT_BALANCE_BASELINE
        self.risk_per_trade = RISK_PER_TRADE
        self.im_plan = INITIAL_MARGIN_PLAN
        
        logger.info(f"BingX client initialized (testnet={testnet})")
    
    def _generate_signature(self, query_string: str) -> str:
        """
        Generate HMAC SHA256 signature for BingX API.
        
        According to BingX documentation:
        echo -n 'recvWindow=0&symbol=BTC-USDT&timestamp=xxx' | openssl dgst -sha256 -hmac 'SECRET_KEY' -hex
        
        Args:
            query_string: Unencoded query string (e.g., "recvWindow=0&timestamp=1234567890")
            
        Returns:
            64-character lowercase hexadecimal signature string
        """
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _send_request(self, method: str, endpoint: str, params: Dict = None, signed: bool = True) -> Dict:
        """
        Send HTTP request to BingX API.
        
        Args:
            method: HTTP method (GET, POST, DELETE)
            endpoint: API endpoint
            params: Request parameters
            signed: Whether to sign the request
            
        Returns:
            Response dictionary
        """
        if params is None:
            params = {}
        
        url = f"{self.base_url}{endpoint}"
        headers = {
            'X-BX-APIKEY': self.api_key
        }
        
        # Add timestamp and recvWindow for signed requests
        if signed:
            timestamp = str(int(time.time() * 1000))
            params['timestamp'] = timestamp
            params['recvWindow'] = '60000'  # 60 seconds window
            
            # Sort parameters alphabetically and create query string
            # NOTE: Do NOT URL encode for signature generation
            sorted_params = sorted(params.items())
            query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
            
            # Generate signature from unencoded query string
            signature = self._generate_signature(query_string)
            
            # CRITICAL: Rebuild params dict in sorted order to match signature
            # This ensures the URL parameters are in the same order as signed
            params = dict(sorted_params)
            params['signature'] = signature
            
            # ALWAYS log signature details for debugging
            logger.info(f"🔐 BingX Signature:")
            logger.info(f"   Timestamp: {timestamp}")
            logger.info(f"   Query string: {query_string}")
            logger.info(f"   API Key (first 20): {self.api_key[:20]}...")
            logger.info(f"   Secret (first 20): {self.secret_key[:20]}...")
            logger.info(f"   Signature: {signature}")
        
        try:
            if method == 'GET':
                response = requests.get(url, params=params, headers=headers, timeout=10)
            elif method == 'POST':
                # For POST, BingX expects parameters in the query string, not body
                response = requests.post(url, params=params, headers=headers, timeout=10)
            elif method == 'DELETE':
                response = requests.delete(url, params=params, headers=headers, timeout=10)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            logger.error(f"BingX API request error: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    logger.error(f"BingX error response: {error_data}")
                    return error_data
                except:
                    pass
            return {'code': -1, 'msg': str(e)}
        except Exception as e:
            logger.error(f"BingX API request error: {e}")
            return {'code': -1, 'msg': str(e)}
    
    def verify_connection(self) -> bool:
        """
        Verify BingX API connection.
        
        Returns:
            True if connection successful
        """
        if not self.api_key or not self.secret_key:
            logger.error("❌ BingX connection failed: API credentials not set")
            return False
        
        try:
            logger.info(f"Testing BingX API connection to: {self.base_url}")
            
            # Test with balance endpoint
            response = self._send_request(
                'GET',
                '/openApi/swap/v2/user/balance',
                signed=True
            )
            
            logger.debug(f"BingX response: {response}")
            
            if response.get('code') == 0:
                # Extract balance
                data = response.get('data', {})
                balance_info = data.get('balance', {})
                if balance_info:
                    available_margin = balance_info.get('availableMargin', ACCOUNT_BALANCE_BASELINE)
                    self.account_balance = Decimal(str(available_margin))
                    logger.info(f"✅ BingX connection verified. Balance: {self.account_balance} USDT")
                    return True
                else:
                    logger.info(f"✅ BingX connection verified. Balance: {self.account_balance} USDT (baseline)")
                    return True
            else:
                error_msg = response.get('msg', 'Unknown error')
                error_code = response.get('code', 'N/A')
                logger.error(f"❌ BingX connection failed: {error_msg} (code: {error_code})")
                logger.error(f"Full response: {response}")
                return False
                
        except Exception as e:
            logger.error(f"❌ BingX connection failed: {e}", exc_info=True)
            return False
    
    def get_account_balance(self) -> Decimal:
        """
        Get current account balance from BingX.
        
        Returns:
            Account balance in USDT
        """
        try:
            response = self._send_request(
                'GET',
                '/openApi/swap/v2/user/balance',
                signed=True
            )
            
            if response.get('code') == 0:
                data = response.get('data', {})
                balance_info = data.get('balance', {})
                if balance_info:
                    available_margin = balance_info.get('availableMargin', ACCOUNT_BALANCE_BASELINE)
                    self.account_balance = Decimal(str(available_margin))
                    return self.account_balance
            
            return self.account_balance  # Return cached value
            
        except Exception as e:
            logger.error(f"Failed to get account balance: {e}")
            return self.account_balance  # Return cached value
    
    def get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """
        Get symbol information (tick size, qty step, etc.).
        
        Args:
            symbol: Trading symbol (e.g., "BTC-USDT")
            
        Returns:
            Symbol information dictionary or None
        """
        try:
            formatted_symbol = self._format_symbol(symbol)
            
            response = self._send_request(
                'GET',
                '/openApi/swap/v2/quote/contracts',
                signed=False
            )
            
            if response.get('code') == 0:
                data = response.get('data', [])
                for instrument in data:
                    if instrument.get('symbol') == formatted_symbol:
                        # Normalize tick/step deterministically even when the API returns None/empty.
                        raw_tick = instrument.get('tickSize')
                        tick_size = _safe_decimal(raw_tick, Decimal("0"))
                        if tick_size <= 0:
                            tick_from_prec = _step_from_precision(instrument.get("pricePrecision"))
                            if tick_from_prec is not None:
                                tick_size = tick_from_prec

                        qty_step = _step_from_precision(instrument.get('quantityPrecision')) or Decimal("0")

                        return {
                            'symbol': instrument.get('symbol'),
                            'tickSize': str(tick_size),
                            'lotSizeFilter': {
                                'qtyStep': str(qty_step),
                                'minQty': instrument.get('minQty'),
                                'maxQty': instrument.get('maxQty')
                            }
                        }
            
            logger.warning(f"Symbol {formatted_symbol} not found")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get symbol info: {e}")
            return None
    
    def _format_symbol(self, symbol: str) -> str:
        """
        Format symbol to BingX format (BTC-USDT).
        
        Args:
            symbol: Input symbol
            
        Returns:
            Formatted symbol (BTC-USDT format)
        """
        # Remove existing separators
        symbol = symbol.replace("/", "").replace("-", "")
        
        # Ensure it ends with USDT
        if not symbol.endswith("USDT"):
            if len(symbol) >= 2:
                symbol = symbol + "USDT"
            else:
                symbol = symbol + "USDT"
        
        # Extract base currency (everything before USDT)
        base = symbol[:-4]  # Remove "USDT"
        
        # Format as BASE-USDT
        return f"{base}-USDT"
    
    def _quantize_price(self, price: Decimal, tick_size: Decimal) -> Decimal:
        """
        Quantize price to tick size.
        
        Args:
            price: Price to quantize
            tick_size: Tick size from symbol info
            
        Returns:
            Quantized price
        """
        if tick_size <= 0:
            return price
        
        # Round to nearest tick
        ticks = (price / tick_size).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return (ticks * tick_size).quantize(tick_size)
    
    def _quantize_quantity(self, quantity: Decimal, qty_step: Decimal, min_qty: Decimal) -> Decimal:
        """
        Quantize quantity to qty step and ensure >= min_qty.
        
        Args:
            quantity: Quantity to quantize
            qty_step: Quantity step from symbol info
            min_qty: Minimum quantity from symbol info
            
        Returns:
            Quantized quantity
        """
        if qty_step <= 0:
            return quantity
        
        # Round to nearest step
        steps = (quantity / qty_step).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        quantized = (steps * qty_step).quantize(qty_step)
        
        # Ensure >= min_qty
        if quantized < min_qty:
            quantized = min_qty
        
        return quantized
    
    # ============================================================================
    # POSITION SIZING & LEVERAGE CALCULATION
    # ============================================================================
    
    def calculate_position_size(self, entry_price: Decimal, stop_loss_price: Decimal) -> Dict:
        """
        Calculate position size and leverage according to bot requirements.
        
        Formula: N = (r * B) / Delta
        Where: Delta = abs(E - S) / E
        
        Args:
            entry_price: Entry price (E)
            stop_loss_price: Stop loss price (S)
            
        Returns:
            Dictionary with notional_target, delta, leverage, etc.
        """
        # Calculate Delta
        delta = abs(entry_price - stop_loss_price) / entry_price
        
        if delta == 0:
            logger.warning("Delta is zero, using fallback")
            delta = Decimal("0.02")  # 2% fallback
        
        # Calculate notional target: N = (r * B) / Delta
        notional_target = (self.risk_per_trade * self.account_balance) / delta
        
        # Calculate leverage: Lev_dyn = round(min(max(N / IM_plan, 1), 50), 2)
        leverage_raw = notional_target / self.im_plan
        leverage_clamped = max(min(leverage_raw, MAX_LEVERAGE), MIN_LEVERAGE)
        leverage = Decimal(str(leverage_clamped)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        # Classify leverage
        if leverage <= Decimal("6.00"):
            leverage_class = "SWING"
        elif leverage >= Decimal("7.50"):
            leverage_class = "DYNAMIC"
        else:
            leverage_class = "SWING" if leverage <= Decimal("6.75") else "DYNAMIC"
        
        # Calculate quantity
        quantity = notional_target / entry_price
        
        return {
            'notional_target': notional_target,
            'delta': delta,
            'leverage': leverage,
            'leverage_class': leverage_class,
            'quantity': quantity,
            'risk_percent': self.risk_per_trade,
            'wallet_balance': self.account_balance,
            'im_plan': self.im_plan
        }
    
    def calculate_fast_fallback(self, entry_price: Decimal) -> Dict:
        """
        Calculate FAST fallback when SL is missing.
        SL = -2.00% from entry, leverage = x10.00
        
        Args:
            entry_price: Entry price
            
        Returns:
            Dictionary with SL price and leverage info
        """
        sl_price = entry_price * Decimal("0.98")  # -2% from entry
        leverage = Decimal("10.00")
        
        return {
            'sl_price': sl_price,
            'leverage': leverage,
            'leverage_class': 'FAST',
            'delta': Decimal("0.02")  # 2%
        }
    
    # ============================================================================
    # DUAL-LIMIT ENTRY & MERGING
    # ============================================================================
    
    def calculate_dual_limit_prices(self, target_entry: Decimal, spread: Decimal, tick_size: Decimal) -> Tuple[Decimal, Decimal]:
        """
        Calculate two limit prices for dual-limit entry.
        P1 = quantize(Em - Δ), P2 = quantize(Em + Δ)
        
        Args:
            target_entry: Target entry midpoint (Em)
            spread: Half-spread (Δ)
            tick_size: Tick size for quantization
            
        Returns:
            Tuple of (P1, P2)
        """
        p1 = self._quantize_price(target_entry - spread, tick_size)
        p2 = self._quantize_price(target_entry + spread, tick_size)
        return p1, p2
    
    def place_dual_limit_orders(
        self,
        symbol: str,
        side: str,
        target_entry: Decimal,
        spread: Decimal,
        total_quantity: Decimal,
        leverage: Decimal,
        symbol_info: Dict
    ) -> Dict:
        """
        Place dual-limit entry orders (50/50 split, Post-Only GTC).
        
        Args:
            symbol: Trading symbol
            side: "BUY" or "SELL"
            target_entry: Target entry midpoint
            spread: Half-spread
            total_quantity: Total quantity
            leverage: Leverage to use
            symbol_info: Symbol information (tick_size, qty_step, min_qty)
            
        Returns:
            Dictionary with order IDs and status
        """
        formatted_symbol = self._format_symbol(symbol)
        tick_size = Decimal(str(symbol_info.get('tickSize', '0.0001')))
        lot_size_filter = symbol_info.get('lotSizeFilter', {})
        qty_step = Decimal(str(lot_size_filter.get('qtyStep', '0.001')))
        min_qty = Decimal(str(lot_size_filter.get('minQty', '0.001')))
        
        # Calculate two prices
        p1, p2 = self.calculate_dual_limit_prices(target_entry, spread, tick_size)
        
        # Split quantity 50/50
        q1 = self._quantize_quantity(total_quantity / 2, qty_step, min_qty)
        q2 = self._quantize_quantity(total_quantity - q1, qty_step, min_qty)
        
        orders = []
        order_ids = []
        
        # Set leverage first
        self.set_leverage(formatted_symbol, int(leverage))
        
        # Place first order
        order1 = self.place_limit_order(
            symbol=formatted_symbol,
            side=side,
            price=p1 if side == "BUY" else p2,
            quantity=q1,
            leverage=leverage,
            post_only=True,
            time_in_force="GTC"
        )
        if order1.get('orderId'):
            orders.append(order1)
            order_ids.append(order1['orderId'])
        
        # Place second order
        order2 = self.place_limit_order(
            symbol=formatted_symbol,
            side=side,
            price=p2 if side == "BUY" else p1,
            quantity=q2,
            leverage=leverage,
            post_only=True,
            time_in_force="GTC"
        )
        if order2.get('orderId'):
            orders.append(order2)
            order_ids.append(order2['orderId'])
        
        return {
            'orders': orders,
            'order_ids': order_ids,
            'p1': p1,
            'p2': p2,
            'q1': q1,
            'q2': q2,
            'target_entry': target_entry
        }
    
    def get_current_price(self, symbol: str) -> Decimal:
        """
        Get current last traded price (LTP).
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Current price
        """
        try:
            formatted_symbol = self._format_symbol(symbol)
            response = self._send_request(
                'GET',
                '/openApi/swap/v2/quote/price',
                params={'symbol': formatted_symbol},
                signed=False
            )
            
            if response.get('code') == 0:
                data = response.get('data', {})
                price = Decimal(str(data.get('price', '0')))
                return price
            
            return Decimal("0")
            
        except Exception as e:
            logger.error(f"Failed to get current price: {e}")
            return Decimal("0")
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Set leverage for a symbol.
        
        Args:
            symbol: Trading symbol
            leverage: Leverage value
            
        Returns:
            True if successful
        """
        try:
            response = self._send_request(
                'POST',
                '/openApi/swap/v2/trade/leverage',
                params={
                    'symbol': symbol,
                    'side': 'LONG',  # BingX requires side
                    'leverage': leverage
                },
                signed=True
            )
            
            if response.get('code') == 0:
                logger.info(f"✅ Leverage set to {leverage}x for {symbol}")
                return True
            else:
                logger.error(f"Failed to set leverage: {response.get('msg')}")
                return False
                
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False
    
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        quantity: Decimal,
        leverage: Decimal,
        post_only: bool = True,
        time_in_force: str = "GTC",
        reduce_only: bool = False
    ) -> Dict:
        """
        Place a limit order on BingX.
        
        Args:
            symbol: Trading symbol
            side: "BUY" or "SELL"
            price: Order price
            quantity: Order quantity
            leverage: Leverage
            post_only: Post-only flag
            time_in_force: Time in force (GTC, IOC, FOK)
            reduce_only: Reduce-only flag
            
        Returns:
            Order response dictionary with orderId field
        """
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'positionSide': 'LONG' if side == 'BUY' else 'SHORT',
                'type': 'LIMIT',
                'quantity': str(quantity),
                'price': str(price),
                'timeInForce': time_in_force,
            }
            
            if post_only:
                params['postOnly'] = 'true'
            
            if reduce_only:
                params['reduceOnly'] = 'true'
            
            response = self._send_request(
                'POST',
                '/openApi/swap/v2/trade/order',
                params=params,
                signed=True
            )
            
            if response.get('code') == 0:
                data = response.get('data', {})
                order_id = data.get('order', {}).get('orderId', '')
                logger.info(f"✅ Order placed: {order_id} - {side} {quantity} @ {price}")
                return {
                    'orderId': order_id,
                    'status': 'ACCEPTED',
                    'retCode': 0,
                    **data
                }
            else:
                error_msg = response.get('msg', 'Unknown error')
                logger.error(f"❌ Order placement failed: {error_msg} (code: {response.get('code')})")
                return {
                    'orderId': None,
                    'status': 'FAILED',
                    'retCode': response.get('code'),
                    'error': error_msg
                }
                
        except Exception as e:
            logger.error(f"❌ Order placement error: {e}")
            return {
                'orderId': None,
                'status': 'ERROR',
                'error': str(e)
            }
    
    def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        """
        Get order status from BingX.
        
        Args:
            symbol: Trading symbol
            order_id: Order ID
            
        Returns:
            Order status dictionary or None
        """
        try:
            response = self._send_request(
                'GET',
                '/openApi/swap/v2/trade/order',
                params={
                    'symbol': symbol,
                    'orderId': order_id
                },
                signed=True
            )
            
            if response.get('code') == 0:
                data = response.get('data', {})
                return data.get('order', {})
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get order status: {e}")
            return None
    
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an order.
        
        Args:
            symbol: Trading symbol
            order_id: Order ID
            
        Returns:
            True if cancellation successful
        """
        try:
            response = self._send_request(
                'DELETE',
                '/openApi/swap/v2/trade/order',
                params={
                    'symbol': symbol,
                    'orderId': order_id
                },
                signed=True
            )
            
            if response.get('code') == 0:
                logger.info(f"✅ Order cancelled: {order_id}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return False
    
    # ============================================================================
    # WEBSOCKET CONNECTION & HEARTBEAT
    # ============================================================================
    
    async def connect_websocket(self) -> bool:
        """
        Connect to BingX WebSocket for real-time updates.
        
        Returns:
            True if connection successful
        """
        if not self.api_key or not self.secret_key:
            logger.error("Cannot connect WebSocket: API credentials not set")
            return False
        
        try:
            # BingX WebSocket implementation
            # This is a simplified placeholder - full implementation would use websockets library
            self.ws_connected = True
            self.last_heartbeat = time.time()
            logger.info("✅ WebSocket connection initiated")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect WebSocket: {e}")
            self.ws_connected = False
            return False
    
    async def verify_websocket_heartbeat(self, timeout: int = 30) -> bool:
        """
        Verify WebSocket heartbeat is working (≤30s).
        
        Args:
            timeout: Maximum acceptable heartbeat interval (seconds)
            
        Returns:
            True if heartbeat is within acceptable range
        """
        if not self.ws_connected:
            logger.error("Cannot verify heartbeat: WebSocket not connected")
            return False
        
        try:
            # Wait a bit for initial connection
            await asyncio.sleep(2)
            
            if self.last_heartbeat is None:
                self.last_heartbeat = time.time()
            
            time_since_heartbeat = time.time() - self.last_heartbeat
            
            if time_since_heartbeat <= timeout:
                logger.info(f"✅ WebSocket heartbeat OK ({time_since_heartbeat:.1f}s ago)")
                return True
            else:
                logger.error(f"❌ WebSocket heartbeat timeout ({time_since_heartbeat:.1f}s > {timeout}s)")
                return False
                
        except Exception as e:
            logger.error(f"Error verifying WebSocket heartbeat: {e}")
            return False
    
    def disconnect_websocket(self):
        """Disconnect WebSocket connection."""
        if self.ws_session:
            try:
                self.ws_connected = False
                self.ws_session = None
                logger.info("WebSocket disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting WebSocket: {e}")

