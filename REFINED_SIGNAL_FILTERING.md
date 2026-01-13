# Refined Signal Filtering Logic - Based on Real Signal Examples

## 📊 Analysis of Signal Examples

### **✅ All Examples Are Valid Trading Signals (Should Forward)**

All provided examples contain the **essential trading signal components**:

1. **Symbol** ✅ - Trading pair identification
2. **Direction** ✅ - LONG or SHORT
3. **Entry** ✅ - Entry price/zone
4. **Targets** ✅ - Take-Profit levels
5. **Stop Loss** ✅ - Risk management level

---

## 🔍 **Signal Pattern Recognition**

### **Common Signal Formats Found:**

#### **Format 1: Structured with Emojis**
```
💎#GUNUSDT: #LONG
✅Entry zone 0.02350 - 0.02320
☑️Targets: $0.02375, $0.02400...
🚫Stop loss -0.02234
⚜️Leverage: 50x
```

#### **Format 2: "Opening" Style**
```
🟢 Opening LONG 📈
🟢 Symbol: ETHUSDT
💰 Price: 3138.9900
➡️ Entry: 3138.9900
🎯 Targets: TP1: 3223.742730...
🛑 SL: 3013.430400
```

#### **Format 3: Exchange-Based Format**
```
Exchange: BingX #MELANIA/USDT SHORT
Leverage: 25x
Entries: 0.1525
Targets 0.1509 - 0.1506...
Stoploss: 0.1571
```

#### **Format 4: Minimal Format**
```
#FHE LONG SETUP
Target 1: $0.04160
Target 2: $0.04210
Target 3: $0.04305
STOP : $0.03920
```

#### **Format 5: "THE SWEDEN SCALPER" Format**
```
👍THE SWEDEN SCALPER👍
✔️COIN NAME: GUN(USDT)
LEVERAGE: 75x
🔼TRADE TYPE: LONG 📈
✔️ENTRY PRICE (0.02734-0.02650)
☄️TAKE-PROFITS 1️⃣ 0.02765...
STOP LOSS: 0.2530
```

---

## 🎯 **Refined Filtering Logic**

### **Phase 1: Hard Exclusion (Fast Reject - Critical)**

**Exclude messages containing ANY of:**

1. **Status/Completion Keywords:**
   - `"All entry targets achieved"`
   - `"All take-profit targets achieved"`
   - `"Take-Profit target [number] ✅"` (pattern: `target \d+ ✅`)
   - `"Profit: [number]% Period: [time]"` (completed trade)
   - `"achieved 😎"` or `"achieved ✅"`
   - Messages ending with `"✅"` after "target" keyword

2. **Personal/General Chat:**
   - Starts with `"I've"`, `"I am"`, `"I want"`, `"I decided"` (if no trading data)
   - Very generic messages without trading context
   - Investment decisions, motivation statements

3. **Media/Images:**
   - Messages with only image/media, no text content
   - Screenshots (handle in code: `if not message.text: return`)

4. **Advertisements/Spam:**
   - Obvious promotional content
   - Links without trading data

---

### **Phase 2: Signal Pattern Detection (Required for Acceptance)**

**Accept message if it contains ALL required components:**

#### **Required Component 1: Symbol Pattern** ✅
**Must have at least one:**
- `#SYMBOL` format: `#GUNUSDT`, `#FHE`, `#XTZ/USDT`, `#ZBCN/USDT`
- `SYMBOLUSDT` format: `ETHUSDT`, `UNIUSDT`, `FETUSDT`, `APTUSDT`
- `SYMBOL/USDT` format: `CLO/USDT`, `MELANIA/USDT`, `XTZ/USDT`
- `SYMBOL(USDT)` format: `GUN(USDT)`
- Regex patterns:
  - `#[A-Z]{2,10}(USDT|/USDT)?` - #SYMBOL or #SYMBOLUSDT or #SYMBOL/USDT
  - `\b[A-Z]{2,10}USDT\b` - SYMBOLUSDT
  - `\b[A-Z]{2,10}/USDT\b` - SYMBOL/USDT
  - `\b[A-Z]{2,10}\(USDT\)\b` - SYMBOL(USDT)

