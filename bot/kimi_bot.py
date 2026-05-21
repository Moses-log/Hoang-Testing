"""
kimi_bot.py — Kimi No-Stop-Loss trading bot for Render deployment.

Subscribes to live 1-minute bars via Alpaca WebSocket, calculates
Bollinger Bands locally, and places orders directly via Alpaca API.
Sends Discord notifications for entries, exits, and proximity alerts.

Configure via environment variables (see app/config.py for all settings).
Key env vars:
  BOT_SYMBOLS       — comma-separated list e.g. "QQQ,SPY,NVDA"
  BOT_DTBP_USAGE    — fraction of day-trading buying power to use (0.5 = 50%)
  BOT_BB_LENGTH     — Bollinger Band period (default 20)
  BOT_STOP_LOSS_PCT — stop loss % below entry (default 50)
"""

import json
import logging
import os
import signal
import sys
from collections import deque
from datetime import datetime, date, timedelta, time as dtime

import numpy as np
import pandas as pd
import pytz
import requests

from alpaca.data.live import StockDataStream
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from app.config import settings

log = logging.getLogger("KimiBot")

# =============================================================================
# CONFIGURATION — all driven by env vars via settings
# =============================================================================
SYMBOLS         = [s.strip().upper() for s in settings.bot_symbols.split(",") if s.strip()]
BB_LENGTH       = settings.bot_bb_length
BB_MULT         = settings.bot_bb_mult
EXIT_BAND_PCT   = settings.bot_exit_band_pct
STOP_LOSS_PCT   = settings.bot_stop_loss_pct
LEVERAGE_FACTOR = settings.bot_leverage_factor
LEV_BAR_GAP     = settings.bot_lev_bar_gap
BB_ALERT_PCT    = settings.bot_bb_alert_pct
DTBP_USAGE      = settings.bot_dtbp_usage
LEV1_ALLOC      = 1.0
BARS_MAXLEN     = 500

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kimi_state.json")

CST           = pytz.timezone("America/Chicago")
UTC           = pytz.UTC
SESSION_START = dtime(8, 30)
SESSION_END   = dtime(15, 0)
ENTRY_START   = dtime(8, 45)
ENTRY_END     = dtime(14, 0)

PAPER = "paper" in settings.alpaca_base_url.lower()

# =============================================================================
# DISCORD NOTIFICATIONS (synchronous — bot runs in sync/thread context)
# =============================================================================
def _discord(message: str) -> None:
    url = settings.discord_general_webhook_url
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:2000]}, timeout=5)
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)


def _discord_embed(embed: dict) -> None:
    url = settings.discord_trades_webhook_url or settings.discord_general_webhook_url
    if not url:
        return
    try:
        requests.post(url, json={"embeds": [embed]}, timeout=5)
    except Exception as exc:
        log.warning("Discord embed failed: %s", exc)


def _entry_embed(symbol: str, price: float, qty: int, capital: float, stop: float) -> None:
    _discord_embed({
        "title":  f"📈 BOT ENTRY — {symbol}",
        "color":  0x00B300,
        "fields": [
            {"name": "Price",   "value": f"**${price:,.2f}**",  "inline": True},
            {"name": "Shares",  "value": f"**{qty:,}**",         "inline": True},
            {"name": "Capital", "value": f"**${capital:,.0f}**", "inline": True},
            {"name": "Stop",    "value": f"**${stop:,.2f}**",    "inline": True},
        ],
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": f"Kimi Bot · {'Paper' if PAPER else 'Live'}"},
    })


def _exit_embed(symbol: str, price: float, qty: int, entry: float, pnl: float, pnl_pct: float, trade_n: int) -> None:
    icon = "🟢" if pnl >= 0 else "🔴"
    _discord_embed({
        "title":  f"📉 BOT EXIT — {symbol}",
        "color":  0x00B300 if pnl >= 0 else 0xCC0000,
        "fields": [
            {"name": "Exit",    "value": f"**${price:,.2f}**",                          "inline": True},
            {"name": "Shares",  "value": f"**{qty:,}**",                                 "inline": True},
            {"name": "Entry",   "value": f"**${entry:,.2f}**",                           "inline": True},
            {"name": "P&L",     "value": f"{icon} **${pnl:+,.2f}** ({pnl_pct:+.2f}%)", "inline": True},
            {"name": "Trade #", "value": str(trade_n),                                  "inline": True},
        ],
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": f"Kimi Bot · {'Paper' if PAPER else 'Live'}"},
    })


