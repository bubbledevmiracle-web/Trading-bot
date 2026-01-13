#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helper script to identify a Telegram channel by ID
"""

import asyncio
import sys

# Fix for Windows Python 3.8+ event loop issue
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client

# Your API credentials
API_ID = 27590479
API_HASH = "6e60321cbb996b499b6a370af62342de"
PHONE_NUMBER = "+46 70 368 9310"
SESSION_FILE = "telegram_session"

# Channel ID to identify
CHANNEL_ID_TO_FIND = "-1003427538616"

async def identify_channel():
    """Identify a channel by its ID."""
    app = Client(
        SESSION_FILE,
        api_id=API_ID,
        api_hash=API_HASH,
        phone_number=PHONE_NUMBER
    )
    
    await app.start()
    
    print("\n" + "="*60)
    print(f"Identifying channel: {CHANNEL_ID_TO_FIND}")
    print("="*60 + "\n")
    
    try:
        # Try to get channel info
        chat = await app.get_chat(CHANNEL_ID_TO_FIND)
        
        print(f"✅ Channel found!")
        print(f"   Title: {chat.title}")
        print(f"   ID: {chat.id}")
        if chat.username:
            print(f"   Username: @{chat.username}")
        print(f"   Type: {chat.type}")
        if hasattr(chat, 'members_count'):
            print(f"   Members: {chat.members_count}")
        print()
        
    except Exception as e:
        print(f"❌ Cannot access channel: {e}")
        print()
        print("This channel is:")
        print("  - Not in your session cache")
        print("  - OR you're not a member")
        print("  - OR it's a private channel you haven't accessed")
        print()
        print("To identify it:")
        print("  1. Open Telegram app")
        print("  2. Check all your channels/groups")
        print("  3. Look for channels you're subscribed to but not monitoring")
        print()
    
    # Also list all your channels to help identify
    print("\n" + "="*60)
    print("Your monitored channels:")
    print("="*60)
    monitored = {
        "CRYPTORAKETEN": "-1002290339976",
        "SMART_CRYPTO": "-1002339729195",
        "Ramos Crypto": "-1002972812097",
        "SWE Crypto": "-1002234181057",
        "Hassan tahnon": "-1003598458712"
    }
    for name, ch_id in monitored.items():
        print(f"  {name}: {ch_id}")
    
    print("\n" + "="*60)
    print("The error channel ID does NOT match any monitored channel!")
    print("="*60)
    print(f"Error ID: {CHANNEL_ID_TO_FIND}")
    print("This is likely a channel you're subscribed to but not monitoring.")
    print("These errors are harmless and can be ignored.")
    print()
    
    await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(identify_channel())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
        sys.exit(0)

