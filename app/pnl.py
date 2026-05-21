"""
pnl.py — P&L reports (intraday, weekly, monthly, yearly) sent to Discord + SMS.

All four reports share the same structure:
  - Per-ticker realized P&L (FIFO cost-basis matching)
  - Open positions with delta, capital, unrealized today + total
  - Capital invested summary
  - Overall P&L vs capital
  - Period P&L % vs SPY
  - Equity (start → now with % delta + all-time gain excl. deposits)
"""

import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Deque, List, Optional, Tuple

import pytz

from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from app.config import settings
from app.notifications import (
    notify_pnl_embed,
    notify_weekly_pnl_embed,
    notify_monthly_pnl_embed,
    notify_yearly_pnl_embed,
    notify_sms,
)
from app.trading.alpaca_client import get_client, get_portfolio_history

log = logging.getLogger(__name__)
_ET = pytz.timezone("America/New_York")


# ── SPY benchmark ──────────────────────────────────────────────────────────────

def compute_spy_pct(period: str) -> Optional[float]:
    try:
        import yfinance as yf
        interval_map = {"1d": "1m", "5d": "1d", "1mo": "1d", "1y": "1wk"}
        hist = yf.Ticker("SPY").history(period=period, interval=interval_map.get(period, "1d"))
        if hist.empty:
            return None
        open_price  = float(hist["Open"].iloc[0])
        close_price = float(hist["Close"].iloc[-1])
        return (close_price - open_price) / open_price * 100 if open_price else None
    except Exception as exc:
        log.warning("SPY fetch failed: %s", exc)
        return None


def _spy_line(portfolio_pct: float, spy_pct: Optional[float]) -> str:
    if spy_pct is None:
        return ""
    diff      = portfolio_pct - spy_pct
    diff_word = "ahead of" if diff >= 0 else "behind"
    sign      = "+" if spy_pct >= 0 else ""
    return f"SPY: {sign}{spy_pct:.2f}%  ({'+' if diff >= 0 else ''}{diff:.2f}% {diff_word} SPY)"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _underlying(symbol: str) -> str:
    m = re.match(r"^([A-Z]+)\d", str(symbol))
    return m.group(1) if m else str(symbol)


def _is_option(symbol: str) -> bool:
    return len(symbol) > 6 and not symbol.isalpha()


def _fill_time(order) -> Optional[datetime]:
    t = order.filled_at or order.updated_at
    if t is None:
        return None
    if hasattr(t, "tzinfo") and t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _get_net_deposits(client) -> Optional[float]:
    """Sum all cash transfers to compute net deposits (excl. trading P&L)."""
    try:
        from alpaca.trading.requests import GetActivitiesRequest
        activities = client.get_activities(GetActivitiesRequest(
            activity_types=["CSR", "CSD", "JNLC"],
        ))
        net = sum(
            float(getattr(a, "net_amount", None) or getattr(a, "amount", None) or 0)
            for a in activities
        )
        return net if net != 0.0 else None
    except Exception as exc:
        log.warning("Could not fetch deposit history: %s", exc)
        return None


# ── Shared computation helpers ─────────────────────────────────────────────────