def _stop_embed(symbol: str, price: float, qty: int, entry: float, pnl: float, pnl_pct: float) -> None:
    _discord_embed({
        "title":  f"🛑 BOT STOP LOSS — {symbol}",
        "color":  0xCC0000,
        "fields": [
            {"name": "Exit",   "value": f"**${price:,.2f}**",                        "inline": True},
            {"name": "Shares", "value": f"**{qty:,}**",                               "inline": True},
            {"name": "Entry",  "value": f"**${entry:,.2f}**",                         "inline": True},
            {"name": "P&L",    "value": f"🔴 **${pnl:+,.2f}** ({pnl_pct:+.2f}%)",   "inline": True},
        ],
        "timestamp": datetime.now(UTC).isoformat(),
        "footer": {"text": f"Kimi Bot · {'Paper' if PAPER else 'Live'}"},
    })


# =============================================================================
# ALPACA CLIENTS
# =============================================================================
trading_client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=PAPER)
hist_client    = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)
stream         = StockDataStream(settings.alpaca_api_key, settings.alpaca_secret_key, feed=DataFeed.IEX)


# =============================================================================
# BUYING POWER
# =============================================================================
def get_dtbp() -> float:
    try:
        account = trading_client.get_account()
        dtbp    = float(account.daytrading_buying_power)
        log.info("💳 Day Trading BP: $%,.0f", dtbp)
        return dtbp
    except Exception as exc:
        log.error("Failed to get DTBP: %s", exc)
        return 0.0


INITIAL_CAPITAL    = get_dtbp() * DTBP_USAGE
CAPITAL_PER_SYMBOL = INITIAL_CAPITAL / len(SYMBOLS) if SYMBOLS else 1.0


# =============================================================================
# STATE PER SYMBOL
# =============================================================================
def make_state() -> dict:
    return {
        "bars":             deque(maxlen=BARS_MAXLEN),
        "lev1_entry_price": None,
        "lev1_entry_qty":   0,
        "has_lev1":         False,
        "last_lev_bar":     -999,
        "bar_index":        0,
        "lev1_deployed":    0.0,
        "net_profit":       0.0,
        "trade_count":      0,
        "session_pnl":      0.0,
        "alerted_lower":    False,
        "alerted_mid":      False,
        "alerted_upper":    False,
    }


states = {sym: make_state() for sym in SYMBOLS}

SAVE_KEYS = [
    "lev1_entry_price", "lev1_entry_qty", "has_lev1",
    "last_lev_bar", "bar_index", "lev1_deployed",
    "net_profit", "trade_count", "session_pnl",
]


# =============================================================================
# STATE PERSISTENCE
# =============================================================================
def save_state() -> None:
    try:
        data = {sym: {k: states[sym][k] for k in SAVE_KEYS} for sym in SYMBOLS}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.error("Failed to save state: %s", exc)


def load_state() -> None:
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for sym in SYMBOLS:
            if sym in saved:
                for k in SAVE_KEYS:
                    if k in saved[sym]:
                        states[sym][k] = saved[sym][k]
        log.info("✅ State restored from file")
        for sym in SYMBOLS:
            s = states[sym]
            if s["has_lev1"]:
                log.info("   [%s] Open position | Entry=$%s | Qty=%s | P&L=$%.2f",
                         sym, s["lev1_entry_price"], s["lev1_entry_qty"], s["net_profit"])
    except FileNotFoundError:
        log.info("No saved state — starting fresh")
    except Exception as exc:
        log.error("Failed to load state: %s", exc)


