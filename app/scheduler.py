"""
scheduler.py — Background job scheduler.

Every weekday at 9:05 AM ET, checks Alpaca's market calendar for today then
schedules all applicable end-of-period reports at market close + 5 min:

  Daily   → every trading day
  Weekly  → every Friday (last trading day of week)
  Monthly → last trading day of each month
  Yearly  → last trading day of the year

Holidays and early-close days are handled automatically via the calendar.
"""

import logging
from datetime import date, datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.pnl import (
    send_intraday_report,
    send_weekly_report,
    send_monthly_report,
    send_yearly_report,
)

log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

scheduler = AsyncIOScheduler(timezone="America/New_York")


def setup_scheduler() -> None:
    """Register the daily calendar-check job and run it immediately on startup."""
    scheduler.add_job(
        _schedule_todays_report,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=5,
            timezone="America/New_York",
        ),
        id="daily_calendar_check",
        name="Daily market calendar check",
        replace_existing=True,
    )
    # Also fire immediately on startup so a mid-day server restart doesn't miss today's report
    scheduler.add_job(
        _schedule_todays_report,
        trigger="date",
        id="startup_calendar_check",
        name="Startup calendar check",
        replace_existing=True,
    )
    log.info("Scheduler registered: daily calendar check at 9:05 AM ET Mon-Fri + startup check")


async def _schedule_todays_report() -> None:
    """
    Query Alpaca's calendar for today.
    - Not a trading day → skip everything.
    - Trading day → schedule daily + weekly/monthly/yearly if applicable.
    """
    from app.trading.alpaca_client import get_market_calendar

    today = date.today()
    try:
        calendar = get_market_calendar(start=today, end=today)
    except Exception as exc:
        log.error("Could not fetch market calendar: %s", exc)
        return

    if not calendar:
        log.info("Not a trading day (%s) — all reports skipped", today)
        return

    close_raw = calendar[0].close
    if isinstance(close_raw, datetime):
        close_dt = ET.localize(close_raw.replace(tzinfo=None)) + timedelta(minutes=5)
    else:
        close_dt = ET.localize(datetime.combine(today, close_raw)) + timedelta(minutes=5)
    now_et     = datetime.now(ET)

    if close_dt <= now_et:
        log.info("Market already closed — running all applicable reports now")
        await _run_all_reports(today)
        return

    # ── Daily ─────────────────────────────────────────────────────────────────
    scheduler.add_job(
        _run_daily, trigger=DateTrigger(run_date=close_dt),
        id="eod_daily", replace_existing=True,
    )

    # ── Weekly — every Friday ─────────────────────────────────────────────────
    if today.weekday() == 4:
        scheduler.add_job(
            _run_weekly,
            trigger=DateTrigger(run_date=close_dt + timedelta(minutes=2)),
            id="eod_weekly", replace_existing=True,
        )

    # ── Monthly — last trading day of the month ───────────────────────────────
    if await _is_last_trading_day(today, scope="month"):
        scheduler.add_job(
            _run_monthly,
            trigger=DateTrigger(run_date=close_dt + timedelta(minutes=4)),
            id="eod_monthly", replace_existing=True,
        )

    # ── Yearly — last trading day of the year ─────────────────────────────────
    if await _is_last_trading_day(today, scope="year"):
        scheduler.add_job(
            _run_yearly,
            trigger=DateTrigger(run_date=close_dt + timedelta(minutes=6)),
            id="eod_yearly", replace_existing=True,
        )

    reports = ["daily"]
    if today.weekday() == 4: reports.append("weekly")
    log.info(
        "Reports scheduled for %s ET: %s",
        close_dt.strftime("%H:%M"),
        ", ".join(reports),
    )


async def _is_last_trading_day(today: date, scope: str) -> bool:
    """
    Return True if today is the last trading day of the month (scope='month')
    or year (scope='year') by checking whether the next trading day falls
    in a different month/year.
    """
    from app.trading.alpaca_client import get_market_calendar

    tomorrow  = today + timedelta(days=1)
    end_check = today + timedelta(days=10)
    try:
        next_days = get_market_calendar(start=tomorrow, end=end_check)
    except Exception as exc:
        log.warning("Calendar check failed: %s", exc)
        return False

    if not next_days:
        return True  # No more trading days — must be end of year

    next_trading = next_days[0].date
    if scope == "month":
        return next_trading.month != today.month
    if scope == "year":
        return next_trading.year != today.year
    return False


# ── Job wrappers ──────────────────────────────────────────────────────────────

async def _run_daily() -> None:
    try:
        log.info("Running daily EOD P&L report")
        await send_intraday_report()
    except Exception as exc:
        log.error("Daily report failed: %s", exc)


async def _run_weekly() -> None:
    try:
        log.info("Running weekly P&L report")
        await send_weekly_report()
    except Exception as exc:
        log.error("Weekly report failed: %s", exc)


async def _run_monthly() -> None:
    try:
        log.info("Running monthly P&L report")
        await send_monthly_report()
    except Exception as exc:
        log.error("Monthly report failed: %s", exc)


async def _run_yearly() -> None:
    try:
        log.info("Running yearly P&L report")
        await send_yearly_report()
    except Exception as exc:
        log.error("Yearly report failed: %s", exc)


async def _run_all_reports(today: date) -> None:
    """Run all applicable reports immediately (used when server starts after close)."""
    await _run_daily()
    if today.weekday() == 4:
        await _run_weekly()
    if await _is_last_trading_day(today, scope="month"):
        await _run_monthly()
    if await _is_last_trading_day(today, scope="year"):
        await _run_yearly()