def _fifo_realized(client, period_start: datetime, lookback_days: int) -> Dict[str, dict]:
    """
    FIFO cost-basis matching over `lookback_days` of order history.
    Counts realized P&L only for sells on or after `period_start`.
    Returns dict: ticker → {realized, buy_qty, sell_qty}
    """
    history_start = period_start - timedelta(days=lookback_days)
    all_orders = client.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=history_start,
        limit=500,
    ))
    all_orders = sorted(
        all_orders,
        key=lambda o: _fill_time(o) or datetime.min.replace(tzinfo=timezone.utc),
    )

    buy_lots: Dict[str, Deque[Tuple[float, float]]] = {}
    pnl: Dict[str, dict] = {}

    for o in all_orders:
        if "filled" not in str(o.status).lower():
            continue
        sym   = str(o.symbol)
        qty   = float(o.filled_qty or 0)
        price = float(o.filled_avg_price or 0)
        side  = str(o.side).lower()
        mult  = 100 if _is_option(sym) else 1
        ft    = _fill_time(o)

        if "buy" in side:
            buy_lots.setdefault(sym, deque()).append((qty, price))

        elif "sell" in side:
            in_period = ft is not None and ft >= period_start
            remaining = qty
            lots = buy_lots.get(sym, deque())
            matched_pnl = 0.0

            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                match_qty = min(remaining, lot_qty)
                if in_period:
                    matched_pnl += (price - lot_price) * match_qty * mult
                remaining -= match_qty
                if lot_qty <= match_qty:
                    lots.popleft()
                else:
                    lots[0] = (lot_qty - match_qty, lot_price)

            if remaining > 0 and in_period:
                matched_pnl += price * remaining * mult
                log.warning("No buy lot for %s x%.0f — showing proceeds only", sym, remaining)

            if in_period:
                ticker = _underlying(sym)
                e = pnl.setdefault(ticker, {"realized": 0.0, "buy_qty": 0.0, "sell_qty": 0.0})
                e["realized"]  += matched_pnl
                e["sell_qty"]  += qty

    # Count buys in period
    for o in all_orders:
        if "filled" not in str(o.status).lower():
            continue
        if "buy" not in str(o.side).lower():
            continue
        ft = _fill_time(o)
        if ft and ft >= period_start:
            ticker = _underlying(str(o.symbol))
            e = pnl.setdefault(ticker, {"realized": 0.0, "buy_qty": 0.0, "sell_qty": 0.0})
            e["buy_qty"] += float(o.filled_qty or 0)

    return pnl


def _fetch_open_positions(client) -> Tuple[
    Dict[str, float], Dict[str, float], Dict[str, float], Dict[str, dict]
]:
    """Returns (open_pnl, open_intraday_pnl, open_capital, open_prices)."""
    open_pnl: Dict[str, float]          = {}
    open_intraday_pnl: Dict[str, float] = {}
    open_capital: Dict[str, float]      = {}
    open_prices: Dict[str, dict]        = {}

    for p in client.get_all_positions():
        ticker = _underlying(str(p.symbol))
        cost_basis    = float(p.cost_basis or 0)
        avg_entry     = float(p.avg_entry_price or 0)
        current_price = float(p.current_price or 0)
        open_pnl[ticker]          = open_pnl.get(ticker, 0.0)          + float(p.unrealized_pl or 0)
        open_intraday_pnl[ticker] = open_intraday_pnl.get(ticker, 0.0) + float(p.unrealized_intraday_pl or 0)
        open_capital[ticker]      = open_capital.get(ticker, 0.0)      + cost_basis
        open_prices[ticker]       = {
            "avg_entry":    avg_entry,
            "current_price": current_price,
            "qty":           float(p.qty or 0),
            "intraday_pl":   float(p.unrealized_intraday_pl or 0),
        }

    return open_pnl, open_intraday_pnl, open_capital, open_prices