# =============================================================================
# HISTORICAL BAR LOADER
# =============================================================================
def fetch_bars(symbol: str, start, end) -> list:
    try:
        bars = hist_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            session="regular",
            feed="iex",
        )).df
        if bars.empty:
            return []
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.loc[symbol]
        return bars[["open", "high", "low", "close", "volume"]].to_dict("records")
    except Exception as exc:
        log.error("[%s] fetch_bars failed: %s", symbol, exc)
        return []


def preload_bars(symbol: str) -> None:
    bars = fetch_bars(symbol, date.today() - timedelta(days=30), datetime.now(UTC))
    if not bars:
        log.warning("[%s] No bars to preload", symbol)
        return
    s = states[symbol]
    for b in bars:
        s["bars"].append(b["close"])
        s["bar_index"] += 1
    basis, upper, lower = calc_bb(s["bars"])
    if basis:
        log.info("[%s] ✅ Preloaded %d bars | Last=$%.2f | BB[%.2f/%.2f/%.2f]",
                 symbol, len(bars), bars[-1]["close"], lower, basis, upper)
    else:
        log.warning("[%s] Preloaded %d bars — BB still warming up", symbol, len(bars))


# =============================================================================
# REPLAY YESTERDAY (restore signal state across restarts)
# =============================================================================
def replay_yesterday(symbol: str) -> None:
    today = date.today()
    bars  = fetch_bars(symbol, today - timedelta(days=7), today - timedelta(days=1))
    if not bars:
        log.warning("[%s] No yesterday bars to replay", symbol)
        return

    try:
        last_date = pd.to_datetime([b.get("timestamp", i) for i, b in enumerate(bars)]).normalize().max()
        bars = [b for i, b in enumerate(bars)
                if pd.to_datetime(b.get("timestamp", i)).normalize() == last_date]
    except Exception:
        pass

    s = states[symbol]
    if s["has_lev1"]:
        log.info("[%s] Position already restored — skipping replay", symbol)
        return

    stop_mult = 1 - STOP_LOSS_PCT / 100
    for b in bars:
        c          = b["close"]
        bar_gap_ok = (s["bar_index"] - s["last_lev_bar"]) >= LEV_BAR_GAP
        basis, upper, lower = calc_bb(s["bars"])
        if basis is None:
            s["bars"].append(c)
            s["bar_index"] += 1
            continue
        exit_level = basis + (upper - basis) * EXIT_BAND_PCT

        if not s["has_lev1"] and c <= lower and bar_gap_ok:
            qty = max(1, int(buying_power(s) * LEVERAGE_FACTOR * LEV1_ALLOC / c))
            s.update({"lev1_entry_qty": qty, "lev1_entry_price": c,
                       "lev1_deployed": qty * c, "has_lev1": True,
                       "last_lev_bar": s["bar_index"], "session_pnl": 0.0})
            log.info("[%s] 📼 REPLAY ENTRY $%.2f x%d", symbol, c, qty)

        elif s["has_lev1"] and s["lev1_entry_price"]:
            if c > exit_level and c > s["lev1_entry_price"] * 1.0003:
                pnl = (c - s["lev1_entry_price"]) * s["lev1_entry_qty"]
                s["session_pnl"] += pnl; s["net_profit"] += pnl; s["trade_count"] += 1
                s["lev1_deployed"] = 0.0
                s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None})
                log.info("[%s] 📼 REPLAY EXIT $%.2f P&L=$%.2f", symbol, c, pnl)
            elif c <= s["lev1_entry_price"] * stop_mult:
                pnl = (c - s["lev1_entry_price"]) * s["lev1_entry_qty"]
                s["session_pnl"] += pnl; s["net_profit"] += pnl
                s["lev1_deployed"] = 0.0
                s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None})
                log.info("[%s] 📼 REPLAY STOP $%.2f P&L=$%.2f", symbol, c, pnl)

        s["bars"].append(c)
        s["bar_index"] += 1

    # Reconcile with Alpaca live position
    actual_qty, actual_price = get_position_details(symbol)
    if s["has_lev1"] and actual_qty == 0:
        log.warning("[%s] ⚠️ Replay open but Alpaca flat — resetting", symbol)
        s.update({"has_lev1": False, "lev1_entry_qty": 0,
                  "lev1_entry_price": None, "lev1_deployed": 0.0})
    elif not s["has_lev1"] and actual_qty > 0:
        log.warning("[%s] ⚠️ Alpaca has position but replay flat — syncing", symbol)
        s.update({"has_lev1": True, "lev1_entry_qty": int(actual_qty),
                  "lev1_entry_price": actual_price, "lev1_deployed": actual_qty * actual_price})

    log.info("[%s] ✅ Replay done | has_lev1=%s | P&L=$%.2f", symbol, s["has_lev1"], s["net_profit"])


