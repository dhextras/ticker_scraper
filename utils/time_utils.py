import asyncio
import random
from datetime import datetime, timedelta

import pytz

from utils.logger import log_message


def get_current_time():
    return datetime.now(pytz.timezone("America/Chicago"))


def get_next_market_times(start=6, end=19):
    """
    Calculates the next market open and close times.
    Adjusts to the next business day if it's weekend or already past market close.
    """
    current_time_cst = get_current_time()

    # Start with the current day
    market_open_time = current_time_cst.replace(
        hour=start, minute=0, second=0, microsecond=0
    )
    market_close_time = current_time_cst.replace(
        hour=end, minute=0, second=0, microsecond=0
    )

    # Function to advance to next business day (skip weekends)
    def advance_to_next_business_day(date_time):
        days_to_add = 1
        if date_time.weekday() == 4:  # Friday is 4
            days_to_add = 3
        elif date_time.weekday() == 5:  # Saturday is 5
            days_to_add = 2

        return date_time + timedelta(days=days_to_add)

    # Check if current day is a weekend
    if current_time_cst.weekday() >= 5:  # 5 is Saturday, 6 is Sunday
        days_to_monday = (7 - current_time_cst.weekday()) % 7
        if days_to_monday == 0:
            days_to_monday = 1

        market_open_time = (current_time_cst + timedelta(days=days_to_monday)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
        market_close_time = (current_time_cst + timedelta(days=days_to_monday)).replace(
            hour=end, minute=0, second=0, microsecond=0
        )
    elif current_time_cst > market_close_time:
        market_open_time = advance_to_next_business_day(market_open_time)
        market_close_time = advance_to_next_business_day(market_close_time)

    pre_market_login_time = market_open_time - timedelta(minutes=40)
    return pre_market_login_time, market_open_time, market_close_time


async def sleep_until_market_open(start=6, end=19):
    """
    Sleep until market open, handling weekends and after-hours.
    """
    pre_market_login_time, market_open_time, _ = get_next_market_times(
        start=start, end=end
    )
    current_time = get_current_time()

    log_message(
        f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S %A')}", "INFO"
    )
    log_message(
        f"Pre-market login time: {pre_market_login_time.strftime('%Y-%m-%d %H:%M:%S %A')}",
        "INFO",
    )
    log_message(
        f"Market open time: {market_open_time.strftime('%Y-%m-%d %H:%M:%S %A')}", "INFO"
    )

    if current_time < pre_market_login_time:
        sleep_duration = (pre_market_login_time - current_time).total_seconds()
        sleep_duration += random.choice(
            [i for i in range(60, 120)]
        )  # NOTE: Extra random time to avoid overloading telegram bot

        # Format the sleep duration in a more readable way for longer durations
        if sleep_duration > 3600:  # more than an hour
            hours = sleep_duration // 3600
            minutes = (sleep_duration % 3600) // 60
            log_message(
                f"Sleeping until pre-market login time: {hours:.0f} hours and {minutes:.0f} minutes",
                "INFO",
            )
        else:
            log_message(
                f"Sleeping until pre-market login time. Sleep duration: {sleep_duration:.2f} seconds",
                "INFO",
            )

        await asyncio.sleep(sleep_duration)
        log_message("Pre-market login time reached", "INFO")
    elif current_time < market_open_time:
        sleep_duration = (market_open_time - current_time).total_seconds()
        log_message(
            f"Sleeping until market open time. Sleep duration: {sleep_duration:.2f} seconds",
            "INFO",
        )
        await asyncio.sleep(sleep_duration)
        log_message("Market open time reached", "INFO")
    else:
        log_message("Market is already open", "INFO")
