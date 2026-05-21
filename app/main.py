"""
main.py — FastAPI application entry point.

Endpoints
─────────
POST /webhook   Receives TradingView alerts and routes them to Alpaca.
GET  /health    Liveness probe — returns 200 + uptime info.

Security model
──────────────
Every request to /webhook must carry the correct "secret" field in the
JSON body (matched via constant-time comparison in security.py). There is
no separate API-key header — the secret is embedded in the alert payload
as TradingView requires.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import settings
from app.idempotency import is_duplicate, mark_processed
from app.logging_config import setup_logging
from app.models import AlertPayload, TradingAction
from app.notifications import notify
from app.pnl import send_intraday_report
from app.scheduler import scheduler, setup_scheduler
from app.security import verify_webhook_secret
from app import state
from app.trade_stream import start_trade_stream, stop_trade_stream
from app.trading import alpaca_client as ac
from app.trading.order_logic import execute_action
from alpaca.common.exceptions import APIError

# ── Logging must be set up before the first log call ─────────────────────────
setup_logging()
log = logging.getLogger(__name__)

_start_time = time.time()

_EXIT_ACTIONS = {
    TradingAction.SELL,
    TradingAction.CLOSE_LONG,
    TradingAction.CLOSE_SHORT,
    TradingAction.REVERSE_TO_LONG,
    TradingAction.REVERSE_TO_SHORT,
    TradingAction.REMOVE_LEVERAGE,
    TradingAction.REMOVE_LEVERAGE2,
    TradingAction.REMOVE_LEVERAGE3,
    TradingAction.STOP_LOSS,
}

_ENTRY_ACTIONS = {
    TradingAction.BUY,
    TradingAction.ADD_LEVERAGE,
    TradingAction.ADD_LEVERAGE2,
    TradingAction.ADD_LEVERAGE3,
}

_ACTION_LABELS = {
    TradingAction.BUY:               "BUY",
    TradingAction.SELL:              "SELL",
    TradingAction.ADD_LEVERAGE:      "BUY (Add)",
    TradingAction.ADD_LEVERAGE2:     "BUY (Add 2)",
    TradingAction.ADD_LEVERAGE3:     "BUY (Add 3)",
    TradingAction.REMOVE_LEVERAGE:   "SELL (Remove)",
    TradingAction.REMOVE_LEVERAGE2:  "SELL (Remove 2)",
    TradingAction.REMOVE_LEVERAGE3:  "SELL (Remove 3)",
    TradingAction.CLOSE_LONG:        "CLOSE",
    TradingAction.CLOSE_SHORT:       "CLOSE",
    TradingAction.STOP_LOSS:         "STOP LOSS",
    TradingAction.REVERSE_TO_LONG:   "REVERSE → LONG",
    TradingAction.REVERSE_TO_SHORT:  "REVERSE → SHORT",
}


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TradingView → Alpaca webhook server starting",
        extra={"paper_trading": "paper" in settings.alpaca_base_url},
    )
    setup_scheduler()
    scheduler.start()
    log.info("Scheduler started")
    start_trade_stream(asyncio.get_running_loop())
    yield
    scheduler.shutdown(wait=False)
    stop_trade_stream()
    log.info("Server shutting down.")


app = FastAPI(
    title="TradingView → Alpaca Webhook",
    version="1.0.0",
    docs_url=None,   # Disable Swagger UI in production (re-enable for dev)
    redoc_url=None,
    lifespan=lifespan,
)


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    log.warning("Invalid payload", extra={"errors": exc.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "Invalid payload", "detail": exc.errors()},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health():
    """Liveness / readiness probe. Returns 200 while the server is up."""
    from app.trade_stream import _stream_thread
    return {
        "status":        "ok",
        "uptime_s":      round(time.time() - _start_time, 1),
        "paper":         "paper" in settings.alpaca_base_url,
        "stream_alive":  _stream_thread is not None and _stream_thread.is_alive(),
    }


@app.get("/report", tags=["ops"])
async def report():
    """
    Fetch today's intraday P&L from Alpaca, break it down by ticker,
    and send a Discord embed. Returns the same data as JSON.
    """
    try:
        result = await send_intraday_report()
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except Exception as exc:
        log.exception("P&L report failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(exc)},
        )


@app.post("/webhook", tags=["trading"])
async def webhook(request: Request):
    """
    Main TradingView alert receiver.

    Flow:
      1. Parse raw JSON (surface parse errors early).
      2. Validate secret.
      3. Parse + validate the full AlertPayload.
      4. Reject duplicates.
      5. Execute trading action via Alpaca.
      6. Return structured response.
    """
    # ── 1. Raw JSON parse ─────────────────────────────────────────────────────
    try:
        raw = await request.json()
    except Exception:
        log.warning("Received non-JSON request body")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must be valid JSON."},
        )

    log.debug("Raw alert received", extra={"body": raw})

    # ── 2. Secret check ───────────────────────────────────────────────────────
    received_secret = raw.get("secret", "")
    try:
        verify_webhook_secret(received_secret)
    except Exception as exc:
        log.warning("Alert rejected — bad secret", extra={"ip": _client_ip(request)})
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "Unauthorized."},
        )

    # ── 3. Payload validation ─────────────────────────────────────────────────
    try:
        payload = AlertPayload(**raw)
    except ValidationError as exc:
        log.warning("Alert rejected — validation error", extra={"errors": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Payload validation failed.", "detail": exc.errors()},
        )

    log.info(
        "Alert received",
        extra={
            "ticker":    payload.ticker,
            "action":    payload.action,
            "contracts": payload.contracts,
            "order_id":  payload.order_id,
            "timestamp": payload.timestamp,
        },
    )

    # ── 4. Idempotency check ──────────────────────────────────────────────────
    if is_duplicate(payload):
        log.info(
            "Duplicate alert ignored",
            extra={"ticker": payload.ticker, "order_id": payload.order_id},
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "duplicate", "message": "Alert already processed."},
        )

    # ── 5. Execute trade ──────────────────────────────────────────────────────
    try:
        result = await execute_action(payload)
        mark_processed(payload)

        log.info(
            "Trade executed",
            extra={"ticker": payload.ticker, "action": payload.action, "result": result},
        )

        # Register order IDs so the WebSocket stream sends fill notification
        for o in result.get("orders", []):
            oid = o.get("alpaca_order_id", "")
            if oid:
                state.webhook_order_ids.add(oid)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "result": result},
        )

    except ValueError as exc:
        # Bad input (e.g. qty = 0), not an Alpaca error
        log.warning("Trade rejected — bad value: %s", exc, extra={"ticker": payload.ticker})
        await notify(f"⚠️ Trade rejected for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": str(exc)},
        )

    except APIError as exc:
        log.error(
            "Alpaca API error",
            exc_info=True,
            extra={"ticker": payload.ticker, "action": payload.action},
        )
        await notify(f"❌ Alpaca error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "Alpaca API error.", "detail": str(exc)},
        )

    except Exception as exc:
        log.exception("Unexpected error processing alert")
        await notify(f"❌ Unexpected error for {payload.ticker}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error."},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Best-effort client IP (respects X-Forwarded-For from proxies)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_config=None,  # We manage logging ourselves
    )
