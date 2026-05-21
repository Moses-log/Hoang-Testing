"""
notifications.py — Optional Discord / Telegram / SMS alert stubs.

Set DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
to enable those channels. For SMS set SMS_GMAIL_USER, SMS_GMAIL_APP_PASSWORD,
and SMS_TO (carrier email gateway, e.g. 5551234567@vtext.com).

All functions are fire-and-forget — they log errors but never raise so
that a notification failure never blocks order execution.
"""

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def notify(message: str) -> None:
    """Send a notification to all configured channels."""
    if settings.discord_general_webhook_url:
        await _discord(message)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await _telegram(message)


async def _discord(message: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                settings.discord_general_webhook_url,
                json={"content": message[:2000]},  # Discord 2 000-char limit
            )
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


async def notify_embed(embed: dict, webhook_url: str | None = None) -> None:
    """Send a rich Discord embed to a specific channel URL, or fall back to the general webhook."""
    url = webhook_url or settings.discord_general_webhook_url
    if not url:
        log.warning("No Discord webhook URL configured — skipping embed notification")
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"embeds": [embed]})
    except Exception as exc:
        log.warning("Discord embed notification failed: %s", exc)


async def notify_pnl_embed(embed: dict) -> None:
    """Send a daily P&L embed to #daily-p-l-logs (falls back to general webhook)."""
    await notify_embed(embed, webhook_url=settings.discord_daily_pnl_webhook_url or settings.discord_general_webhook_url)


async def notify_weekly_pnl_embed(embed: dict) -> None:
    """Send a weekly P&L embed (falls back to general webhook)."""
    await notify_embed(embed, webhook_url=settings.discord_weekly_pnl_webhook_url or settings.discord_general_webhook_url)


async def notify_monthly_pnl_embed(embed: dict) -> None:
    """Send a monthly P&L embed (falls back to general webhook)."""
    await notify_embed(embed, webhook_url=settings.discord_monthly_pnl_webhook_url or settings.discord_general_webhook_url)


async def notify_yearly_pnl_embed(embed: dict) -> None:
    """Send a yearly P&L embed (falls back to general webhook)."""
    await notify_embed(embed, webhook_url=settings.discord_yearly_pnl_webhook_url or settings.discord_general_webhook_url)


async def notify_trades_embed(embed: dict) -> None:
    """Send a trade fill embed to #trade-reports (falls back to general webhook)."""
    await notify_embed(embed, webhook_url=settings.discord_trades_webhook_url or settings.discord_general_webhook_url)


async def notify_sms(message: str) -> None:
    """SMS notifications disabled."""
    return


async def _telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        f"/sendMessage"
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message[:4096],  # Telegram 4 096-char limit
                    "parse_mode": "HTML",
                },
            )
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)
