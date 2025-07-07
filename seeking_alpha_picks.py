import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import Keys

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

SEEKING_ALPHA_BASE_URL = "https://seekingalpha.com"
SEEKING_ALPHA_LOGIN_URL = "https://seekingalpha.com/"
STOCK_PICKS_PAGE_URL = "https://seekingalpha.com/alpha-picks/articles"
API_ENDPOINT = "https://seekingalpha.com/api/v3/service_plans/458/picks"
CHECK_INTERVAL = 1
PROCESSED_PICKS_FILE = "data/seeking_alpha_stock_picks_processed.json"

# Environment variables
SEEKING_ALPHA_EMAIL = os.getenv("SEEKING_ALPHA_EMAIL")
SEEKING_ALPHA_PASSWORD = os.getenv("SEEKING_ALPHA_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("SEEKING_ALPHA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SEEKING_ALPHA_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)

co = ChromiumOptions()
page = ChromiumPage(co)


def load_processed_picks():
    try:
        with open(PROCESSED_PICKS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_picks(picks):
    with open(PROCESSED_PICKS_FILE, "w") as f:
        json.dump(picks, f, indent=2)
    log_message("Processed stock picks saved.", "INFO")


def none_element(element):
    return True if "NoneElement" in str(element) else False


async def send_captcha_notification():
    message = f"<b>Seeking Alpha Login Captcha Detected (Stock Picks)</b>\n\n"
    message += f"<b>Time:</b> {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Action Required:</b> Manual login needed\n"
    message += f"<b>Status:</b> Bot waiting for manual intervention"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message("Captcha notification sent to Telegram", "WARNING")


async def login_seeking_alpha():
    """Login to Seeking Alpha using DrissionPage"""
    global page

    try:
        log_message("Navigating to Seeking Alpha homepage", "INFO")
        page.get(SEEKING_ALPHA_LOGIN_URL)
        await asyncio.sleep(3)

        if await check_login_status():
            log_message("Already logged in to Seeking Alpha", "INFO")
            return True

        login_button = page.ele("@aria-label=Login / Register", timeout=1)
        if none_element(login_button):
            login_button = page.ele("Log in", timeout=1)

        if not none_element(login_button):
            login_button.click()
            await asyncio.sleep(2)
            log_message("Clicked login button", "INFO")
        else:
            log_message("Login button not found", "ERROR")
            return False

        email_input = page.ele("@autocomplete=username", timeout=1)
        if not none_element(email_input):
            email_input.clear()
            email_input.input(SEEKING_ALPHA_EMAIL)
            log_message("Entered email", "INFO")
        else:
            log_message("Email input not found", "ERROR")
            return False

        password_input = page.ele("@autocomplete=current-password", timeout=1)
        if none_element(password_input):
            password_input = page.ele("#signInPasswordField", timeout=1)

        if not none_element(password_input):
            password_input.clear()
            password_input.input(
                vals=SEEKING_ALPHA_PASSWORD + Keys.ENTER, clear=True, by_js=False
            )
            log_message("Entered password waiting for verification", "INFO")
            await asyncio.sleep(5)
        else:
            log_message("Password input not found", "ERROR")
            return False

        if await check_captcha():
            await send_captcha_notification()
            log_message(
                "Captcha detected, waiting for manual intervention...", "WARNING"
            )

            while await check_captcha():
                await asyncio.sleep(10)

            log_message("Captcha resolved, continuing...", "INFO")

        await asyncio.sleep(3)
        if await check_login_status():
            log_message("Successfully logged into Seeking Alpha", "INFO")
            return True
        else:
            log_message("Login failed - verification unsuccessful", "ERROR")
            return False

    except Exception as e:
        log_message(f"Error during Seeking Alpha login: {e}", "ERROR")
        return False


async def check_captcha():
    try:
        captcha_elements = [
            not none_element(page.ele('div[class*="captcha"]', timeout=3)),
            not none_element(page.ele('iframe[src*="captcha"]', timeout=3)),
            "captcha" in page.html.lower(),
        ]
        return any(captcha_elements)
    except:
        return False


async def check_login_status():
    """Check if user is logged in by navigating to stock picks page"""
    try:
        page.get(STOCK_PICKS_PAGE_URL)
        await asyncio.sleep(3)

        if "subscribe" in page.url.lower():
            log_message("Redirected to subscribe page - need to re-login", "WARNING")
            return False
        else:
            return True

    except Exception as e:
        log_message(f"Error checking login status: {e}", "ERROR")
        return False


async def fetch_stock_picks_data():
    """Fetch stock picks data by navigating directly to API endpoint"""
    try:
        start_time = time.time()

        api_url = f"{API_ENDPOINT}?include=ticker,ticker.sector,ticker.tickerMetrics,ticker.tickerMetrics.metricType&page[size]=500&sort=undefined"
        page.get(api_url)

        pre_element = page.ele("pre")
        if none_element(pre_element):
            log_message("No <pre> element found - possibly redirected", "ERROR")
            return None

        # NOTE: Ignore this error dont know why the fuck `text` is called a method in the docs
        json_content = pre_element.text
        result = json.loads(json_content)

        paywalled_count = sum(
            1 for article in result.get("data", []) if article.get("isPaywalled", False)
        )

        if paywalled_count > 0:
            # FIXME: Make it warning later on
            log_message("Found paywalled articles trying to refresh tokens", "ERROR")
            return None

        fetch_time = time.time() - start_time
        log_message(
            f"Successfully fetched stock picks data in {fetch_time:.2f} seconds", "INFO"
        )
        return result

    except json.JSONDecodeError as e:
        log_message(f"Failed to parse JSON response: {e}", "ERROR")
        return None
    except Exception as e:
        log_message(f"Error fetching stock picks data: {e}", "ERROR")
        return None


def extract_stock_picks_data(json_data):
    """Extract stock picks information from API response"""
    try:
        stock_pick_map = {}

        for pick in json_data.get("data", []):
            if pick.get("type") == "stock_pick":
                ticker_id = (
                    pick.get("relationships", {})
                    .get("ticker", {})
                    .get("data", {})
                    .get("id")
                )
                attrs = pick.get("attributes", {})
                stock_pick_map[ticker_id] = {
                    "active": attrs.get("active"),
                    "buy_price": attrs.get("buy_price"),
                    "buy_price_status": attrs.get("buy_price_status"),
                    "sell_price": attrs.get("sell_price"),
                    "sell_price_status": attrs.get("sell_price_status"),
                    "weight_in_portfolio": attrs.get("weight_in_portfolio"),
                }

        stock_picks = []
        for item in json_data.get("included", []):
            if item.get("type") == "ticker":
                ticker_id = item.get("id")
                ticker_name = item.get("attributes", {}).get("name")
                company_name = item.get("attributes", {}).get("company", "")
                trading_view_slug = item.get("attributes", {}).get(
                    "tradingViewSlug", ""
                )
                pick_info = stock_pick_map.get(ticker_id)

                if pick_info:
                    stock_pick = {
                        "ticker_id": ticker_id,
                        "ticker_name": ticker_name,
                        "company_name": company_name,
                        "trading_view_slug": trading_view_slug,
                        **pick_info,
                    }
                    stock_picks.append(stock_pick)

        return stock_picks

    except Exception as e:
        log_message(f"Error extracting stock picks data: {e}", "ERROR")
        return []


async def send_stock_pick_notification(stock_pick, is_update=False):
    """Send notification for a stock pick to Telegram"""
    try:
        current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

        ticker_name = stock_pick.get("ticker_name", "N/A")
        company_name = stock_pick.get("company_name", "N/A")
        trading_view_slug = stock_pick.get("trading_view_slug", "N/A")

        action = "Updated" if is_update else "New"

        telegram_message = f"<b>{action} Seeking Alpha Stock Pick</b>\n\n"
        telegram_message += f"<b>Ticker:</b> {ticker_name}\n"
        telegram_message += f"<b>Company:</b> {company_name}\n"
        telegram_message += f"<b>TradingView:</b> {trading_view_slug}\n"
        telegram_message += (
            f"<b>Active:</b> {'Yes' if stock_pick.get('active') else 'No'}\n"
        )

        buy_price = stock_pick.get("buy_price")
        if buy_price:
            telegram_message += f"<b>Buy Price:</b> ${buy_price:.2f}\n"
            telegram_message += (
                f"<b>Buy Status:</b> {stock_pick.get('buy_price_status', 'N/A')}\n"
            )

        sell_price = stock_pick.get("sell_price")
        if sell_price:
            telegram_message += f"<b>Sell Price:</b> ${sell_price:.2f}\n"
            telegram_message += (
                f"<b>Sell Status:</b> {stock_pick.get('sell_price_status', 'N/A')}\n"
            )

        weight = stock_pick.get("weight_in_portfolio")
        if weight:
            telegram_message += f"<b>Portfolio Weight:</b> {weight:.2f}%\n"

        telegram_message += f"\n<b>Current Time:</b> {current_time}"

        await send_telegram_message(telegram_message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(f"Stock pick notification sent: {ticker_name}", "INFO")

    except Exception as e:
        log_message(f"Error sending stock pick notification: {e}", "ERROR")


async def process_stock_picks(api_data):
    """Process fetched stock picks and send notifications for new/updated ones"""
    if not api_data:
        log_message("No valid stock picks data received", "WARNING")
        return

    processed_picks = load_processed_picks()
    current_picks = extract_stock_picks_data(api_data)

    new_picks_count = 0
    updated_picks_count = 0

    for pick in current_picks:
        ticker_id = pick.get("ticker_id")

        if ticker_id not in processed_picks:
            await send_stock_pick_notification(pick, is_update=False)
            processed_picks[ticker_id] = pick
            new_picks_count += 1
        else:
            previous_pick = processed_picks[ticker_id]
            if pick != previous_pick:
                await send_stock_pick_notification(pick, is_update=True)
                processed_picks[ticker_id] = pick
                updated_picks_count += 1

    if new_picks_count > 0 or updated_picks_count > 0:
        save_processed_picks(processed_picks)
        log_message(
            f"Processed {new_picks_count} new and {updated_picks_count} updated stock picks",
            "INFO",
        )
    else:
        log_message("No new or updated stock picks found", "INFO")


async def run_scraper():
    """Main scraper loop"""
    log_message("Starting Seeking Alpha Stock Picks scraper", "INFO")

    if not await login_seeking_alpha():
        log_message("Failed to login to Seeking Alpha", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message(
            "Market is open. Starting to monitor Seeking Alpha stock picks...", "INFO"
        )

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...", "INFO")
                break

            api_data = await fetch_stock_picks_data()
            if api_data:
                await process_stock_picks(api_data)
            else:
                if not await check_login_status():
                    log_message("Session expired, attempting re-login...", "WARNING")
                    if not await login_seeking_alpha():
                        log_message("Re-login failed, waiting before retry...", "ERROR")
                        await asyncio.sleep(300)
                        continue

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    required_vars = [
        SEEKING_ALPHA_EMAIL,
        SEEKING_ALPHA_PASSWORD,
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_GRP,
    ]
    if not all(required_vars):
        log_message("Missing required environment variables", "CRITICAL")
        log_message(
            "Required: SEEKING_ALPHA_EMAIL, SEEKING_ALPHA_PASSWORD, SEEKING_ALPHA_TELEGRAM_BOT_TOKEN, SEEKING_ALPHA_TELEGRAM_GRP",
            "CRITICAL",
        )
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        page.quit()
        sys.exit(1)


if __name__ == "__main__":
    main()
