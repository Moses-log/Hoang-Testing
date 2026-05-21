"""
trade_stream.py — Alpaca WebSocket trade update listener.

Subscribes to ALL order fill events from Alpaca.
- TradingView webhook orders: send fill notification with actual fill price.
- Manual Alpaca orders: same notification, labelled "manual".

Runs in a daemon thread alongside the FastAPI server.
Auto-reconnects if the WebSocket drops.
"""

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone

from app.config import settings
from app.notifications import notify_trades_embed as notify_embed, notify_sms
from app import state

log = logging.getLogger(__name__)

_stream_thread: threading.Thread | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_entry_prices: dict = {}  # underlying -> weighted avg buy price
_entry_qtys:   dict = {}  # underlying -> total qty currently held


def _is_paper() -> bool:
    return "paper" in settings.alpaca_base_url.lower()


def _underlying(symbol: str) -> str:
    m = re.match(r"^([A-Z]+)\d", str(symbol))
    return m.group(1) if m else str(symbol)


async def _handle_trade_update(data) -> None:
    """Process a single trade update event from Alpaca."""
    try:
        event = str(getattr(data, "event", "")).lower()
        log.debug("Trade update received: event=%s", event)

        if event not in ("fill", "partial_fill"):
            return

        order    = data.order
        order_id = str(order.id)

        is_webhook = order_id in state.webhook_order_ids
        if is_webhook:
            state.webhook_order_ids.discard(order_id)

        sym      = str(order.symbol)
        side     = str(order.side).lower()
        qty      = float(getattr(data, "qty", None) or order.filled_qty or 0)
        price    = float(getattr(data, "price", None) or order.filled_avg_price or 0)
        capital  = qty * price
        is_buy   = "buy" in side
        is_opt   = len(sym) > 6 and not sym.isalpha()

        underlying = _underlying(sym)
        emoji      = "📈" if is_buy else "📉"
        color      = 0x00B300 if is_buy else 0xCC0000
        cap_lbl    = "Capital Used" if is_buy else "Exit Amount"
        direction  = ("ENTRY" if is_buy else "EXIT") if is_webhook else ("Manually BUY Order" if is_buy else "Manually SELL Order")
        src_note   = f"{'partial ' if 'partial' in event else ''}fill · {'TradingView' if is_webhook else 'manual'}"
        unit       = "contracts" if is_opt else "shares"

        # Cache weighted avg entry price on buys; calculate P&L on sells
        pnl_fields = []
        pnl_sms    = ""
        if is_buy:
            prev_qty   = _entry_qtys.get(underlying, 0.0)
            prev_price = _entry_prices.get(underlying, 0.0)
            new_qty    = prev_qty + qty
            if prev_qty > 0 and prev_price > 0:
                _entry_prices[underlying] = (prev_price * prev_qty + price * qty) / new_qty
            else:
                _entry_prices[underlying] = price
            _entry_qtys[underlying] = new_qty
        else:
            avg_entry = _entry_prices.get(underlying, 0.0)
            if avg_entry <= 0:
                try:
                    from app.trading.alpaca_client import get_client
                    pos = get_client().get_open_position(sym)
                    avg_entry = float(pos.avg_entry_price or 0)
                except Exception:
                    pass
            if avg_entry > 0:
                pnl      = (price - avg_entry) * qty
                pnl_pct  = (price - avg_entry) / avg_entry * 100
                pe_emoji = "🟢" if pnl >= 0 else "🔴"
                pnl_fields = [
                    {"name": "Avg Entry", "value": f"${avg_entry:,.2f}", "inline": True},
                    {"name": "P&L", "value": f"{pe_emoji} **${pnl:+,.2f}** ({pnl_pct:+.2f}%)", "inline": False},
                ]
                pnl_sms = f"\nAvg Entry: ${avg_entry:,.2f}\nP&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
            pos_qty = float(getattr(data, "position_qty", None) or 1)
            if pos_qty == 0:
                _entry_prices.pop(underlying, None)
                _entry_qtys.pop(underlying, None)
            else:
                _entry_qtys[underlying] = pos_qty

        fields = [
            {"name": "Ticker",   "value": f"**{underlying}**",      "inline": True},
            {"name": "Symbol",   "value": sym,                       "inline": True},
            {"name": "Qty",      "value": f"**{qty:,.0f}** {unit}", "inline": True},
            {"name": "Filled @", "value": f"**${price:,.2f}**",     "inline": True},
            {"name": cap_lbl,    "value": f"**${capital:,.2f}**",   "inline": True},
        ] + pnl_fields

        embed = {
            "title":     f"{emoji} {direction} — {sym}",
            "color":     color,
            "fields":    fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer":    {"text": f"Order: {order_id[:16]} | {src_note}"},
        }
        await notify_embed(embed)

        sms = (
            f"{emoji} {direction} — {sym}\n"
            f"Qty: {qty:,.0f} {unit}\n"
            f"Filled @ ${price:,.2f}\n"
            f"{cap_lbl}: ${capital:,.2f}"
            f"{pnl_sms}"
        )
        await notify_sms(sms)

        log.info(
            "Fill notification sent",
            extra={"symbol": sym, "side": side, "qty": qty, "price": price, "source": src_note},
        )

    except Exception as exc:
        log.error("Error handling trade update: %s", exc, exc_info=True)


async def _trade_handler(data) -> None:
    """Async handler required by alpaca-py — runs in the stream thread's event loop.
    Posts the actual notification work onto the main FastAPI event loop."""
    if _main_loop is None or _main_loop.is_closed():
        log.warning("Main event loop unavailable — skipping trade update")
        return
    try:
        future = asyncio.run_coroutine_threadsafe(
            _handle_trade_update(data), _main_loop
        )
        future.result(timeout=15)
    except Exception as exc:
        log.error("Trade update handler error: %s", exc)


def _run_stream_with_reconnect() -> None:
    """Run the stream, auto-reconnecting on failure."""
    from alpaca.trading.stream import TradingStream

    # Each thread needs its own event loop for alpaca-py's async internals
    thread_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(thread_loop)

    while True:
        try:
            log.info("Connecting to Alpaca trade stream (paper=%s)", _is_paper())
            stream = TradingStream(
                api_key=settings.alpaca_api_key,
                secret_key=settings.alpaca_secret_key,
                paper=_is_paper(),
            )
            stream.subscribe_trade_updates(_trade_handler)
            log.info("Trade stream subscribed — waiting for fills")
            stream.run()
            log.warning("Trade stream disconnected — reconnecting in 5s")
        except Exception as exc:
            log.error("Trade stream error: %s", exc, exc_info=True)
            log.info("Reconnecting in 5s...")
        time.sleep(5)


def start_trade_stream(loop: asyncio.AbstractEventLoop) -> None:
    """Start the Alpaca trade stream in a daemon background thread."""
    global _stream_thread, _main_loop
    _main_loop = loop
    _stream_thread = threading.Thread(
        target=_run_stream_with_reconnect,
        daemon=True,
        name="alpaca-trade-stream",
    )
    _stream_thread.start()
    log.info("Trade stream thread started (paper=%s)", _is_paper())


def stop_trade_stream() -> None:
    log.info("Trade stream stopping (process exit)")
