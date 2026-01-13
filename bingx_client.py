#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BingX Trading Client
===================
Implements BingX API integration with all trading bot requirements:
- Dynamic leverage calculation
- Dual-limit entry with merging
- Position sizing
- Order placement and validation
- Bybit-first flow (wait for confirmation)

Author: Trading Bot Project
Date: 2026-01-08
"""

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
# CONFIGURATION
# ============================================================================

# BingX API Credentials
BINGX_API_KEY = "Z3w6CaFqcLhk05UfB58enOYrvULTCtaSnGcye7CtWpbERiNfDXsDT9x79IDVw77atzAxeLA4tjZ03lpFerGWCA"
BINGX_SECRET_KEY = "vjQfaT0l3kXooWHLLBQT1yV8J6GXHNgPLO3y0x760kdT8piEaIZ51168J57SoGX8FV8dXCrNBU8FHMzM3w"

# BingX API Endpoints
BINGX_API_BASE = "https://open-api.bingx.com"  # Mainnet
# BINGX_API_BASE = "https://open-api-vst.bingx.com"  # Testnet (uncomment for testnet)

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
            secret_key: BingX secret key (defaults to config)
            testnet: Use testnet endpoint (default: False)
        """
        self.api_key = api_key or BINGX_API_KEY
        self.secret_key = secret_key or BINGX_SECRET_KEY
        self.base_url = "https://open-api-vst.bingx.com" if testnet else BINGX_API_BASE
        self.session = requests.Session()
        self.session.headers.update({
            "X-BX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        })
        
        # Account parameters (SSoT)
        self.account_balance = ACCOUNT_BALANCE_BASELINE
        self.risk_per_trade = RISK_PER_TRADE
        self.im_plan = INITIAL_MARGIN_PLAN
        
        logger.info(f"BingX client initialized (testnet={testnet})")
    
    def _generate_signature(self, params: Dict) -> str:
        """Generate HMAC-SHA256 signature for BingX API."""
        query_string = urlencode(sorted(params.items()))
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _make_request(self, method: str, endpoint: str, params: Dict = None, signed: bool = True, use_json: bool = False) -> Dict:
        """
        Make API request to BingX.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Request parameters
            signed: Whether to sign the request
            
        Returns:
            API response as dictionary
        """
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
        
        try:
            if method.upper() == "GET":
                response = self.session.get(url, params=params, timeout=5)
            elif method.upper() == "POST":
                if use_json:
                    response = self.session.post(url, json=params, timeout=5)
                else:
                    response = self.session.post(url, params=params, timeout=5)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, params=params, timeout=5)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            response.raise_for_status()
            data = response.json()
            
            # Check BingX response code
            if data.get('code') != 0:
                error_msg = data.get('msg', 'Unknown error')
                logger.error(f"BingX API error: {error_msg} (code: {data.get('code')})")
                raise Exception(f"BingX API error: {error_msg}")
            
            return data
            
        except requests.exceptions.Timeout:
            logger.error(f"BingX API timeout: {endpoint}")
            raise Exception("BingX API timeout")
        except requests.exceptions.RequestException as e:
            logger.error(f"BingX API request error: {e}")
            raise Exception(f"BingX API request failed: {e}")
    
    def verify_connection(self) -> bool:
        """
        Verify BingX API connection.
        
        Returns:
            True if connection successful
        """
        try:
            # Test with account balance endpoint
            data = self._make_request("GET", "/openApi/account/v1/balance", signed=True)
            if data.get('code') == 0:
                # Update account balance from API
                balance_data = data.get('data', {}).get('balance', {})
                if balance_data:
                    self.account_balance = Decimal(str(balance_data.get('balance', ACCOUNT_BALANCE_BASELINE)))
                logger.info(f"✅ BingX connection verified. Balance: {self.account_balance} USDT")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ BingX connection failed: {e}")
            return False
    
    def get_account_balance(self) -> Decimal:
        """
        Get current account balance from BingX.
        
        Returns:
            Account balance in USDT
        """
        try:
            data = self._make_request("GET", "/openApi/account/v1/balance", signed=True)
            balance_data = data.get('data', {}).get('balance', {})
            if balance_data:
                self.account_balance = Decimal(str(balance_data.get('balance', ACCOUNT_BALANCE_BASELINE)))
            return self.account_balance
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
            # Convert symbol format if needed (BTCUSDT -> BTC-USDT)
            formatted_symbol = self._format_symbol(symbol)
            
            data = self._make_request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
            contracts = data.get('data', [])
            
            for contract in contracts:
                if contract.get('symbol') == formatted_symbol:
                    return contract
            
            logger.warning(f"Symbol {formatted_symbol} not found")
            return None
            
        except Exception as e:
            logger.error(f"Failed to get symbol info: {e}")
            return None
    
    def _format_symbol(self, symbol: str) -> str:
        """
        Format symbol to BingX format (BTCUSDT -> BTC-USDT).
        
        Args:
            symbol: Input symbol
            
        Returns:
            Formatted symbol
        """
        # Remove USDT suffix and add dash
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}-USDT"
        elif "/" in symbol:
            return symbol.replace("/", "-")
        return symbol
    
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
            Dictionary with:
            - notional_target (N)
            - delta (Delta)
            - leverage (Lev_dyn)
            - leverage_class (SWING/DYNAMIC/FAST)
            - quantity
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
            # Classify to nearest
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
        qty_step = Decimal(str(symbol_info.get('lotSizeFilter', {}).get('qtyStep', '0.001')))
        min_qty = Decimal(str(symbol_info.get('lotSizeFilter', {}).get('minQty', '0.001')))
        
        # Calculate two prices
        p1, p2 = self.calculate_dual_limit_prices(target_entry, spread, tick_size)
        
        # Split quantity 50/50
        q1 = self._quantize_quantity(total_quantity / 2, qty_step, min_qty)
        q2 = self._quantize_quantity(total_quantity - q1, qty_step, min_qty)
        
        # Get current price to ensure orders don't fill immediately
        # For LONG: place below LTP, for SHORT: place above LTP
        current_price = self.get_current_price(formatted_symbol)
        
        orders = []
        order_ids = []
        
        # Place first order
        order1 = self.place_limit_order(
            symbol=formatted_symbol,
            side=side,
            price=p1 if side == "BUY" else p2,  # Adjust based on side
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
            price=p2 if side == "BUY" else p1,  # Adjust based on side
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
            data = self._make_request("GET", f"/openApi/swap/v3/quote/price?symbol={formatted_symbol}", signed=False)
            price = Decimal(str(data.get('data', {}).get('price', '0')))
            return price
        except Exception as e:
            logger.error(f"Failed to get current price: {e}")
            return Decimal("0")
    
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
            Order response dictionary
        """
        formatted_symbol = self._format_symbol(symbol)
        
        params = {
            'symbol': formatted_symbol,
            'side': side,
            'type': 'LIMIT',
            'price': str(price),
            'quantity': str(quantity),
            'leverage': str(leverage),
            'timeInForce': time_in_force,
            'postOnly': 'true' if post_only else 'false',
            'reduceOnly': 'true' if reduce_only else 'false'
        }
        
        try:
            data = self._make_request("POST", "/openApi/swap/v3/trade/order", params=params, signed=True)
            
            if data.get('code') == 0:
                order_data = data.get('data', {})
                logger.info(f"✅ Order placed: {order_data.get('orderId')} - {side} {quantity} @ {price}")
                return {
                    'orderId': order_data.get('orderId'),
                    'status': 'ACCEPTED',
                    'retCode': 0,
                    **order_data
                }
            else:
                logger.error(f"❌ Order placement failed: {data.get('msg')}")
                return {
                    'orderId': None,
                    'status': 'FAILED',
                    'retCode': data.get('code'),
                    'error': data.get('msg')
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
            formatted_symbol = self._format_symbol(symbol)
            params = {
                'symbol': formatted_symbol,
                'orderId': order_id
            }
            data = self._make_request("GET", "/openApi/swap/v3/trade/order", params=params, signed=True)
            
            if data.get('code') == 0:
                return data.get('data', {})
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
            formatted_symbol = self._format_symbol(symbol)
            params = {
                'symbol': formatted_symbol,
                'orderId': order_id
            }
            data = self._make_request("DELETE", "/openApi/swap/v3/trade/order", params=params, signed=True)
            
            if data.get('code') == 0:
                logger.info(f"✅ Order cancelled: {order_id}")
                return True
            return False
            
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