#### **Required Component 2: Direction Indicator** ✅
**Must have at least one:**
- Direction keywords: `LONG`, `SHORT`, `BUY`, `SELL`
- Explicit format: `"Trade Type: Short"`, `"Signal Type: Long"`, `"Type - LONG"`
- Emoji indicators: `📈` (LONG), `📉` (SHORT) in context
- Direction in context: `"Opening LONG"`, `"LONG SETUP"`, `"SHORT"`

#### **Required Component 3: Trading Data** ✅
**Must have at least TWO of:**
- **Entry:** `"Entry"`, `"Entry zone"`, `"Entry Price"`, `"Entry Targets"`, `"Entry Orders"`, `"Entries"`, `"Entry:"`
- **Targets:** `"Target"`, `"Targets"`, `"Take-Profit"`, `"Take Profits"`, `"TP"`, `"TP1"`, `"TP2"`, `"target 1:"`, `"Target -"`
- **Stop Loss:** `"Stop Loss"`, `"Stop-Loss"`, `"Stop loss"`, `"SL"`, `"STOP"`, `"Stoploss"`

**OR if message has:**
- Symbol + Direction + Entry (without TP/SL) - Still acceptable (new signal)
- Symbol + Direction + TP + SL (without explicit Entry) - Still acceptable

---

### **Phase 3: Signal Validation Rules**

**High Confidence Signal (Forward):**
```
✅ Has Symbol
✅ Has Direction (LONG/SHORT)
✅ Has Entry (or Entry zone)
✅ Has at least one Target/TP
✅ Has Stop Loss
```

**Medium Confidence Signal (Forward - Tunable):**
```
✅ Has Symbol
✅ Has Direction
✅ Has Entry (or Entry zone)
✅ Has Target/TP (but no explicit SL) - OR has SL but no TP
```

**Low Confidence (Skip):**
```
❌ Has Symbol but no Direction
❌ Has Symbol but no Entry/Target/SL
❌ Has Direction but no Symbol
```

---

## 📋 **Implementation Logic Structure**

### **Main Filter Function:**

```python
def is_trading_signal(message_text: str) -> bool:
    """
    Determine if message is a trading signal.
    Returns True if signal, False if non-signal.
    """
    
    # Phase 1: Hard Exclusion (Fast Reject)
    if should_exclude_message(message_text):
        return False
    
    # Phase 2: Required Pattern Detection
    has_symbol = detect_symbol_pattern(message_text)
    has_direction = detect_direction_indicator(message_text)
    has_entry = detect_entry_pattern(message_text)
    has_targets = detect_targets_pattern(message_text)
    has_stop_loss = detect_stop_loss_pattern(message_text)
    
    # Phase 3: Signal Validation
    # Minimum requirements: Symbol + Direction + (Entry OR Targets OR SL)
    if not has_symbol:
        return False
    
    if not has_direction:
        return False
    
    # Must have at least one trading data component
    has_trading_data = has_entry or has_targets or has_stop_loss
    if not has_trading_data:
        return False
    
    # High confidence: Has Entry + (Targets OR SL)
    if has_entry and (has_targets or has_stop_loss):
        return True
    
    # Medium confidence: Has Targets + SL (even without explicit Entry)
    if has_targets and has_stop_loss:
        return True
    
    # Accept if has all core components
    return True
```

---

### **Pattern Detection Functions:**