def _ticker_discord_fields(
    pnl: Dict[str, dict],
    open_pnl: Dict[str, float],
    open_intraday_pnl: Dict[str, float],
    open_capital: Dict[str, float],
    open_prices: Dict[str, dict],
) -> List[dict]:
    """Build per-ticker Discord embed fields (closed + open positions)."""
    fields = []

    # Closed / partially-closed tickers
    for ticker, data in sorted(pnl.items()):
        r        = data["realized"]
        emoji    = "🟢" if r >= 0 else "🔴"
        buy_qty  = data.get("buy_qty", 0.0)
        sell_qty = data.get("sell_qty", 0.0)
        value    = (
            f"Realized: **${r:+,.2f}**\n"
            f"Bought: {buy_qty:,.0f} | Sold: {sell_qty:,.0f} shares"
        )
        # If shares are still held, show full open position details
        if ticker in open_pnl:
            upl         = open_pnl[ticker]
            prices      = open_prices.get(ticker, {})
            avg_e       = prices.get("avg_entry", 0.0)
            cur_p       = prices.get("current_price", 0.0)
            qty         = prices.get("qty", 0.0)
            intraday_pl = prices.get("intraday_pl", 0.0)
            delta       = cur_p - avg_e
            delta_pct   = (delta / avg_e * 100) if avg_e > 0 else 0.0
            d_emoji     = "🟢" if delta >= 0 else "🔴"
            id_emoji    = "🟢" if intraday_pl >= 0 else "🔴"
            value += (
                f"\n─── Still Holding ───\n"
                f"Shares: **{qty:,.0f}** @ **${avg_e:,.2f}** → **${cur_p:,.2f}**\n"
                f"Delta: {d_emoji} **${delta:+,.2f}** ({delta_pct:+.2f}%)\n"
                f"Unrealized Today: {id_emoji} **${intraday_pl:+,.2f}**\n"
                f"Unrealized Total: {d_emoji} **${upl:+,.2f}**"
            )
        fields.append({
            "name":   f"{emoji} {ticker}",
            "value":  value,
            "inline": True,
        })

    # Open-only positions
    for ticker, upl in sorted(open_pnl.items()):
        if ticker in pnl:
            continue
        cap          = open_capital.get(ticker, 0.0)
        prices       = open_prices.get(ticker, {})
        avg_e        = prices.get("avg_entry", 0.0)
        cur_p        = prices.get("current_price", 0.0)
        qty          = prices.get("qty", 0.0)
        intraday_pl  = prices.get("intraday_pl", 0.0)
        delta        = cur_p - avg_e
        delta_pct    = (delta / avg_e * 100) if avg_e > 0 else 0.0
        d_emoji      = "🟢" if delta >= 0 else "🔴"
        id_emoji_pos = "🟢" if intraday_pl >= 0 else "🔴"
        cur_value    = qty * cur_p
        fields.append({
            "name":   f"🟡 {ticker} (open)",
            "value":  (
                f"Shares: **{qty:,.0f}**\n"
                f"Bought @ **${avg_e:,.2f}**  →  Now **${cur_p:,.2f}**\n"
                f"Delta: {d_emoji} **${delta:+,.2f}** ({delta_pct:+.2f}%) per share\n"
                f"Capital: **${cap:,.2f}**  →  Now: **${cur_value:,.2f}**\n"
                f"Unrealized Today: {id_emoji_pos} **${intraday_pl:+,.2f}**\n"
                f"Unrealized Total: {d_emoji} **${upl:+,.2f}**"
            ),
            "inline": True,
        })

    if not fields:
        fields.append({"name": "No activity this period", "value": "No filled orders found.", "inline": False})

    return fields


def _open_positions_discord_fields(
    open_pnl: Dict[str, float],
    open_intraday_pnl: Dict[str, float],
    open_capital: Dict[str, float],
    open_prices: Dict[str, dict],
) -> List[dict]:
    """Build Discord embed fields for open positions only (used by period reports)."""
    fields = []
    for ticker, upl in sorted(open_pnl.items()):
        cap          = open_capital.get(ticker, 0.0)
        prices       = open_prices.get(ticker, {})
        avg_e        = prices.get("avg_entry", 0.0)
        cur_p        = prices.get("current_price", 0.0)
        qty          = prices.get("qty", 0.0)
        intraday_pl  = prices.get("intraday_pl", 0.0)
        delta        = cur_p - avg_e
        delta_pct    = (delta / avg_e * 100) if avg_e > 0 else 0.0
        d_emoji      = "🟢" if delta >= 0 else "🔴"
        id_emoji_pos = "🟢" if intraday_pl >= 0 else "🔴"
        cur_value    = qty * cur_p
        fields.append({
            "name":   f"🟡 {ticker} (open)",
            "value":  (
                f"Shares: **{qty:,.0f}**\n"
                f"Bought @ **${avg_e:,.2f}**  →  Now **${cur_p:,.2f}**\n"
                f"Delta: {d_emoji} **${delta:+,.2f}** ({delta_pct:+.2f}%) per share\n"
                f"Capital: **${cap:,.2f}**  →  Now: **${cur_value:,.2f}**\n"
                f"Unrealized Today: {id_emoji_pos} **${intraday_pl:+,.2f}**\n"
                f"Unrealized Total: {d_emoji} **${upl:+,.2f}**"
            ),
            "inline": True,
        })
    if not fields:
        fields.append({"name": "No open positions", "value": "All positions closed.", "inline": False})
    return fields


