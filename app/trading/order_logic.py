"""
order_logic.py — Translates TradingView alert actions into Alpaca orders.

Action mapping
──────────────
buy              → BUY qty shares
sell             → SELL qty shares
close_long       → close any open long position (all shares)
close_short      → close any open short position (buy to cover)
reverse_to_long  → close short (if any) then BUY qty shares
reverse_to_short → close long  (if any) then SELL qty shares

Kimi strategy actions
──────────────────────
base_entry       → ignored (you place the base order manually on Alpaca)
add_leverage     → BUY exactly payload.contracts shares, no checks
remove_leverage  → SELL exactly payload.contracts shares (safety cap applied)
stop_loss        → close ALL open positions on Alpaca
"""

import logging
import math
from typing import Optional

from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Order

from app.models import AlertPayload, TradingAction
from app.trading import alpaca_client as ac

log = logging.getLogger(__name__)

# Imported lazily to avoid circular import at module load time
def _notify_sync(message: str) -> None:
    """Fire-and-forget Discord text notification from sync context."""
    import asyncio
    try:
        from app.notifications import notify
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(notify(message))
        else:
            loop.run_until_complete(notify(message))
    except Exception:
        pass


def _dtbp_cap_qty(ticker: str, requested_qty: int, price: Optional[float]) -> int:
    """
    Cap requested_qty so the total cost fits within Day Trading Buying Power.
    Returns the capped qty (may be less than requested_qty, or 0 if DTBP is exhausted).
    Falls back to requested_qty if DTBP cannot be determined.
    """
    try:
        dtbp = ac.get_daytrading_buying_power()
        if dtbp <= 0:
            log.warning(
                "DTBP is zero — skipping buy",
                extra={"ticker": ticker, "dtbp": dtbp, "requested_qty": requested_qty},
            )
            return 0

        if price is None or price <= 0:
            price = ac.get_latest_price(ticker)
        if price is None or price <= 0:
            log.warning("Cannot determine price for DTBP check — using full requested qty", extra={"ticker": ticker})
            return requested_qty

        max_qty = math.floor(dtbp / price)
        if max_qty <= 0:
            log.warning(
                "DTBP insufficient for 1 share — skipping buy",
                extra={"ticker": ticker, "dtbp": dtbp, "price": price, "requested_qty": requested_qty},
            )
            _notify_sync(
                f"🚫 **{ticker}** buy skipped — Day Trading Buying Power is exhausted "
                f"(DTBP: ${dtbp:,.2f}, price: ${price:,.2f})."
            )
            return 0

        capped = min(requested_qty, max_qty)
        if capped < requested_qty:
            log.info(
                "Order qty reduced to fit DTBP",
                extra={
                    "ticker": ticker,
                    "requested_qty": requested_qty,
                    "capped_qty": capped,
                    "dtbp": dtbp,
                    "price": price,
                },
            )
            _notify_sync(
                f"⚠️ **{ticker}** order reduced: TradingView requested {requested_qty} shares "
                f"but DTBP is ${dtbp:,.2f} (≈{max_qty} shares @ ${price:,.2f}). "
                f"Placing {capped} shares instead."
            )
        return capped

    except Exception as exc:
        log.warning("DTBP cap check failed (%s) — using full requested qty", exc)
        return requested_qty


# ── Entry point ───────────────────────────────────────────────────────────────

