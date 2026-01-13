# Implementation Plan - Telegram Message Forwarder

## ✅ Confirmed Information

### Channels to Monitor (Confirmed - 5 channels):
1. **CRYPTORAKETEN** - ID: `1002290339976` (numeric ID)
2. **SMART_CRYPTO** - ID: `1002339729195` (numeric ID)
3. **Ramos Crypto** - Username: `@ramoscryptotrading`
4. **SWE Crypto** - Username: `@cryptosignalandanalyse`
5. **Hassan tahnon** - Username: `@hassantahnon0`

**Note**: LUX_LEAK is NOT included. We're monitoring 5 channels instead of the original 3 from bot spec.

### Personal Channel:
- **Personal Channel ID**: 1003179263982 ✅

### API Credentials:
- **Phone**: +46 70 368 9310 ✅
- **API ID**: 27590479 ✅
- **API Hash**: 6e60321cbb996b499b6a370af62342de ✅

### First Goal:
- Monitor 3 group channels (CRYPTORAKETEN, LUX_LEAK, SMART_CRYPTO)
- Forward messages to personal channel (1003179263982)
- Transform messages into template format

---

## 📋 Implementation Plan

### **Step 1: Setup & Dependencies**
- Install Pyrogram library
- Create configuration structure
- Set up authentication (session file)

### **Step 2: Channel Connection**
- Connect to Telegram using Pyrogram
- Authenticate with phone number
- Verify access to all 3 source channels
- Verify access to personal channel

### **Step 3: Message Monitoring**
- Listen for new messages from the 3 channels
- Filter message types (text messages)
- Capture: message text, timestamp, channel name, message ID

### **Step 4: Template Transformation**
Use the **"Signal received & copied"** template format:

```
✅ Signal mottagen & kopierad
🕒 Tid: {{timestamp}}
📢 Från kanal: {{channel_name}}
📊 Meddelande:
{{original_message_text}}
```

**Note**: Since we don't have example messages yet, I'll start with basic formatting. If messages contain trading data (symbol, entry, TP, SL), we can enhance parsing later.

### **Step 5: Message Publishing**
- Send transformed message to personal channel (1003179263982)
- Handle rate limits gracefully
- Add error handling and retry logic

### **Step 6: Error Handling**
- Connection loss handling (auto-reconnect)
- Channel access errors
- Message sending failures
- Basic logging

### **Step 7: Basic Deduplication** (Future Enhancement)
- Store message hashes
- Prevent re-processing same message
- 2-hour TTL (as per bot spec)

---

## 🔧 Technical Approach

### **Single File Structure** (`telegram_message_forwarder.py`):

```python
# Configuration section
# Pyrogram setup
# Channel IDs
# Template definitions
# Message handler function
# Main loop
```

### **Key Components**:

1. **Configuration**:
   - API credentials
   - Channel IDs (3 source + 1 personal)
   - Template format

2. **Pyrogram Client**:
   - Authentication
   - Message handlers
   - Connection management

3. **Template Engine**:
   - Format messages according to template
   - Fill placeholders (timestamp, channel name, message text)

4. **Error Handling**:
   - Try-catch blocks
   - Retry logic
   - Logging to file

---

## ⚠️ Missing Information Needed

### **Before Development Can Start:**

1. **LUX_LEAK Channel ID/Username** ⚠️
   - Do you have the channel ID or username (@lux_leak)?
   - Can verify during first run if you provide

2. **Example Messages** (Optional but Recommended)
   - 2-3 sample messages from any source channel
   - Helps design better parsing if needed
   - Not critical for basic version

3. **Timezone Preference**
   - UTC (default) or Europe/Stockholm?
   - For timestamp formatting

---

## 📝 File Structure

```
trading_bot_2026_01_08/
├── telegram_message_forwarder.py  (single file - main script)
├── .env  (optional - for credentials if you prefer)
└── logs/  (auto-created for logging)
```

---

## 🚀 Development Steps

1. **Create single Python file** with all logic
2. **Add Pyrogram setup** and authentication
3. **Implement message monitoring** for 3 channels
4. **Create template transformation** function
5. **Add message publishing** to personal channel
6. **Implement error handling** and logging
7. **Test with one channel** first, then expand
8. **Document usage** in comments

---

## ✅ Ready to Proceed?

**If you approve this plan**, I'll create the single-file solution with:
- ✅ Clean, easy-to-understand code
- ✅ Pyrogram implementation
- ✅ Template formatting
- ✅ Error handling
- ✅ Basic logging
- ✅ Comments explaining each section

**Or if you want to provide:**
- LUX_LEAK channel ID/username
- Example messages (optional)
- Any other requirements

I can start development immediately!

---

## 📌 Next Steps

**Please confirm:**
1. ✅ Approve this implementation plan?
2. ⚠️ Provide LUX_LEAK channel ID/username (or can we verify at runtime)?
3. ❓ Any changes to the plan?

Once confirmed, I'll create `telegram_message_forwarder.py`!

