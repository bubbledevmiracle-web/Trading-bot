# Signal Filtering Logic - Implementation Proposal

## 🎯 Problem Statement

Channels contain:
- ✅ Trading signals (need to forward)
- ❌ News updates (should skip)
- ❌ Announcements (should skip)
- ❌ General chat (should skip)
- ❌ Other non-signal content (should skip)

**Goal:** Only forward messages that are trading signals, filter out everything else.

---

## 🔍 Signal Detection Approaches

### **Approach 1: Keyword-Based Filtering (Recommended for Start)**

**Logic:**
Messages that contain trading-related keywords are considered signals.

**Signal Indicators:**
- Must contain at least 2-3 of these:
  - Direction keywords: `LONG`, `SHORT`, `BUY`, `SELL`
  - Trading terms: `Entry`, `Entry:`, `TP`, `TP1`, `TP2`, `SL`, `Stop Loss`
  - Symbol format: Contains pattern like `BTCUSDT`, `ETHUSDT`, `USDT`, or `#BTC`
  - Action words: `Opening`, `Signal`, `Trade`, `Position`

**Non-Signal Indicators (Exclude if contains):**
- `News`, `Update`, `Announcement`, `Important`, `Notice`
- `Maintenance`, `System`, `Bug fix`, `Update available`
- Patterns like dates-only, links-only without trading data
- Very short messages (< 20 characters)

**Pros:**
- ✅ Simple to implement
- ✅ Fast execution
- ✅ Easy to tune
- ✅ Works for most signal formats

**Cons:**
- ⚠️ Might miss signals with unusual format
- ⚠️ Might include false positives

---

### **Approach 2: Pattern-Based Detection (More Accurate)**

**Logic:**
Look for specific patterns that indicate trading signals.

**Required Patterns (Signal must have):**
1. **Symbol Pattern:**
   - Contains trading pair: `[SYMBOL]USDT` or `#SYMBOL`
   - Examples: `BTCUSDT`, `ETHUSDT`, `#BTC`, `#IMX`

2. **Direction Pattern:**
   - Contains: `LONG`, `SHORT`, `BUY`, `SELL`
   - Or directional emojis: `📈` (LONG), `📉` (SHORT)

3. **Entry Pattern:**
   - Contains: `Entry`, `Entry:`, `Entry price`, `Entry: [number]`

**Optional Patterns (Strengthen signal probability):**
- TP patterns: `TP`, `TP1`, `TP2`, `Take Profit`
- SL patterns: `SL`, `Stop Loss`, `Stop:`
- Price patterns: Numbers that look like prices
- Leverage: `x5`, `x10`, `leverage`

**Signal Confidence Scoring:**
- High confidence: Has symbol + direction + entry
- Medium confidence: Has symbol + direction (or entry)
- Low confidence: Only has one indicator → skip

---

### **Approach 3: Hybrid Approach (Best Balance)**

**Logic:**
Combine keyword detection with pattern matching and heuristics.

**Step 1: Quick Exclusion Filter**
- Skip messages that are clearly not signals:
  - Contains: `News`, `Update`, `Announcement`
  - Very short (< 20 chars) without trading keywords
  - Only links/URLs without trading data
  - Only emojis without text

**Step 2: Signal Pattern Matching**
- Must contain at least ONE of:
  - Symbol pattern: `[A-Z]{2,10}USDT` or `#[A-Z]{2,10}`
  - Entry pattern: `Entry:` followed by number
  - Direction keywords: `LONG`, `SHORT`, `BUY`, `SELL`

**Step 3: Signal Validation**
- If contains symbol → high priority (likely signal)
- If contains `Entry` + `TP` or `SL` → high priority
- If contains direction + price → medium priority
- If only has generic words → low priority, skip

---

## 📋 Recommended Implementation Logic

### **Phase 1: Exclusion Filter (Fast Reject)**

**Skip message if:**
```
IF message contains ANY of:
  - "News" or "Update" or "Announcement"
  - "Maintenance" or "System"
  - Only URLs/links without trading text
  - Message length < 20 characters (and no trading keywords)
THEN skip (not a signal)
```

### **Phase 2: Signal Detection (Required Patterns)**

**Accept message if:**
```
IF message contains ALL of:
  - Symbol pattern (BTCUSDT, ETHUSDT, #BTC, etc.)
  - Direction indicator (LONG/SHORT/BUY/SELL)
  - Entry or TP or SL indicator
  
OR if message contains:
  - "Opening LONG" or "Opening SHORT"
  - Symbol + Entry price
  - Symbol + Direction + Price
  
THEN accept as signal (forward)
```

### **Phase 3: Confidence Scoring (Optional Enhancement)**

**Score-based acceptance:**
```
Score = 0

IF contains symbol pattern: score += 3
IF contains direction (LONG/SHORT): score += 2
IF contains Entry: score += 2
IF contains TP: score += 1
IF contains SL: score += 1
IF contains price/number: score += 1

IF score >= 4: Forward message (high confidence)
IF score >= 2: Forward message (medium confidence - can tune threshold)
IF score < 2: Skip message (low confidence)
```

---

## 🎨 Signal Format Examples (From Your Logs)

**Good Signals (Should Forward):**
```
✅ "🟢 Opening LONG 📈
Symbol: TNSRUSDT
Entry: 0.074840
TP1: 0.076861
SL: 0.070724"

✅ "🔵 Opening SHORT 📉
Symbol: STBLUSDT
Entry: 0.057250"
```