def _open_positions_sms_lines(
    open_pnl: Dict[str, float],
    open_capital: Dict[str, float],
    open_prices: Dict[str, dict],
) -> List[str]:
    """Build SMS lines for open positions only (used by period reports)."""
    lines = []
    for ticker, upl in sorted(open_pnl.items()):
        cap          = open_capital.get(ticker, 0.0)
        prices       = open_prices.get(ticker, {})
        avg_e        = prices.get("avg_entry", 0.0)
        cur_p        = prices.get("current_price", 0.0)
        qty          = prices.get("qty", 0.0)
        intraday_pl  = prices.get("intraday_pl", 0.0)
        delta        = cur_p - avg_e
        delta_pct    = (delta / avg_e * 100) if avg_e > 0 else 0.0
        d_emoji      = "🟢" if delta >= 0 else "🔴"
        id_emoji_pos = "🟢" if intraday_pl >= 0 else "🔴"
        cur_value    = qty * cur_p
        lines.append(f"🟡 {ticker} (open)")
        lines.append(f"  Shares: {qty:,.0f}")
        lines.append(f"  Bought: ${avg_e:,.2f} → Now: ${cur_p:,.2f}")
        lines.append(f"  Delta: {d_emoji} ${delta:+,.2f} ({delta_pct:+.2f}%) per share")
        lines.append(f"  Capital: ${cap:,.2f} → Now: ${cur_value:,.2f}")
        lines.append(f"  Unrealized Today: {id_emoji_pos} ${intraday_pl:+,.2f}")
        lines.append(f"  Unrealized Total: {d_emoji} ${upl:+,.2f}")
    if not lines:
        lines.append("No open positions.")
    return lines


def _ticker_sms_lines(
    pnl: Dict[str, dict],
    open_pnl: Dict[str, float],
    open_capital: Dict[str, float],
    open_prices: Dict[str, dict],
) -> List[str]:
    """Build per-ticker SMS lines (closed + open positions)."""
    lines = []

    for ticker, data in sorted(pnl.items()):
        r        = data["realized"]
        emoji    = "🟢" if r >= 0 else "🔴"
        buy_qty  = data.get("buy_qty", 0.0)
        sell_qty = data.get("sell_qty", 0.0)
        upl      = open_pnl.get(ticker)
        lines.append(f"{emoji} {ticker}")
        lines.append(f"  Realized: ${r:+,.2f}")
        lines.append(f"  Bought: {buy_qty:,.0f} | Sold: {sell_qty:,.0f} shares")
        if upl is not None:
            lines.append(f"  Unrealized Total: ${upl:+,.2f}")

    for ticker, upl in sorted(open_pnl.items()):
        if ticker in pnl:
            continue
        cap          = open_capital.get(ticker, 0.0)
        prices       = open_prices.get(ticker, {})
        avg_e        = prices.get("avg_entry", 0.0)
        cur_p        = prices.get("current_price", 0.0)
        qty          = prices.get("qty", 0.0)
        intraday_pl  = prices.get("intraday_pl", 0.0)
        delta        = cur_p - avg_e
        delta_pct    = (delta / avg_e * 100) if avg_e > 0 else 0.0
        d_emoji      = "🟢" if delta >= 0 else "🔴"
        id_emoji_pos = "🟢" if intraday_pl >= 0 else "🔴"
        cur_value    = qty * cur_p
        lines.append(f"🟡 {ticker} (open)")
        lines.append(f"  Shares: {qty:,.0f}")
        lines.append(f"  Bought: ${avg_e:,.2f} → Now: ${cur_p:,.2f}")
        lines.append(f"  Delta: {d_emoji} ${delta:+,.2f} ({delta_pct:+.2f}%) per share")
        lines.append(f"  Capital: ${cap:,.2f} → Now: ${cur_value:,.2f}")
        lines.append(f"  Unrealized Today: {id_emoji_pos} ${intraday_pl:+,.2f}")
        lines.append(f"  Unrealized Total: {d_emoji} ${upl:+,.2f}")

    if not pnl and not open_pnl:
        lines.append("No activity this period.")

    return lines


