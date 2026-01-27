import logging
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

import config
from bingx_client import BingXClient

logger = logging.getLogger(__name__)


def get_pending_orders(bingx: BingXClient, symbol: str | None = None):
	open_orders = bingx.get_open_orders(symbol)
	pending = [o for o in open_orders if (str(o.get("status", "")).upper() == "PENDING")]
	return pending

def get_opened_orders(bingx: BingXClient):
    open_orders = bingx.get_open_orders()
    return open_orders


def cancel_all_open_orders(bingx: BingXClient) -> int:
	open_orders = bingx.get_open_orders()
	cancelled = 0
	for order in open_orders:
		order_id = order.get("orderId")
		symbol = order.get("symbol")
		if order_id and symbol:
			result = bingx.cancel_order(symbol, order_id)
			logger.info("Cancelled order: symbol=%s order_id=%s result=%s", symbol, order_id, result)
			if result:
				cancelled += 1
	return cancelled


def clear_bingx_account(bingx: BingXClient) -> dict:
	"""
	Clear BingX by closing all open positions and cancelling all open orders.
	NOTE: BingX does NOT allow deleting trade/order history.
	This only clears ACTIVE exposure and OPEN orders.
	"""
	closed_count = close_all_positions(bingx)
	cancelled_count = cancel_all_open_orders(bingx)
	result = {
		"positions_closed": closed_count,
		"orders_cancelled": cancelled_count,
		"history_cleared": False,
		"note": "BingX does not support deleting order/trade history. Only active positions and open orders were cleared.",
	}
	logger.info("Clear BingX result: %s", result)
	return result


def close_all_positions(bingx: BingXClient) -> int:
	positions = bingx.get_positions()
	closed = 0
	for pos in positions or []:
		symbol = pos.get("symbol") or pos.get("symbolName")
		qty_raw = pos.get("positionAmt") or pos.get("positionSize") or pos.get("positionQty") or "0"
		try:
			qty = Decimal(str(qty_raw))
		except Exception:
			qty = Decimal("0")
		if not symbol or qty == 0:
			continue

		position_side = str(pos.get("positionSide") or "").upper()
		if position_side not in {"LONG", "SHORT"}:
			position_side = "LONG" if qty > 0 else "SHORT"

		side = "SELL" if position_side == "LONG" else "BUY"
		quantity = abs(qty)
		resp = bingx.place_market_order(
			symbol=symbol,
			side=side,
			quantity=quantity,
			reduce_only=True,
			position_side=position_side,
		)
		logger.info(
			"Close position: symbol=%s position_side=%s qty=%s resp=%s",
			symbol,
			position_side,
			quantity,
			resp,
		)
		if resp.get("orderId"):
			closed += 1
	return closed


def get_positions_list(bingx: BingXClient):
	return bingx.get_positions()