```python
def should_exclude_message(text: str) -> bool:
    """Hard exclusion - reject obvious non-signals."""
    
    exclusion_patterns = [
        r"All entry targets achieved",
        r"All take-profit targets achieved",
        r"target \d+ ✅",  # "target 5 ✅"
        r"Take-Profit target \d+ ✅",
        r"Profit:\s*[\d.]+%\s*Period:",  # Completed trade pattern
        r"achieved (😎|✅)",
    ]
    
    for pattern in exclusion_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    # Personal messages (if no trading data)
    if re.match(r'^I[\'m\s]*(ve|am|want|decided|motivated)', text, re.IGNORECASE):
        # Only exclude if doesn't contain trading data
        if not (contains_symbol(text) or contains_trading_keywords(text)):
            return True
    
    return False

def detect_symbol_pattern(text: str) -> bool:
    """Detect cryptocurrency symbol in various formats."""
    
    symbol_patterns = [
        r'#[A-Z]{2,10}(?:USDT|/USDT)?',      # #GUNUSDT, #FHE, #XTZ/USDT
        r'\b[A-Z]{2,10}USDT\b',              # ETHUSDT, UNIUSDT, MASKUSDT
        r'\b[A-Z]{2,10}/USDT\b',             # CLO/USDT, MELANIA/USDT
        r'\b[A-Z]{2,10}\(USDT\)',            # GUN(USDT)
        r'Symbol:\s*([A-Z]{2,10}USDT)',      # Symbol: ETHUSDT
        r'COIN NAME:\s*([A-Z]{2,10})',       # COIN NAME: GUN
    ]
    
    return any(re.search(p, text, re.IGNORECASE) for p in symbol_patterns)

def detect_direction_indicator(text: str) -> bool:
    """Detect trading direction (LONG/SHORT)."""
    
    direction_patterns = [
        r'\b(LONG|SHORT|BUY|SELL)\b',
        r'(Trade Type|Signal Type|Type)\s*[:\-]\s*(Long|Short)',
        r'Opening\s+(LONG|SHORT)',
        r'(LONG|SHORT)\s+SETUP',
        r'#(LONG|SHORT)',  # #LONG, #SHORT
    ]
    
    return any(re.search(p, text, re.IGNORECASE) for p in direction_patterns)

def detect_entry_pattern(text: str) -> bool:
    """Detect entry price/zone."""
    
    entry_patterns = [
        r'Entry\s*(?:zone|Price|Targets?|Orders?)?\s*[:\-]?\s*[\d.]+',
        r'Entry\s*[:\-]\s*[\d.]+',
        r'Entries?:\s*[\d.]+',
        r'Entry\s+price\s*[:\-]?\s*[\d.]+',
        r'ENTRY\s+PRICE\s*\([^)]+\)',  # ENTRY PRICE (0.02734-0.02650)
    ]
    
    return any(re.search(p, text, re.IGNORECASE) for p in entry_patterns)

def detect_targets_pattern(text: str) -> bool:
    """Detect take-profit targets."""
    
    target_patterns = [
        r'Target\s*\d*[:\-]?\s*[\d.$]+',      # Target 1: 0.02375, Target: $0.04160
        r'Targets?\s*[:\-]?\s*[\d.$]+',       # Targets: 0.02375
        r'Take[- ]?Profit\s*(?:Targets?)?',   # Take-Profit, Take Profit Targets
        r'\bTP\d*\b',                         # TP, TP1, TP2
        r'TP\d*:\s*[\d.]+',                   # TP1: 0.00308
        r'\d+[️⃣)\-]\s*[\d.]+',              # 1️⃣ 0.02765, 2) 0.02880
    ]
    
    return any(re.search(p, text, re.IGNORECASE) for p in target_patterns)

def detect_stop_loss_pattern(text: str) -> bool:
    """Detect stop loss."""
    
    stop_patterns = [
        r'Stop[- ]?Loss',                     # Stop Loss, Stop-Loss, Stop loss
        r'\bSL\b',                            # SL
        r'\bSTOP\b',                          # STOP
        r'Stoploss',                          # Stoploss
        r'Stop\s+loss\s*[:\-]?\s*[\d.$]+',   # Stop loss :0.62702
        r'SL:\s*[\d.]+',                      # SL: 3013.430400
        r'STOP\s*:\s*[\d.$]+',               # STOP : $0.03920
        r'Stop[- ]?Loss\s*[:\-]?\s*[\d.$]+', # Stop Loss -0.02234
    ]
    
    return any(re.search(p, text, re.IGNORECASE) for p in stop_patterns)

def contains_trading_keywords(text: str) -> bool:
    """Check if text contains any trading-related keywords."""
    keywords = ['entry', 'target', 'tp', 'stop', 'loss', 'leverage', 'symbol', 'trade']
    return any(keyword in text.lower() for keyword in keywords)
```

---

## ✅ **Signal Classification Examples**

### **✅ Valid Signals (Forward - All Your Examples):**