# ── Intraday report ────────────────────────────────────────────────────────────

async def send_intraday_report() -> dict:
    today = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = _ET.localize(today.replace(tzinfo=None)).astimezone(timezone.utc)

    client = get_client()
    pnl    = _fifo_realized(client, period_start=today_utc, lookback_days=0)
    open_pnl, open_intraday_pnl, open_capital, open_prices = _fetch_open_positions(client)

    account        = client.get_account()
    equity         = float(account.equity or 0)
    last_equity    = float(account.last_equity or equity)
    total_invested = sum(open_capital.values())
    total_r        = sum(v["realized"] for v in pnl.values())
    total_u        = sum(open_pnl.values())
    total_intraday_u = sum(open_intraday_pnl.values())

    spy_intraday    = compute_spy_pct("1d")
    initial_capital = settings.initial_capital or _get_net_deposits(client)
    all_time_pnl    = (equity - initial_capital) if initial_capital else None
    all_time_pct    = ((all_time_pnl / initial_capital) * 100) if initial_capital else None

    # ── Discord ───────────────────────────────────────────────────────────────
    fields = _ticker_discord_fields(pnl, open_pnl, open_intraday_pnl, open_capital, open_prices)

    # Capital invested
    if open_capital:
        cap_lines  = "\n".join(f"{t}: **${c:,.2f}**" for t, c in sorted(open_capital.items()))
        cap_lines += f"\n**Total: ${total_invested:,.2f}**"
        fields.append({"name": "💰 Capital Invested", "value": cap_lines, "inline": False})
    else:
        fields.append({"name": "💰 Capital Invested", "value": "**$0.00** (no open positions)", "inline": False})

    # Overall P&L vs capital
    total_pnl = total_r + total_u
    roc_pct   = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
    roc_emoji = "🟢" if total_pnl >= 0 else "🔴"
    roc_value = (
        f"{roc_emoji} **${total_pnl:+,.2f}** ({roc_pct:+.2f}% of ${total_invested:,.2f} capital)"
        if total_invested > 0 else f"{roc_emoji} **${total_pnl:+,.2f}** (no capital deployed)"
    )
    fields.append({"name": "💹 Overall P&L vs Capital", "value": roc_value, "inline": False})

    # Intraday P&L
    intraday_dollar = total_r + total_intraday_u
    intraday_pct    = (intraday_dollar / last_equity * 100) if last_equity > 0 else 0.0
    id_emoji        = "🟢" if intraday_pct >= 0 else "🔴"
    spy_line        = _spy_line(intraday_pct, spy_intraday)
    fields.append({
        "name":   "📈 Intraday % P&L",
        "value":  f"{id_emoji} **${intraday_dollar:+,.2f}** ({intraday_pct:+.2f}%)" + (f"\n{spy_line}" if spy_line else ""),
        "inline": False,
    })

    # Equity
    eq_delta     = equity - last_equity
    eq_delta_pct = (eq_delta / last_equity * 100) if last_equity > 0 else 0.0
    eq_emoji     = "🟢" if eq_delta >= 0 else "🔴"
    equity_value = (
        f"**${last_equity:,.2f}**  →  Now: **${equity:,.2f}**\n"
        f"{eq_emoji} **${eq_delta:+,.2f}** ({eq_delta_pct:+.2f}%)"
    )
    if all_time_pnl is not None:
        at_emoji = "🟢" if all_time_pnl >= 0 else "🔴"
        equity_value += f"\n{at_emoji} Gain (excl. deposits): **${all_time_pnl:+,.2f}** ({all_time_pct:+.2f}%)"
    fields.append({"name": "💰 Equity", "value": equity_value, "inline": False})

    date_str = datetime.now(_ET).strftime("%Y-%m-%d")
    embed = {
        "title":  f"📊 Intraday P&L — {date_str}",
        "color":  0x00B300 if total_r >= 0 else 0xCC0000,
        "fields": fields,
        "footer": {"text": f"Realized: ${total_r:+,.2f}  •  Unrealized: ${total_u:+,.2f}  •  Equity: ${equity:,.2f}"},
    }
    await notify_pnl_embed(embed)

    # ── SMS ───────────────────────────────────────────────────────────────────
    sms = [f"📊 Intraday P&L — {date_str}"]
    sms += _ticker_sms_lines(pnl, open_pnl, open_capital, open_prices)
    sms.append(f"Realized: ${total_r:+,.2f} | Unrealized: ${total_u:+,.2f}")
    if open_capital:
        cap_parts = " | ".join(f"{t}: ${c:,.0f}" for t, c in sorted(open_capital.items()))
        sms.append(f"Capital: {cap_parts}")
        sms.append(f"Total Invested: ${total_invested:,.2f}")
    else:
        sms.append("Capital Invested: $0.00")
    sms.append(f"Overall P&L: {roc_emoji} ${total_pnl:+,.2f} ({roc_pct:+.2f}% of capital)")
    sms.append(f"Intraday P&L: {id_emoji} ${intraday_dollar:+,.2f} ({intraday_pct:+.2f}%)")
    if spy_intraday is not None:
        sms.append(f"SPY: {spy_intraday:+.2f}% | Diff: {intraday_pct - spy_intraday:+.2f}%")
    sms.append(f"Equity: ${last_equity:,.2f} → Now: ${equity:,.2f} ({eq_emoji} {eq_delta_pct:+.2f}%)")
    if all_time_pnl is not None:
        at_emoji = "🟢" if all_time_pnl >= 0 else "🔴"
        sms.append(f"Gain (excl. deposits): {at_emoji} ${all_time_pnl:+,.2f} ({all_time_pct:+.2f}%)")
    await notify_sms("\n".join(sms))

    log.info("Intraday P&L report sent", extra={"realized": total_r, "unrealized": total_u})
    return {
        "status": "sent", "date": date_str,
        "total_realized": round(total_r, 2), "total_unrealized": round(total_u, 2),
        "by_ticker": {k: round(v["realized"], 2) for k, v in pnl.items()},
    }