# =============================================================================
# GRACEFUL SHUTDOWN
# =============================================================================
def shutdown(sig, frame):
    log.info("🛑 Kimi Bot shutting down...")
    save_state()
    _discord("🛑 Kimi Bot stopped")
    sys.exit(0)


signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)


# =============================================================================
# HELPERS
# =============================================================================
def now_cst() -> datetime:
    return datetime.now(CST)

def in_session() -> bool:
    t = now_cst().time()
    return SESSION_START <= t <= SESSION_END

def in_entry_window() -> bool:
    t = now_cst().time()
    return ENTRY_START <= t <= ENTRY_END

def calc_bb(closes):
    if len(closes) < BB_LENGTH:
        return None, None, None
    arr   = np.array(list(closes)[-BB_LENGTH:])
    basis = arr.mean()
    dev   = arr.std(ddof=0) * BB_MULT
    return basis, basis + dev, basis - dev

def buying_power(s: dict) -> float:
    return max(0.0, CAPITAL_PER_SYMBOL + s["net_profit"] - s["lev1_deployed"])

def get_position(symbol: str) -> float:
    try:
        return float(trading_client.get_open_position(symbol).qty)
    except Exception:
        return 0.0

def get_position_details(symbol: str):
    try:
        pos = trading_client.get_open_position(symbol)
        return float(pos.qty), float(pos.avg_entry_price)
    except Exception:
        return 0.0, 0.0

def place_order(symbol: str, side: OrderSide, qty: int, note: str = "") -> bool:
    if qty <= 0:
        return False
    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=int(qty), side=side, time_in_force=TimeInForce.DAY,
        ))
        log.info("✅ [%s] %s %d | %s | id=%s", symbol, side.value.upper(), int(qty), note, order.id)
        save_state()
        return True
    except Exception as exc:
        log.error("❌ [%s] Order failed: %s", symbol, exc)
        return False


