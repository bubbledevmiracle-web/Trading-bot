import logging
from decimal import Decimal

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

if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	bingx = BingXClient(testnet=config.BINGX_TESTNET)
	pending_orders = get_pending_orders(bingx)
	opened_orders = get_opened_orders(bingx)
	positions = get_positions_list(bingx)
	
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
	# Pending orders
	# -----------------------------------------------------
	# logger.info("Pending orders: %s", pending_orders)
	
	# -----------------------------------------------------
	# Opened orders
	# -----------------------------------------------------
	logger.info("Opened orders count: %s", len(opened_orders))
	logger.info("Opened orders: %s", opened_orders)

	# -----------------------------------------------------
	# Positions
	# -----------------------------------------------------
	# logger.info("Positions count: %s", len(positions or []))
	# logger.info("Positions: %s", positions)