**Bad Messages (Should Skip):**
```
❌ "Channel update: New features added"
❌ "Important announcement: Maintenance scheduled"
❌ "News: Market analysis for today"
❌ "Hello everyone, welcome to the channel"
```

---

## 🔧 Implementation Strategy

### **Step 1: Create Signal Detector Function**

```python
def is_trading_signal(message_text: str) -> bool:
    """
    Determine if message is a trading signal.
    Returns True if signal, False otherwise.
    """
    
    # Step 1: Quick exclusion
    exclude_keywords = ["News", "Update", "Announcement", "Maintenance"]
    if any(keyword in message_text for keyword in exclude_keywords):
        return False
    
    # Step 2: Check for required patterns
    has_symbol = check_symbol_pattern(message_text)
    has_direction = check_direction_pattern(message_text)
    has_entry_tp_sl = check_trading_patterns(message_text)
    
    # Step 3: Signal validation
    if has_symbol and (has_direction or has_entry_tp_sl):
        return True
    
    return False
```

### **Step 2: Pattern Matching Functions**

```python
def check_symbol_pattern(text: str) -> bool:
    # Pattern: SYMBOLUSDT or #SYMBOL
    # Regex: [A-Z]{2,10}USDT|#[A-Z]{2,10}
    # Or simple: contains "USDT" or "#" followed by letters
    pass

def check_direction_pattern(text: str) -> bool:
    # Check for: LONG, SHORT, BUY, SELL
    # Or emojis: 📈, 📉
    pass

def check_trading_patterns(text: str) -> bool:
    # Check for: Entry, TP, SL, Take Profit, Stop Loss
    pass
```

### **Step 3: Integration**

```python
async def handle_new_message(self, client: Client, message: Message):
    # ... existing code ...
    
    # NEW: Check if message is a trading signal
    if not is_trading_signal(message_text):
        logger.debug(f"Skipping non-signal message from {channel_name}")
        return  # Skip non-signal messages
    
    # Continue with existing template transformation and sending
    # ...
```

---

## 📊 Tuning & Configuration

### **Configurable Parameters:**

```python
# Signal Detection Configuration
SIGNAL_KEYWORDS = {
    "direction": ["LONG", "SHORT", "BUY", "SELL"],
    "trading": ["Entry", "TP", "TP1", "TP2", "SL", "Stop Loss", "Take Profit"],
    "action": ["Opening", "Signal", "Trade", "Position"],
    "exclude": ["News", "Update", "Announcement", "Maintenance", "System"]
}

# Pattern Configuration
SYMBOL_PATTERN = r"[A-Z]{2,10}USDT|#[A-Z]{2,10}"
MIN_SIGNAL_CONFIDENCE = 3  # Minimum score to forward
```

### **Adjustment Strategy:**
1. Start with strict filtering (high confidence only)
2. Monitor what gets filtered
3. Adjust patterns based on false positives/negatives
4. Tune confidence threshold

---

## 🧪 Testing Approach

### **Test Cases Needed:**

1. **True Positives (Should Forward):**
   - Message with symbol + direction + entry
   - Message with "Opening LONG" + symbol
   - Message with TP/SL patterns

2. **True Negatives (Should Skip):**
   - News messages
   - Announcements
   - General chat
   - Links without trading data

3. **Edge Cases:**
   - Messages with partial signal data
   - Messages with symbol but no direction
   - Mixed content (signal + news)

---

## 💡 Recommended Approach

**Start with: Hybrid Approach (Approach 3)**

**Rationale:**
1. ✅ Balances accuracy with simplicity
2. ✅ Easy to tune and adjust
3. ✅ Can start strict, then relax if needed
4. ✅ Handles most common signal formats
5. ✅ Filters out obvious non-signals quickly

**Implementation Priority:**
1. **Phase 1:** Quick exclusion filter (fast reject)
2. **Phase 2:** Basic pattern matching (symbol + direction)
3. **Phase 3:** Confidence scoring (refinement)

**Fallback Option:**
- If filtering is too strict (misses signals):
  - Lower confidence threshold
  - Add more keywords to accept list
- If filtering is too loose (includes non-signals):
  - Raise confidence threshold
  - Add more exclusion keywords

---

## 🔄 Iterative Improvement

### **After Initial Implementation:**
1. Monitor forwarded messages for 1-2 days
2. Check for false positives (non-signals forwarded)
3. Check for false negatives (signals missed)
4. Adjust patterns based on real data
5. Fine-tune confidence scores

---

## 📝 Summary

**Proposed Logic:**
1. **Exclusion Filter:** Quick reject of obvious non-signals (news, announcements)
2. **Pattern Detection:** Look for symbol + direction + entry patterns
3. **Confidence Scoring:** Optional scoring system for ambiguous cases
4. **Tunable Thresholds:** Easy to adjust based on results

**Key Patterns to Detect:**
- Symbol formats: `BTCUSDT`, `#BTC`, etc.
- Direction: `LONG`, `SHORT`, `BUY`, `SELL`
- Trading data: `Entry`, `TP`, `SL`

**This approach will:**
- ✅ Filter out news, updates, announcements
- ✅ Forward only trading signals
- ✅ Be adjustable based on actual channel content
- ✅ Handle various signal formats