# ── Period reports (weekly / monthly / yearly) ─────────────────────────────────

async def send_period_report(period: str) -> None:
    labels       = {"1W": "Weekly",  "1M": "Monthly",  "1A": "Yearly"}
    icons        = {"1W": "📅",      "1M": "🗓️",       "1A": "📆"}
    spy_map      = {"1W": "5d",      "1M": "1mo",       "1A": "1y"}
    lookback_map = {"1W": 90,        "1M": 180,          "1A": 400}
    label  = labels.get(period, period)
    icon   = icons.get(period, "📊")

    now = datetime.now(_ET)

    # Period start (ET midnight)
    if period == "1W":
        period_start_et = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        title_date = f"Week of {period_start_et.strftime('%b %-d')}–{now.strftime('%-d, %Y')}"
    elif period == "1M":
        period_start_et = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        title_date = now.strftime("%B %Y")
    else:
        period_start_et = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        title_date = str(now.year)

    period_start_utc = _ET.localize(period_start_et.replace(tzinfo=None)).astimezone(timezone.utc)

    client = get_client()
    pnl    = _fifo_realized(client, period_start=period_start_utc, lookback_days=lookback_map[period])
    open_pnl, open_intraday_pnl, open_capital, open_prices = _fetch_open_positions(client)

    account        = client.get_account()
    equity         = float(account.equity or 0)
    last_equity    = float(account.last_equity or equity)
    total_invested = sum(open_capital.values())
    total_r        = sum(v["realized"] for v in pnl.values())
    total_u        = sum(open_pnl.values())
    total_intraday_u = sum(open_intraday_pnl.values())

    initial_capital = settings.initial_capital or _get_net_deposits(client)
    all_time_pnl    = (equity - initial_capital) if initial_capital else None
    all_time_pct    = ((all_time_pnl / initial_capital) * 100) if initial_capital else None

    # Portfolio history for start → end equity
    try:
        history  = get_portfolio_history(period=period, timeframe="1D")
        equities = [e for e in (history.equity or []) if e is not None]
        start_equity = equities[0]  if len(equities) >= 2 else last_equity
        end_equity   = equities[-1] if len(equities) >= 2 else equity
    except Exception:
        start_equity, end_equity = last_equity, equity

    period_pnl = end_equity - start_equity
    period_pct = (period_pnl / start_equity * 100) if start_equity else 0.0
    spy_pct    = compute_spy_pct(spy_map.get(period, "5d"))
    spy_line   = _spy_line(period_pct, spy_pct)
    pnl_emoji  = "🟢" if period_pnl >= 0 else "🔴"

    # Overall P&L vs capital
    total_pnl = total_r + total_u
    roc_pct   = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
    roc_emoji = "🟢" if total_pnl >= 0 else "🔴"
    roc_value = (
        f"{roc_emoji} **${total_pnl:+,.2f}** ({roc_pct:+.2f}% of ${total_invested:,.2f} capital)"
        if total_invested > 0 else f"{roc_emoji} **${total_pnl:+,.2f}** (no capital deployed)"
    )

    # Period P&L vs capital
    period_roc_pct = (period_pnl / total_invested * 100) if total_invested > 0 else 0.0

    # Intraday move (for current day portion)
    intraday_dollar = total_r + total_intraday_u
    intraday_pct    = (intraday_dollar / last_equity * 100) if last_equity > 0 else 0.0
    id_emoji        = "🟢" if intraday_pct >= 0 else "🔴"

    # Equity delta
    eq_delta     = end_equity - start_equity
    eq_delta_pct = (eq_delta / start_equity * 100) if start_equity > 0 else 0.0
    eq_emoji     = "🟢" if eq_delta >= 0 else "🔴"

    # ── Discord ───────────────────────────────────────────────────────────────
    fields = _open_positions_discord_fields(open_pnl, open_intraday_pnl, open_capital, open_prices)

    # Capital invested
    if open_capital:
        cap_lines  = "\n".join(f"{t}: **${c:,.2f}**" for t, c in sorted(open_capital.items()))
        cap_lines += f"\n**Total: ${total_invested:,.2f}**"
        fields.append({"name": "💰 Capital Invested", "value": cap_lines, "inline": False})
    else:
        fields.append({"name": "💰 Capital Invested", "value": "**$0.00** (no open positions)", "inline": False})

    # Overall P&L vs capital
    fields.append({"name": "💹 Overall P&L vs Capital", "value": roc_value, "inline": False})

    # Period P&L + SPY
    period_roc_line = f"\n💹 {period_roc_pct:+.2f}% of ${total_invested:,.2f} capital" if total_invested > 0 else ""
    fields.append({
        "name":   f"📈 {label} % P&L",
        "value":  (
            f"{pnl_emoji} **${period_pnl:+,.2f}** ({period_pct:+.2f}%)"
            + period_roc_line
            + (f"\n{spy_line}" if spy_line else "")
        ),
        "inline": False,
    })

    # Today's intraday P&L
    spy_today = _spy_line(intraday_pct, compute_spy_pct("1d"))
    fields.append({
        "name":   "📅 Today's P&L",
        "value":  f"{id_emoji} **${intraday_dollar:+,.2f}** ({intraday_pct:+.2f}%)" + (f"\n{spy_today}" if spy_today else ""),
        "inline": False,
    })

    # Equity
    equity_value = (
        f"**${start_equity:,.2f}**  →  Now: **${end_equity:,.2f}**\n"
        f"{eq_emoji} **${eq_delta:+,.2f}** ({eq_delta_pct:+.2f}%)"
    )
    if all_time_pnl is not None:
        at_emoji = "🟢" if all_time_pnl >= 0 else "🔴"
        equity_value += f"\n{at_emoji} Gain (excl. deposits): **${all_time_pnl:+,.2f}** ({all_time_pct:+.2f}%)"
    fields.append({"name": "💰 Equity", "value": equity_value, "inline": False})

    embed = {
        "title":     f"{icon} {label} P&L — {title_date}",
        "color":     0x00B300 if period_pnl >= 0 else 0xCC0000,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Realized: ${total_r:+,.2f}  •  Unrealized: ${total_u:+,.2f}  •  Equity: ${equity:,.2f}"},
    }
    period_notify = {"1W": notify_weekly_pnl_embed, "1M": notify_monthly_pnl_embed, "1A": notify_yearly_pnl_embed}
    await period_notify.get(period, notify_pnl_embed)(embed)

    # ── SMS ───────────────────────────────────────────────────────────────────
    sms = [f"{icon} {label} P&L — {title_date}"]
    sms += _open_positions_sms_lines(open_pnl, open_capital, open_prices)
    sms.append(f"Realized: ${total_r:+,.2f} | Unrealized: ${total_u:+,.2f}")
    if open_capital:
        cap_parts = " | ".join(f"{t}: ${c:,.0f}" for t, c in sorted(open_capital.items()))
        sms.append(f"Capital: {cap_parts} | Total: ${total_invested:,.2f}")
    else:
        sms.append("Capital Invested: $0.00")
    sms.append(f"Overall P&L: {roc_emoji} ${total_pnl:+,.2f} ({roc_pct:+.2f}% of capital)")
    sms.append(f"{label} P&L: {pnl_emoji} ${period_pnl:+,.2f} ({period_pct:+.2f}%)")
    if spy_pct is not None:
        sms.append(f"SPY ({label}): {spy_pct:+.2f}% | Diff: {period_pct - spy_pct:+.2f}%")
    sms.append(f"Today: {id_emoji} ${intraday_dollar:+,.2f} ({intraday_pct:+.2f}%)")
    sms.append(f"Equity: ${start_equity:,.2f} → Now: ${end_equity:,.2f} ({eq_emoji} {eq_delta_pct:+.2f}%)")
    if all_time_pnl is not None:
        at_emoji = "🟢" if all_time_pnl >= 0 else "🔴"
        sms.append(f"Gain (excl. deposits): {at_emoji} ${all_time_pnl:+,.2f} ({all_time_pct:+.2f}%)")
    await notify_sms("\n".join(sms))

    log.info("%s P&L report sent: pnl=%.2f pct=%.2f", label, period_pnl, period_pct)


async def send_weekly_report()  -> None: await send_period_report("1W")
async def send_monthly_report() -> None: await send_period_report("1M")
async def send_yearly_report()  -> None: await send_period_report("1A")
