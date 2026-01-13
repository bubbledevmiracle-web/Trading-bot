# Step-by-Step Testing Guide - Telegram Message Forwarder

## 🎯 Overview

This guide will walk you through testing the Telegram Message Forwarder step by step. Follow each step in order to verify all functionality.

---

## 📋 Pre-Testing Checklist

Before you begin, ensure you have:

- [ ] Python 3.8+ installed
- [ ] Pyrogram installed (`pip install pyrogram`)
- [ ] Access to all 5 source Telegram channels
- [ ] Access to your personal channel (ID: 1003179263982)
- [ ] Telegram account credentials ready (phone: +46 70 368 9310)
- [ ] `telegram_message_forwarder.py` file in your project directory

---

## 🚀 Step 1: Verify Installation & Configuration

### **1.1 Check Python Installation**

Open terminal/command prompt and run:

```bash
python --version
```

**Expected Result:** Python 3.8 or higher

**If Error:** Install Python from [python.org](https://www.python.org/)

---

### **1.2 Check Pyrogram Installation**

Run:

```bash
python -m pip show pyrogram
```

**Expected Result:** Shows Pyrogram package info with version

**If Error:** Install with `python -m pip install pyrogram`

---

### **1.3 Verify Configuration File**

Open `telegram_message_forwarder.py` and check these values are correct:

- [ ] `API_ID = 27590479`
- [ ] `API_HASH = "6e60321cbb996b499b6a370af62342de"`
- [ ] `PHONE_NUMBER = "+46 70 368 9310"`
- [ ] `DRY_RUN = True` (for initial testing)
- [ ] `PERSONAL_CHANNEL_ID = "1003179263982"`
- [ ] All 5 source channels are listed correctly

---

## 🔐 Step 2: First Run - Authentication

### **2.1 Run the Script for First Time**

In terminal, navigate to project directory and run:

```bash
cd d:\project\trading_bot_2026_01_08
python telegram_message_forwarder.py
```

---

### **2.2 Authentication Process**

**What Will Happen:**

1. **Script starts** and shows:
   ```
   ============================================================
   Telegram Message Forwarder - First Goal
   ============================================================
   API ID: 27590479
   Phone: +46 70 368 9310
   Source Channels: 5
   Personal Channel: 1003179263982
   Dry Run: True
   Timezone: Europe/Stockholm
   ============================================================
   ```

2. **Telegram will send you a code** to your phone (+46 70 368 9310)

3. **Enter the code** when prompted:
   ```
   Enter the code you received: [ENTER CODE HERE]
   ```

4. **If 2FA is enabled**, you'll be asked for password:
   ```
   Enter password (2FA): [ENTER PASSWORD]
   ```

5. **Session file created**: `telegram_session.session` file will be created

---

### **2.3 Verify Authentication Success**

**Look for these success messages:**

```
✅ Telegram client started successfully
✅ Personal channel verified: [Channel Name]
✅ CRYPTORAKETEN: [Channel Name]
✅ SMART_CRYPTO: [Channel Name]
✅ Ramos Crypto: [Channel Name]
✅ SWE Crypto: [Channel Name]
✅ Hassan tahnon: [Channel Name]
============================================================
🚀 Bot is running and monitoring channels...
📊 Dry Run Mode: ENABLED
============================================================

Monitoring for messages... Press Ctrl+C to stop
```

**If you see warnings (⚠️):**
- Check that you're a member of those channels
- Verify channel IDs/usernames are correct

---

### **2.4 Stop the Script**

Press `Ctrl+C` to stop the script gracefully.

**Expected Message:**
```
🛑 Shutdown requested by user
✅ Telegram client stopped
👋 Goodbye!
```

---

## 📝 Step 3: Test Channel Verification

### **3.1 Verify Channel Access**

Run the script again:

```bash
python telegram_message_forwarder.py
```

### **3.2 Check Each Channel**

Look at the startup messages. For each channel, you should see:

- **✅ Green checkmark** = Channel accessible
- **⚠️ Warning** = Need to check access

**For warnings, verify:**
1. You are a member of the channel
2. Channel ID/username is correct
3. You have permission to read messages

---

## 🧪 Step 4: Test Message Detection (Dry Run Mode)

### **4.1 Ensure Dry Run is Enabled**

In `telegram_message_forwarder.py`, verify:

```python
DRY_RUN = True   # Production: False, Test: True
```

---

### **4.2 Start the Script**

```bash
python telegram_message_forwarder.py
```

Keep the script running in the terminal.

---

### **4.3 Send Test Messages**

**From another device/app:**
1. Go to one of the source channels (e.g., CRYPTORAKETEN)
2. Send a test message: `"Test message from CRYPTORAKETEN"`
3. Wait 2-5 seconds

---

### **4.4 Check Script Output**

**Look for these messages in the terminal:**

```
📨 New message from CRYPTORAKETEN
🔍 [DRY RUN] Would send message:

✅ Signal mottagen & kopierad
🕒 Tid: 2026-01-08 14:30:00
📢 Från kanal: CRYPTORAKETEN
📊 Meddelande:
Test message from CRYPTORAKETEN
```

**✅ Success Indicators:**
- Message detected ✅
- Template format correct ✅
- Channel name correct ✅
- Timestamp correct ✅
- Original message text included ✅

---

### **4.5 Test Multiple Channels**

Repeat Step 4.3-4.4 for each channel:

- [ ] CRYPTORAKETEN
- [ ] SMART_CRYPTO
- [ ] Ramos Crypto (@ramoscryptotrading)
- [ ] SWE Crypto (@cryptosignalandanalyse)
- [ ] Hassan tahnon (@hassantahnon0)

**Expected:** Each message should be detected and formatted correctly.

---

## 🎨 Step 5: Verify Template Format

### **5.1 Check Template Elements**

For each detected message, verify the template contains:

1. **✅ Emoji and "Signal mottagen & kopierad"**
2. **🕒 Timestamp** - Format: `YYYY-MM-DD HH:MM:SS`
3. **📢 Channel name** - Should match source channel
4. **📊 "Meddelande:" label**
5. **Original message text** - Full text from source

---

### **5.2 Verify Timestamp**

Check that:
- [ ] Timestamp is in correct format
- [ ] Timezone is correct (Europe/Stockholm or UTC as configured)
- [ ] Time is recent (within seconds of actual time)

---

### **5.3 Test Different Message Types**

Send different types of messages:

1. **Short message:** `"BTCUSDT LONG"`
2. **Long message:** `"ETHUSDT SHORT Entry: 2000 TP1: 2100 TP2: 2200 SL: 1950"`
3. **Message with emojis:** `"🚀 IMX LONG"`
4. **Message with special characters:** `"SOL/USDT @ $100"`

**Expected:** All should be formatted correctly with template.

---

## 🔄 Step 6: Test Duplicate Detection

### **6.1 Send First Message**

1. From a source channel, send: `"Duplicate test message"`
2. Wait for detection in script

---

### **6.2 Send Same Message Again**

Within 2 hours:
1. Send the exact same message again from the same channel
2. Wait for detection

**Expected Result:**
- First message: Detected and processed ✅
- Second message: Detected but marked as duplicate (not processed again) ✅

**Look for:**
```
Duplicate message from [CHANNEL], skipping
```

---

## 🔌 Step 7: Test Connection Resilience

### **7.1 Start Script**

Run the script and let it monitor channels.

### **7.2 Simulate Disconnect**

1. **Disconnect your internet** for 30 seconds
2. **Reconnect internet**
3. Wait 10-15 seconds

**Expected Behavior:**
- Script should automatically reconnect
- Continue monitoring without manual intervention
- No errors or crashes

**Look for reconnection messages in logs.**

---

## 📊 Step 8: Review Logs

### **8.1 Check Log File**

Open the log file:
```
logs/telegram_forwarder.log
```

### **8.2 Verify Log Contents**

Check that logs contain:
- [ ] All startup information
- [ ] Channel verification results
- [ ] Each detected message
- [ ] Template formatting details
- [ ] Any errors (should be none in normal operation)
- [ ] Timestamps for all events

---

## ✅ Step 9: Production Test (Send Actual Messages)

### **⚠️ IMPORTANT: Only do this after all dry-run tests pass!**

### **9.1 Disable Dry Run Mode**

Edit `telegram_message_forwarder.py`:

```python
DRY_RUN = False   # Production: False, Test: True
```

### **9.2 Start Script**

```bash
python telegram_message_forwarder.py
```

### **9.3 Send Test Message**

From a source channel, send: `"Production test message"`

### **9.4 Verify Message in Personal Channel**

1. Open your personal Telegram channel (ID: 1003179263982)
2. Check for the forwarded message
3. Verify it matches the template format exactly

**Expected:** Message appears in personal channel within 5 seconds.

---

### **9.5 Verify Format in Personal Channel**

Check the received message contains:

```
✅ Signal mottagen & kopierad
🕒 Tid: [timestamp]
📢 Från kanal: [channel name]
📊 Meddelande:
[original message text]
```

---

## 🐛 Step 10: Troubleshooting Common Issues

### **Issue 1: "Channel is private" or "Not a participant"**

**Solution:**
1. Verify you are a member of all source channels
2. Check channel IDs/usernames in configuration
3. Try accessing channels manually in Telegram app

---

### **Issue 2: "Cannot access personal channel"**

**Solution:**
1. Verify personal channel ID: `1003179263982`
2. Ensure you have admin/access rights to the channel
3. Check channel is not archived or deleted

---

### **Issue 3: "Event loop" error**

**Solution:**
This should be fixed in the code, but if it appears:
1. Verify the event loop fix is in the code (lines 13-25)
2. Try updating Python: `python -m pip install --upgrade pip`

---

### **Issue 4: Messages not detected**

**Possible Causes:**
1. Not a member of source channel
2. Script not running
3. Channel ID/username incorrect

**Check:**
- Verify script is running
- Check startup messages show all channels verified
- Test with a message you send yourself

---

### **Issue 5: Duplicate messages not filtered**

**Check:**
- Verify duplicate detection is working (Step 6)
- Check message content is exactly the same
- Verify within 2-hour window

---

## 📋 Step 11: Final Verification Checklist

After completing all steps, verify:

- [ ] ✅ Authentication successful (session file created)
- [ ] ✅ All 5 channels verified and accessible
- [ ] ✅ Messages detected from all channels
- [ ] ✅ Template format correct for all messages
- [ ] ✅ Channel names correctly identified
- [ ] ✅ Timestamps accurate
- [ ] ✅ Duplicate detection working
- [ ] ✅ Connection resilience tested
- [ ] ✅ Logs being written correctly
- [ ] ✅ Production test successful (messages forwarded)
- [ ] ✅ Messages appear in personal channel
- [ ] ✅ No errors or crashes during testing

---

## 🎯 Step 12: Production Deployment

### **12.1 Final Configuration**

Before production:
- [ ] Set `DRY_RUN = False`
- [ ] Verify all channel IDs/usernames
- [ ] Check timezone setting
- [ ] Review log directory permissions

### **12.2 Start Production**

```bash
python telegram_message_forwarder.py
```

### **12.3 Monitor First Hour**

- [ ] Monitor logs for any errors
- [ ] Verify messages are being forwarded
- [ ] Check personal channel for forwarded messages
- [ ] Verify no duplicate messages
- [ ] Check script is running continuously

---

## 📊 Step 13: Continuous Monitoring

### **Daily Checks:**
- [ ] Script is running (check process)
- [ ] Messages being forwarded
- [ ] Review logs for errors
- [ ] Check disk space (log files)

### **Weekly Checks:**
- [ ] Review error rates
- [ ] Check for duplicate messages
- [ ] Verify all channels still accessible
- [ ] Review log file size

---

## 🆘 Need Help?

If you encounter issues not covered here:

1. **Check the log file:** `logs/telegram_forwarder.log`
2. **Review error messages** in terminal
3. **Verify all configuration** matches your setup
4. **Test with one channel** first before all 5

---

## ✅ Success Criteria

Your testing is successful when:

1. ✅ All 5 channels are verified
2. ✅ Messages are detected from all channels
3. ✅ Template format is correct
4. ✅ Messages are forwarded to personal channel (production mode)
5. ✅ No errors in logs
6. ✅ Script runs continuously without crashes
7. ✅ Duplicate detection works
8. ✅ Connection auto-recovery works

---

**Good luck with your testing! 🚀**

