# BingX Implementation Requirements - Extracted from Bot Specification

## 📊 Account & Risk Parameters (SSoT Baseline)

### Base Configuration:
- **Account Balance (B)**: 402.10 USDT (baseline)
- **Risk Per Trade (r)**: 2% per trade
- **Initial Margin (IM_plan)**: 20 USDT per trade
- **Max Active Trades**: 100 simultaneously
- **Max Leverage**: 50x (clamped)

---

## 🧮 Dynamic Leverage Formula (MANDATORY)

### Definitions:
```
B = account balance (USDT)
r = risk per trade (fraction, e.g., 0.02 for 2%)
E = entry price
S = stop-loss price
Delta = abs(E - S) / E
IM_plan = planned initial margin (USDT) = 20 USDT
```

### Calculation:
```
N = (r * B) / Delta
Lev_dyn = round(min(max(N / IM_plan, 1), 50), 2)
```

### Classification:
- **SWING**: Lev ≤ 6.00×
- **DYNAMIC**: Lev ≥ 7.50×
- **Intermediate (6.00× - 7.50×)**: Classified to nearest (SWING or DYNAMIC)
- **FAST Fallback**: If SL missing → SL = -2.00% from entry, leverage = x10.00

### Formatting:
- Leverage: Always 2 decimals (xNN.NN format)
- Percent: Always 2 decimals with % (e.g., -2.00 %)

---

## 📈 Position Sizing Formula

### Calculation:
```
N = (r * B) / Delta
```
Where:
- `N` = Notional target (position size in USDT)
- `r` = Risk per trade (0.02 = 2%)
- `B` = Account balance (402.10 USDT baseline)
- `Delta` = abs(E - S) / E (price risk percentage)

### Quantity Calculation:
```
Quantity = N / Entry_Price
```
Then quantize to symbol's `qtyStep` and ensure ≥ `minQty`.

---

## 🎯 Dual-Limit Entry & Merging (MANDATORY)

### Purpose:
Two post-only GTC limit orders that wait to be hit and merge into one position.

### Target Entry & Spread:
- Define target entry `Em` (midpoint)
- Define half-spread `Δ` (price, percent, or ticks)

### Calculate Two Prices:
```
P1 = quantize(Em - Δ)
P2 = quantize(Em + Δ)
```

### Placement Rules:
- **LONG**: Both limit buys placed **below** LTP (Last Traded Price)
- **SHORT**: Both limit sells placed **above** LTP
- **TimeInForce**: GTC (Good Till Cancel)
- **PostOnly**: true (maker protection)
- **ReduceOnly**: false

### Quantity Split:
- Total quantity `Q` split 50/50:
  - `q1 = Q/2`
  - `q2 = Q - q1`
- Adjust to `minQty`/`qtyStep`

### Merging on First Fill:
1. On first fill (full/partial), keep:
   - Accumulated fill `f`
   - Price-sum: `Σ(fill_qty * fill_price)`

2. Calculate replacement price for remaining `Q - f`:
   ```
   pr = quantize((Em * Q - Σ(fill_qty * fill_price)) / (Q - f))
   ```

3. Cancel the other original order (if open)

4. Place new post-only GTC limit at `pr` for `Q - f`

5. If both partially filled, use total sums; place replacement only if `f < Q`

### Precision:
- Price quantized to symbol's `tickSize`
- Quantity quantized to `qtyStep`
- `postOnly` must not be broken; adjust price 1 tick from spread if needed

---

## 🛑 Stop Loss Rules

### Initial SL:
- Set exactly according to signal
- **Exception**: If SL missing → auto-SL = -2.00% from entry, leverage = x10.00 (FAST)

### SL Adjustments:
- All SL adjustments based on **original entry** (not adjusted entry)
- Move SL to BE (Break Even) → interpreted and executed
- Move SL to TP1 → interpreted and executed
- When TP2 hit → SL moved to BE + cost (0.0015%)

### Trailing Stop:
- Activated at +6.1% unrealized profit
- SL placed 2.5% behind highest price (or lowest for SHORT)
- Follows price continuously

---

## 🎯 Take Profit Rules

### TP Placement:
- TP list from signal: `tp_list[]` with:
  - Price
  - % from entry
  - Quantity/share

### Validation:
- TP prices must be in correct order
- Percent labeling must be accurate
- Quantities/shares must be valid
- Sum of TP quantities must not exceed planned position size

### TP Execution:
- TP orders must have `reduce_only = true`
- TP/SL set exactly according to signal (exception: auto-SL fallback)

---

## 🔄 Pyramid Rules

### Ladder (Example):
- +1.5% → IM = 20 USDT
- +2.3% → SL = BE + cost
- +2.4% → Leverage up to max (50×)
- +2.5% → IM = 40 USDT
- +4% → IM = 60 USDT
- +6% → IM = 80 USDT
- +8.6% → IM = 100 USDT

### Recalculation:
- All pyramid levels recalculated from **original entry** (not adjusted entry)

---

## 🔀 Hedge & Re-entry Rules

### Hedge Trigger:
- If price moves -2% against position → open hedge in opposite direction
- **Size**: 100% of original position
- **TP**: Original SL
- **SL**: Original entry
- **Leverage**: Same as original position

### Re-entry:
- After SL hit: attempt re-entry with dual-limit up to 3 times
- After 3 attempts: stop until new external signal arrives

---

## ✅ Order Validation Requirements

