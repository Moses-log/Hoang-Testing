"""
config.py — Application settings loaded from environment variables.

All sensitive values (API keys, secrets) must be set in a .env file
or as real environment variables. Never hard-code them here.
"""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Alpaca ────────────────────────────────────────────────────────────────
    alpaca_api_key: str
    alpaca_secret_key: str
    # Paper trading endpoint by default. Switch to https://api.alpaca.markets
    # for live trading only after thorough testing.
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"

    # ── Webhook security ──────────────────────────────────────────────────────
    # Must match the "secret" field TradingView sends in every alert payload.
    webhook_secret: str

    # ── Server ────────────────────────────────────────────────────────────────
    port: int = 8000

    # ── Idempotency ───────────────────────────────────────────────────────────
    # How long (seconds) to remember a processed alert_id to block duplicates.
    idempotency_ttl: int = 300  # 5 minutes

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Optional notifications (leave blank to disable) ───────────────────────
    discord_general_webhook_url: Optional[str] = None    # fallback / general channel
    discord_daily_pnl_webhook_url:   Optional[str] = None  # #daily-p-l-logs channel
    discord_weekly_pnl_webhook_url:  Optional[str] = None  # weekly P&L channel
    discord_monthly_pnl_webhook_url: Optional[str] = None  # monthly P&L channel
    discord_yearly_pnl_webhook_url:  Optional[str] = None  # yearly P&L channel
    discord_trades_webhook_url:      Optional[str] = None  # #trade-reports channel
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # ── SMS via email-to-carrier gateway ─────────────────────────────────────
    # SMS_GMAIL_USER:         your Gmail address (e.g. you@gmail.com)
    # SMS_GMAIL_APP_PASSWORD: Gmail app password (not your login password)
    # SMS_TO:                 carrier SMS email  (e.g. 5551234567@vtext.com)
    #   Verizon  → number@vtext.com
    #   AT&T     → number@txt.att.net
    #   T-Mobile → number@tmomail.net
    sms_gmail_user:         Optional[str] = None
    sms_gmail_app_password: Optional[str] = None
    sms_to:                 Optional[str] = None

    # ── Portfolio tracking ────────────────────────────────────────────────────
    # Set to your starting deposit amount so the EOD report can show
    # all-time gain/loss and % return excluding new deposits.
    initial_capital: Optional[float] = None

    # ── Order defaults ────────────────────────────────────────────────────────
    # Set to True only if your Alpaca account has fractional-share trading
    # enabled AND the symbol supports it.
    allow_fractional_shares: bool = False

    # ── Kimi Bot settings ─────────────────────────────────────────────────────
    # Comma-separated list of symbols to trade (e.g. "QQQ,SPY,NVDA")
    bot_symbols: str = "QQQ,SPY,META,TSLA,NVDA,AAPL,CSCO,AMD,MU"
    bot_bb_length: int = 20
    bot_bb_mult: float = 2.0
    bot_exit_band_pct: float = 0.0      # 0=middle band, 1=upper band
    bot_stop_loss_pct: float = 50.0     # % below entry to stop out
    bot_leverage_factor: float = 1.0
    bot_lev_bar_gap: int = 30           # min bars between entries
    bot_bb_alert_pct: float = 0.0005    # proximity alert threshold
    bot_dtbp_usage: float = 0.5         # fraction of DTBP to use

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


# Single shared instance — import this everywhere else.
settings = Settings()
