"""
models.py — Pydantic models for incoming TradingView webhook payloads.
"""
from typing import Optional
from pydantic import BaseModel, field_validator
from enum import Enum


class TradingAction(str, Enum):
    """Normalised set of actions this system understands."""
    BUY              = "buy"
    SELL             = "sell"
    CLOSE_LONG       = "close_long"
    CLOSE_SHORT      = "close_short"
    REVERSE_TO_LONG  = "reverse_to_long"
    REVERSE_TO_SHORT = "reverse_to_short"
    # ── Kimi strategy actions ─────────────────────────────────────────────────
    BASE_ENTRY       = "base_entry"       # First entry — you place manually, bot ignores
    ADD_LEVERAGE     = "add_leverage"     # DD buy — bot calculates qty from Alpaca balance
    ADD_LEVERAGE2     = "add_leverage2"     # DD buy — bot calculates qty from Alpaca balance
    ADD_LEVERAGE3     = "add_leverage3"     # DD buy — bot calculates qty from Alpaca balance
    REMOVE_LEVERAGE  = "remove_leverage"  # DD sell — bot closes "Leverage" position
    REMOVE_LEVERAGE2  = "remove_leverage2"  # DD sell — bot closes "Leverage" position
    REMOVE_LEVERAGE3  = "remove_leverage3"  # DD sell — bot closes "Leverage" position
    STOP_LOSS        = "stop_loss"        # Full close — bot closes all positions


class AlertPayload(BaseModel):
    """
    Mirrors the TradingView alert message template exactly.
    Extra fields are ignored (model_config extra='ignore').
    """
    # Auth — must match WEBHOOK_SECRET env var
    secret: str

    # Symbol, e.g. "SPY"
    ticker: str

    # One of the TradingAction enum values (case-insensitive)
    action: TradingAction

    # Number of shares/contracts — used for legacy buy/sell actions
    # For Kimi actions (add_leverage etc.) qty is calculated live from Alpaca
    contracts: Optional[float] = None

    # Current bar close price — used for Kimi DD sizing
    price: Optional[float] = None

    # If set, a limit order is placed at this price instead of a market order
    limit_price: Optional[float] = None

    # Order time-in-force — defaults to "gtc" if not provided
    time_in_force: str = "gtc"

    # If true, order is eligible for pre/post market execution (limit orders only)
    extended_hours: bool = False

    # TradingView strategy order ID — used as idempotency key
    order_id: Optional[str] = None

    # Strategy position context
    market_position:           Optional[str]   = None
    market_position_size:      Optional[float] = None
    prev_market_position:      Optional[str]   = None
    prev_market_position_size: Optional[float] = None

    # ISO timestamp from {{timenow}}
    timestamp: Optional[str] = None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("ticker", mode="before")
    @classmethod
    def clean_ticker(cls, v: str) -> str:
        """Strip exchange prefix like 'NASDAQ:AAPL' → 'AAPL'."""
        if ":" in v:
            v = v.split(":")[-1]
        return v.strip().upper()

    @field_validator("action", mode="before")
    @classmethod
    def normalise_action(cls, v: str) -> str:
        """Accept mixed-case actions and convert Kimi plain-English messages."""
        v = v.strip().lower()
        # Map Kimi alert() messages to enum values
        mapping = {
            "base entry":       "base_entry",
            "add leverage":     "add_leverage",
            "add leverage2":     "add_leverage2",
            "add leverage3":     "add_leverage3",
            "remove leverage":  "remove_leverage",
            "remove leverage2":  "remove_leverage2",
            "remove leverage3":  "remove_leverage3",
            "stop loss":        "stop_loss",
        }
        return mapping.get(v, v)

    @field_validator("contracts", "limit_price", mode="before")
    @classmethod
    def parse_contracts(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    @field_validator("time_in_force", mode="before")
    @classmethod
    def normalise_tif(cls, v):
        return str(v).strip().lower() if v else "day"

    model_config = {"extra": "ignore"}
