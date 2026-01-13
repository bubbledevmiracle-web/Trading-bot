# Telegram Message Forwarder - First Goal

Single-file solution for monitoring Telegram group channels and forwarding messages to personal channel with template transformation.

## Features

- ✅ Monitors 5 Telegram group channels simultaneously
- ✅ Transforms messages into standardized template format
- ✅ Forwards messages to personal channel
- ✅ Duplicate detection (2-hour TTL)
- ✅ Dry-run mode for safe testing
- ✅ Comprehensive logging
- ✅ Error handling and rate limit management
- ✅ Single clean file, easy to understand

## Installation

1. Install Python 3.8 or higher

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

Edit the configuration section at the top of `telegram_message_forwarder.py`:

```python
# API Credentials (already configured)
API_ID = 27590479
API_HASH = "6e60321cbb996b499b6a370af62342de"
PHONE_NUMBER = "+46 70 368 9310"

# Dry Run Mode (set to False to actually send messages)
DRY_RUN = False

# Timezone
TIMEZONE = "Europe/Stockholm"  # or "UTC"
```

## Usage

### First Run (Authentication)

1. Run the script:
```bash
python telegram_message_forwarder.py
```

2. You will receive a code via Telegram. Enter it when prompted.

3. If 2FA is enabled, enter your password when prompted.

4. Session file (`telegram_session.session`) will be created for future runs.

### Normal Operation

Simply run:
```bash
python telegram_message_forwarder.py
```

### Dry Run Mode (Testing)

Set `DRY_RUN = True` in the script to test without actually sending messages:
- Messages will be detected and logged
- Template format will be shown in logs
- No messages will be sent to personal channel

## Monitored Channels

1. CRYPTORAKETEN (ID: 1002290339976)
2. SMART_CRYPTO (ID: 1002339729195)
3. Ramos Crypto (@ramoscryptotrading)
4. SWE Crypto (@cryptosignalandanalyse)
5. Hassan tahnon (@hassantahnon0)

## Message Template Format

Messages are transformed into this format:

```
✅ Signal mottagen & kopierad
🕒 Tid: 2026-01-08 14:30:00
📢 Från kanal: CRYPTORAKETEN
📊 Meddelande:
[Original message text here]
```

## Logging

Logs are written to:
- Console output (stdout)
- File: `logs/telegram_forwarder.log`

## Troubleshooting

### "Channel is private" or "Not a participant"
- Ensure you are a member of all source channels
- Verify channel IDs/usernames are correct

### "Cannot access personal channel"
- Verify personal channel ID is correct
- Ensure you have access to the personal channel

### Authentication Issues
- Delete `telegram_session.session` file and try again
- Verify API credentials are correct

### Rate Limiting
- The script automatically handles Telegram rate limits
- Wait times are logged automatically

## Stopping the Bot

Press `Ctrl+C` to gracefully stop the bot.

## Next Steps

After testing with `DRY_RUN = True`:
1. Verify messages are detected correctly
2. Check template format is correct
3. Set `DRY_RUN = False`
4. Monitor logs for any issues

## Notes

- Only text messages are processed (media messages are skipped for now)
- Duplicate messages within 2 hours are automatically filtered
- The bot must be running continuously to monitor channels
- Session file stores authentication (keep it secure)

