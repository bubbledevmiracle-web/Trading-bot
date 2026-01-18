#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 6 - Telegram Logging Helpers
==================================
Centralized wrappers for sending Telegram messages while logging telemetry.

Author: Trading Bot Project
Date: 2026-01-16
"""

from __future__ import annotations

from typing import Optional

from stage6_telemetry import TelemetryLogger, TelemetryCorrelation, _stable_hash


async def send_telegram_with_telemetry(
    *,
    telegram_client,
    chat_id: str,
    text: str,
    telemetry: Optional[TelemetryLogger],
    correlation: Optional[TelemetryCorrelation] = None,
) -> Optional[int]:
    """
    Send a Telegram message and log success/failure deterministically.
    Returns telegram message id if available.
    """
    text_norm = (text or "").strip()
    msg_hash = _stable_hash(text_norm)

    if telemetry is not None:
        telemetry.emit(
            event_type="TELEGRAM_SEND_ATTEMPT",
            level="INFO",
            subsystem="TELEGRAM",
            message="Send attempt",
            correlation=correlation,
            payload={"chat_id": str(chat_id), "message_hash": msg_hash, "text_len": len(text_norm)},
        )

    try:
        m = await telegram_client.send_message(chat_id=chat_id, text=text_norm)
        mid = getattr(m, "id", None)
        if telemetry is not None:
            telemetry.emit(
                event_type="TELEGRAM_SEND_OK",
                level="INFO",
                subsystem="TELEGRAM",
                message="Send ok",
                correlation=TelemetryCorrelation(
                    ssot_id=getattr(correlation, "ssot_id", None),
                    bot_order_id=getattr(correlation, "bot_order_id", None),
                    bingx_order_id=getattr(correlation, "bingx_order_id", None),
                    position_id=getattr(correlation, "position_id", None),
                    telegram_chat_id=str(chat_id),
                    telegram_message_id=int(mid) if mid is not None else None,
                ),
                payload={"chat_id": str(chat_id), "message_hash": msg_hash},
            )
        return int(mid) if mid is not None else None
    except Exception as e:
        if telemetry is not None:
            telemetry.emit(
                event_type="TELEGRAM_SEND_ERROR",
                level="ERROR",
                subsystem="TELEGRAM",
                message=str(e),
                correlation=TelemetryCorrelation(
                    ssot_id=getattr(correlation, "ssot_id", None),
                    bot_order_id=getattr(correlation, "bot_order_id", None),
                    bingx_order_id=getattr(correlation, "bingx_order_id", None),
                    position_id=getattr(correlation, "position_id", None),
                    telegram_chat_id=str(chat_id),
                    telegram_message_id=None,
                ),
                payload={"chat_id": str(chat_id), "message_hash": msg_hash},
            )
        raise