def ensure_min_leverage_open_positions(bingx: BingXClient, min_leverage: int = 6) -> int:
	"""
	Ensure all open positions have at least min_leverage.
	Returns the number of positions updated.
	"""
	updated = 0
	positions = bingx.get_positions()
	for pos in positions or []:
		symbol = pos.get("symbol") or pos.get("symbolName")
		lev_raw = pos.get("leverage")
		try:
			lev = int(Decimal(str(lev_raw)))
		except Exception:
			continue
		if not symbol:
			continue
		if lev < int(min_leverage):
			formatted_symbol = bingx._format_symbol(symbol)
			min_lev_val = Decimal(str(min_leverage)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
			ok = bingx.set_leverage(formatted_symbol, min_lev_val)
			logger.info("Set leverage: symbol=%s old=%s new=%s ok=%s", symbol, lev, min_leverage, ok)
			if ok:
				updated += 1
	return updated


def get_order_history(bingx: BingXClient, symbol: str, limit: int = 100):
	"""Get historical orders from BingX (both filled and cancelled)"""
	return bingx.get_my_trades(symbol, limit, None)


def check_position_tp_sl_from_db(ssot_id: int):
	"""Check TP/SL information stored in database for a specific position"""
	db_path = "data/ssot.sqlite3"
	conn = sqlite3.connect(db_path)
	conn.row_factory = sqlite3.Row
	cur = conn.cursor()
	
	# Get position info
	cur.execute("""
		SELECT 
			ssot_id, symbol, side, status, 
			sl_price, sl_order_id,
			tp_levels_json, tp_active_order_ids_json,
			position_qty, remaining_qty,
			avg_entry, realized_pnl, unrealized_pnl,
			closed_reason, closed_at_utc,
			signal_entry_price, signal_sl_price
		FROM stage4_positions 
		WHERE ssot_id = ?
	""", (ssot_id,))
	
	pos = cur.fetchone()
	if not pos:
		logger.warning("Position ssot_id=%s not found in database", ssot_id)
		conn.close()
		return None
	
	# Get order tracker info (TP/SL order IDs with execution status)
	cur.execute("""
		SELECT 
			order_id, kind, level_index, 
			last_executed_qty, last_status
		FROM stage4_order_tracker 
		WHERE ssot_id = ?
		ORDER BY kind, level_index
	""", (ssot_id,))
	
	orders = [dict(row) for row in cur.fetchall()]
	conn.close()
	
	result = dict(pos)
	result["tracked_orders"] = orders
	return result


def print_position_tp_sl_info(ssot_id: int, bingx: BingXClient = None):
	"""Print comprehensive TP/SL information for a position"""
	info = check_position_tp_sl_from_db(ssot_id)
	if not info:
		return
	
	import json
	
	print(f"\n{'='*70}")
	print(f"Position Information (ssot_id={ssot_id})")
	print(f"{'='*70}")
	print(f"Symbol: {info['symbol']}")
	print(f"Side: {info['side']}")
	print(f"Status: {info['status']}")
	print(f"Position Qty: {info['position_qty']}")
	print(f"Remaining Qty: {info['remaining_qty']}")
	print(f"Avg Entry: {info['avg_entry']}")
	print(f"Realized PnL: {info['realized_pnl']}")
	print(f"Unrealized PnL: {info['unrealized_pnl']}")
	
	print(f"\n--- Stop Loss ---")
	print(f"SL Price: {info['sl_price']}")
	print(f"SL Order ID: {info['sl_order_id']}")
	
	print(f"\n--- Take Profit Levels ---")
	tp_levels = json.loads(info['tp_levels_json'] or "[]")
	for i, tp in enumerate(tp_levels):
		print(f"TP{i+1}: Price={tp.get('price')}, Status={tp.get('status')}, "
		      f"Filled={tp.get('filled_qty')}, OrderID={tp.get('order_id')}")
	
	print(f"\n--- Tracked Orders (with execution history) ---")
	for order in info['tracked_orders']:
		print(f"{order['kind']:5} | OrderID={order['order_id']:20} | "
		      f"Level={str(order['level_index'] or 'N/A'):3} | "
		      f"Executed={order['last_executed_qty']:10} | "
		      f"Status={order['last_status'] or 'N/A'}")
	
	if info['status'] == 'CLOSED':
		print(f"\n--- Closed Position ---")
		print(f"Close Reason: {info['closed_reason']}")
		print(f"Closed At: {info['closed_at_utc']}")
	
	# If BingX client provided, fetch live order status from exchange
	if bingx and info['sl_order_id']:
		print(f"\n--- Live BingX Order Status ---")
		try:
			sl_status = bingx.get_order_status(info['symbol'], info['sl_order_id'])
			print(f"SL Order (BingX): {sl_status}")
		except Exception as e:
			print(f"Could not fetch SL from BingX: {e}")
	
	print(f"{'='*70}\n")


def list_all_positions_with_tp_sl():
	"""List all positions with their TP/SL order IDs"""
	db_path = "data/ssot.sqlite3"
	conn = sqlite3.connect(db_path)
	conn.row_factory = sqlite3.Row
	cur = conn.cursor()
	
	cur.execute("""
		SELECT 
			ssot_id, symbol, side, status,
			sl_order_id, tp_active_order_ids_json,
			position_qty, remaining_qty,
			closed_at_utc
		FROM stage4_positions 
		ORDER BY ssot_id DESC
		LIMIT 50
	""")
	
	positions = [dict(row) for row in cur.fetchall()]
	conn.close()
	
	print(f"\n{'='*100}")
	print(f"{'SSOT':<6} {'Symbol':<12} {'Side':<6} {'Status':<8} {'Qty':<10} {'SL OrderID':<22} {'# TP Orders'}")
	print(f"{'='*100}")
	
	import json
	for pos in positions:
		tp_count = len(json.loads(pos['tp_active_order_ids_json'] or "[]"))
		print(f"{pos['ssot_id']:<6} {pos['symbol']:<12} {pos['side']:<6} {pos['status']:<8} "
		      f"{pos['remaining_qty']:<10} {pos['sl_order_id'] or 'N/A':<22} {tp_count}")
	
	print(f"{'='*100}\n")
	return positions


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	bingx = BingXClient(testnet=config.BINGX_TESTNET)
	clear_bingx_account(bingx)
	# -----------------------------------------------------
	# CHECK TP/SL INFORMATION FOR SPECIFIC POSITION
	# -----------------------------------------------------
	# Example: Check position with ssot_id=97
	# print_position_tp_sl_info(ssot_id=97, bingx=bingx)
	
	# -----------------------------------------------------
	# LIST ALL POSITIONS WITH TP/SL ORDER IDs
	# -----------------------------------------------------
	# list_all_positions_with_tp_sl()
	
	# -----------------------------------------------------
	# GET CURRENT OPEN POSITIONS FROM BINGX
	# -----------------------------------------------------
	pending_orders = get_pending_orders(bingx)
	opened_orders = get_opened_orders(bingx)
	positions = get_positions_list(bingx)
	# updated = ensure_min_leverage_open_positions(bingx, min_leverage=6)
	# logger.info("Updated leverage to >= 6 for %s positions", updated)
	
	# -----------------------------------------------------
	# Close all positions (market reduce-only)
	# -----------------------------------------------------
	# closed_count = close_all_positions(bingx)
	# logger.info("Closed positions count: %s", closed_count)
	
	# -----------------------------------------------------
	# Clear orders (open only)
	# -----------------------------------------------------
	# cancelled_count = cancel_all_open_orders(bingx)
	# logger.info("Cancelled open orders count: %s", cancelled_count)

	# -----------------------------------------------------
	# CLEAR BINGX (close positions + cancel orders)
	# -----------------------------------------------------
	# clear_bingx_account(bingx)
	
	# -----------------------------------------------------
	# Pending orders
	# -----------------------------------------------------
	# logger.info("Pending orders: %s", pending_orders)
	
	# -----------------------------------------------------
	# Opened orders
	# -----------------------------------------------------
	# logger.info("Opened orders count: %s", len(opened_orders))
	# logger.info("Opened orders: %s", opened_orders)

	# -----------------------------------------------------
	# Positions
	# -----------------------------------------------------
	logger.info("Positions count: %s", len(positions or []))
	logger.info("Positions: %s", positions)