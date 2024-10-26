import asyncio
from datetime import datetime, timedelta

import pytz
from utils.logger import log_message


def get_next_market_times():
    """Calculates the next market open and close times, adjusts to the next day if already past market close."""
    current_time_edt = datetime.now(pytz.timezone("America/New_York"))
    market_open_time = current_time_edt.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    market_close_time = current_time_edt.replace(
        hour=10, minute=59, second=0, microsecond=0
    )

    # If current time is past the close time, set the open/close times for the next day
    if current_time_edt > market_close_time:
        market_open_time += timedelta(days=1)
        market_close_time += timedelta(days=1)

    pre_market_login_time = market_open_time - timedelta(minutes=40)
    return pre_market_login_time, market_open_time, market_close_time


async def sleep_until_market_open():
    pre_market_login_time, market_open_time, _ = get_next_market_times()
    current_time = datetime.now(pytz.timezone("America/New_York"))

    log_message(f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S')}", "INFO")
    log_message(
        f"Pre-market login time: {pre_market_login_time.strftime('%Y-%m-%d %H:%M:%S')}",
        "INFO",
    )
    log_message(
        f"Market open time: {market_open_time.strftime('%Y-%m-%d %H:%M:%S')}", "INFO"
    )

    if current_time < pre_market_login_time:
        sleep_duration = (pre_market_login_time - current_time).total_seconds()
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