async def execute_action(payload: AlertPayload) -> dict:
    action = payload.action
    ticker = payload.ticker
    qty    = payload.contracts

    log.info(
        "Executing action",
        extra={"action": action, "ticker": ticker, "qty": qty},
    )

    result: dict = {"action": action, "ticker": ticker, "orders": []}

    # ── Legacy actions ────────────────────────────────────────────────────────

    if action == TradingAction.BUY:
        order = _require_qty_then_order(ticker, OrderSide.BUY, qty, payload.price, payload.limit_price, payload.time_in_force)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.SELL:
        order = _require_qty_then_order(ticker, OrderSide.SELL, qty, limit_price=payload.limit_price, time_in_force=payload.time_in_force)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.CLOSE_LONG:
        order = _close_if_long(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No long position to close."

    elif action == TradingAction.CLOSE_SHORT:
        order = _close_if_short(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No short position to close."

    elif action == TradingAction.REVERSE_TO_LONG:
        close_order = _close_if_short(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        long_order = _require_qty_then_order(ticker, OrderSide.BUY, qty, payload.price, payload.limit_price, payload.time_in_force)
        result["orders"].append(_order_summary(long_order))

    elif action == TradingAction.REVERSE_TO_SHORT:
        close_order = _close_if_long(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        short_order = _require_qty_then_order(ticker, OrderSide.SELL, qty, limit_price=payload.limit_price, time_in_force=payload.time_in_force)
        result["orders"].append(_order_summary(short_order))

    # ── Kimi strategy actions ─────────────────────────────────────────────────

    elif action == TradingAction.BASE_ENTRY:
        log.info("Base entry signal received — no action taken (place manually on Alpaca)")
        result["note"] = "Base entry ignored — place base order manually on Alpaca."

    # Grouping Level 1 and Level 2 together since they use the same logic
    elif action in [TradingAction.ADD_LEVERAGE, TradingAction.ADD_LEVERAGE2, TradingAction.ADD_LEVERAGE3]:
        order = _kimi_add_leverage(ticker, math.floor(qty or 0), payload.price, payload.limit_price, payload.time_in_force)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = f"Order skipped for {action} — qty was 0 or DTBP exhausted."

    elif action in [TradingAction.REMOVE_LEVERAGE, TradingAction.REMOVE_LEVERAGE2, TradingAction.REMOVE_LEVERAGE3]:
        order = _kimi_remove_leverage(ticker, math.floor(qty or 0), payload.limit_price, payload.time_in_force)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = f"No position found to remove for {action}."

    elif action == TradingAction.STOP_LOSS:
        orders = _kimi_stop_loss(ticker)
        result["orders"].extend([_order_summary(o) for o in orders if o])
        if not result["orders"]:
            result["note"] = "No open positions to close."

    else:
        raise ValueError(f"Unknown action: {action}")

    return result


# ── Kimi-specific helpers ─────────────────────────────────────────────────────

def _kimi_add_leverage(ticker: str, qty: int, price: Optional[float] = None, limit_price: Optional[float] = None, time_in_force: str = "gtc") -> Optional[Order]:
    """
    Buy up to qty shares, capped by Day Trading Buying Power.
    If DTBP covers fewer shares than requested, the reduced qty is used.
    If DTBP is $0, the order is skipped entirely.
    Uses a limit order when limit_price is provided, otherwise market order.
    TradingView alert message must include:
        "contracts": {{strategy.order.contracts}}
    """
    if qty <= 0:
        log.warning(
            "DD qty from payload is 0 or missing — skipping add_leverage",
            extra={"ticker": ticker, "qty": qty},
        )
        return None

    actual_qty = _dtbp_cap_qty(ticker, qty, price)
    if actual_qty <= 0:
        log.warning(
            "Skipping add_leverage — DTBP exhausted",
            extra={"ticker": ticker, "requested_qty": qty},
        )
        return None

    log.info(
        "Placing Kimi DD buy",
        extra={"ticker": ticker, "requested_qty": qty, "actual_qty": actual_qty, "limit_price": limit_price, "time_in_force": time_in_force},
    )
    if limit_price:
        return ac.place_limit_order(ticker, OrderSide.BUY, actual_qty, limit_price, time_in_force)
    return ac.place_market_order(ticker, OrderSide.BUY, actual_qty, time_in_force)


def _kimi_remove_leverage(ticker: str, qty: int, limit_price: Optional[float] = None, time_in_force: str = "gtc") -> Optional[Order]:
    """
    Sell exactly the qty sent by TradingView.
    TradingView alert message must include:
        "contracts": {{strategy.order.contracts}}

    Safety cap ensures we never sell more than what Alpaca actually holds.
    Uses a limit order when limit_price is provided, otherwise market order.
    """
    if qty <= 0:
        log.warning(
            "DD qty from payload is 0 or missing — skipping remove_leverage",
            extra={"ticker": ticker, "qty": qty},
        )
        return None

    position = ac.get_position(ticker)
    if position is None:
        log.info("No open position found — skipping remove_leverage", extra={"ticker": ticker})
        return None

    held_qty = math.floor(float(position.qty))
    sell_qty = min(qty, held_qty)

    if sell_qty <= 0:
        log.warning(
            "Sell qty after safety cap is 0 — skipping",
            extra={"ticker": ticker, "held_qty": held_qty, "requested_qty": qty},
        )
        return None

    log.info(
        "Closing Kimi DD position",
        extra={"ticker": ticker, "requested_qty": qty, "sell_qty": sell_qty, "limit_price": limit_price},
    )
    if limit_price:
        return ac.place_limit_order(ticker, OrderSide.SELL, sell_qty, limit_price, time_in_force, extended_hours=True)
    return ac.place_market_order(ticker, OrderSide.SELL, sell_qty, time_in_force)


def _kimi_stop_loss(ticker: str) -> list:
    """Close all open positions for the ticker."""
    orders = []
    position = ac.get_position(ticker)
    if position:
        log.info("Stop loss triggered — closing all positions", extra={"ticker": ticker})
        order = ac.close_position(ticker)
        if order:
            orders.append(order)
    return orders


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_qty_then_order(
    ticker: str,
    side: OrderSide,
    qty: Optional[float],
    price: Optional[float] = None,
    limit_price: Optional[float] = None,
    time_in_force: str = "gtc",
) -> Order:
    if qty is None or qty <= 0:
        raise ValueError(
            f"Action '{side.value}' requires a positive 'contracts' value, "
            f"got: {qty!r}. Check your TradingView alert message template."
        )
    if side == OrderSide.BUY:
        qty = _dtbp_cap_qty(ticker, math.floor(qty), price)
        if qty <= 0:
            raise ValueError(
                f"Buy order for {ticker} skipped — Day Trading Buying Power is exhausted."
            )
    if limit_price:
        return ac.place_limit_order(ticker, side, qty, limit_price, time_in_force)
    return ac.place_market_order(ticker, side, qty, time_in_force)


def _close_if_long(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "long":
        log.info("Skipping close_long — position is not long", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _close_if_short(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "short":
        log.info("Skipping close_short — position is not short", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _order_summary(order: Order) -> dict:
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          order.symbol,
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