### Must NOT publish to Telegram if:
1. SL missing and FAST fallback (-2.00%, x10.00) not applied
2. Leverage without 2 decimals or outside [1, 50] after clamping
3. TP list errors: price order, percent labeling, quantities/shares
4. `risk_percent`, `wallet_balance_snapshot`, `im_plan`, `delta` missing or = 0
5. Sum of TP quantities exceeds planned position size
6. `reduce_only`/`post_only` inconsistent with order type
7. TP/SL not exactly according to signal (except auto-SL fallback)
8. Exit orders without `reduce_only = true`

---

## 🔄 Bybit-First Flow (Wait for Confirmation)

### Chain:
1. Ingest from source → Parser/Normalization → Validation → Template builder
2. Order placement to BingX (POST) according to template
3. **Wait for acknowledgement** from BingX API/WS for every step:
   - **ORDER_ACCEPTED**: retCode = 0 and valid order/position IDs
   - **TP_SL_SET**: All TP/SL orders confirmed
   - **POSITION_OPENED**: Verified fill (executedQty > 0) or WS event

4. **Telegram publishing** takes place ONLY after confirmation in respective step

### Mandatory Template Header:
```
SENT ONLY AFTER BYBIT CONFIRMATION (retCode=0/fills)
```

---

## 📋 Mandatory Fields in All Templates

### Identity & Traceability:
- `bot_order_id`
- `bybit_order_id` (when available)
- `position_id` (when applicable)
- `source_channel_name`
- `signal_message_id`
- `parsed_at`
- `sent_at`
- `env` (prod/stage)

### Trading Data:
- `symbol`
- `side` (LONG/SHORT)
- `entry_price`
- `sl_price`
- `tp_list[]` (price, % from entry, quantity/share)
- `leverage_class` ∈ {SWING, DYNAMIC, FAST}
- `leverage_value` (two decimals)
- `order_type`
- `time_in_force`
- `post_only`
- `reduce_only`
- `margin_mode`
- `risk_percent` (= r)
- `wallet_balance_snapshot` (= B)
- `im_plan` (= IM_plan)
- `delta` (= Delta)
- `notional_target` (= N)

### Acknowledgement Flags:
- `order_accepted`
- `tp_sl_set`
- `position_opened`
- `position_closed`
- `be_moved`
- `trailing_active` (when applicable)

---

## 🧹 Order Cleanup (Timeouts)

### Timeout Rules:
- **24h**: Short timeout for hanging opening orders and partially filled orders (re-query + cancel)
- **6d**: Long-term cleanup of remaining orders that did not result in a position

### Requirements:
- Jobs MUST be persistent
- Automatically resumed after restart
- Idempotent cleanup operations

---

## 📊 Calculation & Formatting Policy

### Leverage:
- According to formula
- Clamped [1, 50]
- Rounded HALF-UP to 2 decimals
- Displayed: `xNN.NN`

### Percent:
- Always 2 decimals with % (e.g., `+2.98 %`)

### Price:
- Quantized to BingX's `tickSize` before sending and at every update

### Quantities:
- Quantized to instrument's `minQty`/`qtyStep`

### Currency:
- Internal calculations in USDT (unless otherwise stated)

---

## 🔐 Conflict & Fallback Rules

1. **SL missing** → FAST fallback (SL -2.00%, x10.00)
2. **Lev in (6.00×; 7.50×)** → Classified to closest class
3. **BingX returns deviating values** → Update internal fields and template before publishing

---

## ⚡ Performance Requirements

- **REST latency**: ≤ 500ms p95
- **WS heartbeat**: ≤ 30s
- **Effective rate-limit handling**
- **No blocking chokepoints** in main loop
- Deviation → operations blocked until fixed

---

## 🚦 Startup Requirements

### On Connection (Mandatory Print):
On start/restart, bot must immediately print to private channel with confirmation per line (✅):

- Wallet balance (baseline): 402.10 USDT (SSoT) ✅
- Risk settings (e.g., 2% per trade) ✅
- Strategies that are active ✅
- Active positions & open orders (summary) ✅

All data MUST be confirmed against BingX API/WS and source Telegram. No assumptions may be published.

---

## 📝 Summary: What I Need to Implement

### 1. **Position Sizing**:
   - Formula: `N = (r * B) / Delta`
   - Default: r = 0.02 (2%), B = 402.10 USDT, IM_plan = 20 USDT

### 2. **Leverage Calculation**:
   - Formula: `Lev_dyn = round(min(max(N / IM_plan, 1), 50), 2)`
   - Classification: SWING (≤6x), DYNAMIC (≥7.5x), FAST (fallback)

### 3. **Order Type**:
   - Dual-Limit Entry with merging
   - Post-Only GTC limit orders
   - 50/50 quantity split

### 4. **Risk Management**:
   - 2% risk per trade
   - Dynamic SL based on entry
   - Multiple TP levels with quantities

### 5. **Order Execution**:
   - Wait for BingX confirmation before publishing
   - Validate all fields before sending
   - Handle errors gracefully

### 6. **Cleanup**:
   - 24h timeout for hanging orders
   - 6d timeout for unfilled orders
   - Persistent jobs

---

## ✅ Ready for Implementation

All key requirements extracted. Ready to implement BingX integration with:
- ✅ Position sizing formula
- ✅ Leverage calculation
- ✅ Dual-limit entry logic
- ✅ Risk management rules
- ✅ Order validation
- ✅ Bybit-first flow (wait for confirmation)
- ✅ Cleanup timeouts
- ✅ Performance requirements