```
✅ "#GUNUSDT: #LONG Entry zone... Targets... Stop loss..."
   → Has: Symbol + Direction + Entry + Targets + SL ✅

✅ "🟢 Opening LONG Symbol: ETHUSDT Entry: 3138.9900 Targets... SL..."
   → Has: Direction + Symbol + Entry + Targets + SL ✅

✅ "Exchange: BingX #MELANIA/USDT SHORT Entries: 0.1525 Targets... Stoploss..."
   → Has: Symbol + Direction + Entry + Targets + SL ✅

✅ "#FHE LONG SETUP Target 1... Target 2... STOP..."
   → Has: Symbol + Direction + Targets + SL ✅

✅ "👍THE SWEDEN SCALPER👍 COIN NAME: GUN(USDT) TRADE TYPE: LONG Entry Price..."
   → Has: Symbol + Direction + Entry + Targets + SL ✅
```

---

### **❌ Non-Signals (Exclude):**

```
❌ "#PARTI/USDT All entry targets achieved"
   → Contains "achieved" → Exclude ❌

❌ "#TA/USDT All take-profit targets achieved 😎"
   → Contains "achieved" → Exclude ❌

❌ "#BULLA/USDT Take-Profit target 5 ✅"
   → Pattern "target X ✅" → Exclude ❌

❌ "I've decided to take the next step and commit to an investment"
   → Personal message, no trading data → Exclude ❌

❌ "I am motivated to start with $600"
   → Personal message, no trading data → Exclude ❌

❌ [Image/Screenshot without text]
   → No text content → Exclude ❌
```

---

## 🎯 **Final Filtering Logic Summary**

### **Exclusion Rules (Hard Reject):**

1. ❌ **Status Updates:**
   - `"All entry targets achieved"`
   - `"All take-profit targets achieved"`
   - `"Take-Profit target X ✅"` or `"target X ✅"`
   - `"Profit: X% Period: [time]"`

2. ❌ **Personal Messages:**
   - Starts with `"I've"`, `"I am"`, `"I want"` (if no trading data)

3. ❌ **Media/Images:**
   - No text content (only images)

---

### **Acceptance Rules (Signal Detection):**

**Required (ALL must be present):**
1. ✅ **Symbol** - `#SYMBOL`, `SYMBOLUSDT`, `SYMBOL/USDT`, or `SYMBOL(USDT)`
2. ✅ **Direction** - `LONG`, `SHORT`, `"Trade Type: Short"`, etc.
3. ✅ **Trading Data** - At least one of:
   - Entry (Entry, Entry zone, Entry Price)
   - Targets (Target, TP, Take-Profit)
   - Stop Loss (SL, Stop Loss, STOP)

**Preferred (Higher Confidence):**
- Has Entry + Targets + SL (complete signal)
- Has Entry + (Targets OR SL) (partial but valid)

---

## 🔧 **Implementation Recommendations**

### **Recommended Approach:**

1. **Simple Version (Start Here):**
   - Exclusion filter for status updates
   - Require: Symbol + Direction + (Entry OR Targets OR SL)
   - This will catch all your example signals

2. **Enhanced Version (If needed):**
   - Add confidence scoring
   - Tune thresholds based on false positives/negatives
   - Handle edge cases

### **Tuning Strategy:**

1. **Start Strict:** Require Symbol + Direction + Entry + (Targets OR SL)
2. **Monitor Results:** Check what gets filtered
3. **Adjust:** Relax if valid signals are missed
4. **Refine:** Add more exclusion patterns if non-signals get through

---

## 📊 **Expected Behavior**

### **Will Forward:**
- ✅ All your signal examples (they all have Symbol + Direction + Entry/Targets/SL)
- ✅ New trading signals with complete data
- ✅ Signals with partial data (e.g., Symbol + Direction + Entry, but no SL)

### **Will Exclude:**
- ❌ Status updates ("achieved" messages)
- ❌ Completed trades ("target X ✅")
- ❌ Personal messages without trading data
- ❌ Images/screenshots
- ❌ News, announcements, advertisements

---

**This refined logic should correctly identify and forward all trading signals while filtering out status updates, news, and other non-signal content!**