# =============================================================================
# POSITION SYNC — detects manual trades on Alpaca
# =============================================================================
def sync_position(symbol: str, current_price: float) -> None:
    s = states[symbol]
    qty, avg_price = get_position_details(symbol)

    if not s["has_lev1"] and qty > 0:
        log.warning("[%s] 🔄 Manual entry detected | Qty=%d @ $%.2f", symbol, qty, avg_price)
        s.update({"has_lev1": True, "lev1_entry_qty": int(qty),
                  "lev1_entry_price": avg_price, "lev1_deployed": qty * avg_price,
                  "alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
        _discord(f"🔄 **{symbol}** manual entry synced — {int(qty)} shares @ ${avg_price:.2f}")
        save_state()

    elif s["has_lev1"] and qty == 0:
        pnl = (current_price - s["lev1_entry_price"]) * s["lev1_entry_qty"] if s["lev1_entry_price"] else 0
        log.warning("[%s] 🔄 Manual exit detected | Est P&L=$%.2f", symbol, pnl)
        s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None,
                  "lev1_deployed": 0.0, "last_lev_bar": -999,
                  "alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
        _discord(f"🔄 **{symbol}** manual exit detected | Est P&L: ${pnl:+,.2f}")
        save_state()

    elif s["has_lev1"] and qty > 0 and int(qty) != s["lev1_entry_qty"]:
        log.warning("[%s] 🔄 Qty mismatch | Bot=%d Alpaca=%d — syncing", symbol, s["lev1_entry_qty"], int(qty))
        s["lev1_entry_qty"] = int(qty)
        s["lev1_deployed"]  = qty * avg_price
        save_state()


# =============================================================================
# SIGNAL LOGIC — called on every confirmed 1-min bar
# =============================================================================
def process_bar(symbol: str, bar: dict) -> None:
    s = states[symbol]
    s["bar_index"] += 1
    c = bar["close"]
    s["bars"].append(c)

    basis, upper, lower = calc_bb(s["bars"])
    if basis is None:
        log.info("[%s] Warming up BB (%d/%d)", symbol, len(s["bars"]), BB_LENGTH)
        return

    exit_level = basis + (upper - basis) * EXIT_BAND_PCT
    stop_mult  = 1 - STOP_LOSS_PCT / 100
    bar_gap_ok = (s["bar_index"] - s["last_lev_bar"]) >= LEV_BAR_GAP

    sync_position(symbol, c)
    position = get_position(symbol)

    log.info("[%s] #%d C=%.2f Lo=%.2f Mid=%.2f Hi=%.2f Pos=%s BP=$%,.0f",
             symbol, s["bar_index"], c, lower, basis, upper, position, buying_power(s))

    # ── BB Proximity Alerts ───────────────────────────────────────────────────
    if not s["has_lev1"]:
        near_lower = c > lower and (c - lower) / lower <= BB_ALERT_PCT
        if near_lower and not s["alerted_lower"]:
            log.warning("[%s] ⚠️ Approaching lower BB | $%.2f → lower $%.2f", symbol, c, lower)
            _discord(f"⚠️ **{symbol}** approaching lower BB | ${c:.2f} (lower: ${lower:.2f})")
            s["alerted_lower"] = True
        elif not near_lower:
            s["alerted_lower"] = False
    else:
        near_mid = c < basis and (basis - c) / basis <= BB_ALERT_PCT
        if near_mid and not s["alerted_mid"]:
            log.warning("[%s] 🎯 Approaching middle BB | $%.2f → mid $%.2f", symbol, c, basis)
            _discord(f"🎯 **{symbol}** approaching middle BB — prepare to exit | ${c:.2f}")
            s["alerted_mid"] = True
        elif not near_mid:
            s["alerted_mid"] = False

        near_upper = c < upper and (upper - c) / upper <= BB_ALERT_PCT
        if near_upper and not s["alerted_upper"]:
            log.warning("[%s] 🚀 Approaching upper BB | $%.2f → upper $%.2f", symbol, c, upper)
            _discord(f"🚀 **{symbol}** approaching upper BB | ${c:.2f}")
            s["alerted_upper"] = True
        elif not near_upper:
            s["alerted_upper"] = False

    if not in_session():
        return

    entry_allowed = in_entry_window()

    # ── 1. Entry ──────────────────────────────────────────────────────────────
    if not s["has_lev1"] and position == 0 and c <= lower and bar_gap_ok and entry_allowed:
        bp  = buying_power(s)
        qty = max(1, int(bp * LEVERAGE_FACTOR * LEV1_ALLOC / c))
        ok  = place_order(symbol, OrderSide.BUY, qty, note="ENTRY")

        if not ok:
            real_dtbp = get_dtbp()
            real_qty  = int(real_dtbp / c)
            log.warning("[%s] Retrying with live DTBP=$%,.0f → %d shares", symbol, real_dtbp, real_qty)
            if real_qty > 0:
                ok = place_order(symbol, OrderSide.BUY, real_qty, note="ENTRY RETRY")
                if ok:
                    qty = real_qty
            if not ok:
                _discord(f"🚫 **{symbol}** entry failed — insufficient buying power at ${c:.2f}")

        if ok:
            stop_price = c * stop_mult
            s.update({"lev1_entry_qty": qty, "lev1_entry_price": c,
                       "lev1_deployed": qty * c, "has_lev1": True,
                       "last_lev_bar": s["bar_index"], "session_pnl": 0.0,
                       "alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
            log.info("▶ [%s] ENTRY $%.2f x%d | Stop=$%.2f | BP=$%,.0f", symbol, c, qty, stop_price, bp)
            _entry_embed(symbol, c, qty, bp, stop_price)

    # ── 2. Exit (profit) ──────────────────────────────────────────────────────
    if s["has_lev1"] and s["lev1_entry_price"] and c > exit_level and c > s["lev1_entry_price"] * 1.0003:
        qty     = s["lev1_entry_qty"]
        pnl_usd = (c - s["lev1_entry_price"]) * qty
        pnl_pct = (c - s["lev1_entry_price"]) / s["lev1_entry_price"] * 100
        s["session_pnl"] += pnl_usd
        s["net_profit"]  += pnl_usd
        s["trade_count"] += 1
        s["lev1_deployed"] = 0.0
        s.update({"alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
        place_order(symbol, OrderSide.SELL, qty, note="EXIT")
        log.info("⬇ [%s] EXIT $%.2f P/L=%.2f%% $%.2f Trade#%d", symbol, c, pnl_pct, pnl_usd, s["trade_count"])
        _exit_embed(symbol, c, qty, s["lev1_entry_price"], pnl_usd, pnl_pct, s["trade_count"])
        s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None})

    # ── 3. Stop Loss ──────────────────────────────────────────────────────────
    elif s["has_lev1"] and s["lev1_entry_price"] and c <= s["lev1_entry_price"] * stop_mult:
        qty     = s["lev1_entry_qty"]
        pnl_usd = (c - s["lev1_entry_price"]) * qty
        pnl_pct = (c - s["lev1_entry_price"]) / s["lev1_entry_price"] * 100
        s["session_pnl"] += pnl_usd
        s["net_profit"]  += pnl_usd
        s["lev1_deployed"] = 0.0
        s.update({"alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
        place_order(symbol, OrderSide.SELL, qty, note="STOP")
        log.warning("🛑 [%s] STOP $%.2f P/L=%.2f%% $%.2f", symbol, c, pnl_pct, pnl_usd)
        _stop_embed(symbol, c, qty, s["lev1_entry_price"], pnl_usd, pnl_pct)
        s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None})

    # ── Safety reset ──────────────────────────────────────────────────────────
    if get_position(symbol) == 0:
        s.update({"has_lev1": False, "lev1_entry_qty": 0, "lev1_entry_price": None,
                  "lev1_deployed": 0.0, "last_lev_bar": -999,
                  "alerted_lower": False, "alerted_mid": False, "alerted_upper": False})
        save_state()


# =============================================================================
# WEBSOCKET HANDLER
# =============================================================================
async def on_bar(bar) -> None:
    symbol = bar.symbol
    if symbol not in states:
        return
    process_bar(symbol, {
        "open": float(bar.open), "high": float(bar.high),
        "low":  float(bar.low),  "close": float(bar.close),
        "volume": float(bar.volume),
    })


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%m/%d %I:%M:%S %p",
        handlers=[logging.StreamHandler()],
    )

    log.info("🚀 Kimi Bot starting | Paper=%s", PAPER)
    log.info("   Symbols (%d): %s", len(SYMBOLS), ", ".join(SYMBOLS))
    log.info("   DTBP %.0f%% | Capital $%,.0f | Per symbol $%,.0f",
             DTBP_USAGE * 100, INITIAL_CAPITAL, CAPITAL_PER_SYMBOL)
    log.info("   BB(%d, %.1f) | Stop=%.0f%% | BarGap=%d | AlertPct=%.4f%%",
             BB_LENGTH, BB_MULT, STOP_LOSS_PCT, LEV_BAR_GAP, BB_ALERT_PCT * 100)

    load_state()

    log.info("📊 Preloading 30 days of bars...")
    for sym in SYMBOLS:
        preload_bars(sym)

    log.info("📼 Replaying yesterday for signal state...")
    for sym in SYMBOLS:
        replay_yesterday(sym)

    save_state()

    _discord(
        f"🚀 **Kimi Bot started** | {'Paper' if PAPER else 'Live'} | "
        f"{len(SYMBOLS)} symbols | Capital: ${INITIAL_CAPITAL:,.0f}"
    )

    stream.subscribe_bars(on_bar, *SYMBOLS)
    stream.run()
