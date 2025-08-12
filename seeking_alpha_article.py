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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

SEEKING_ALPHA_BASE_URL = "https://seekingalpha.com"
SEEKING_ALPHA_LOGIN_URL = "https://seekingalpha.com/"
ARTICLES_PAGE_URL = "https://seekingalpha.com/alpha-picks/articles"
API_ENDPOINT = "https://seekingalpha.com/api/v3/service_plans/458/marketplace/articles"
CHECK_INTERVAL = 0.3
PROCESSED_ARTICLES_FILE = "data/seeking_alpha_articles_processed.json"

# Environment variables
SEEKING_ALPHA_EMAIL = os.getenv("SEEKING_ALPHA_EMAIL")
SEEKING_ALPHA_PASSWORD = os.getenv("SEEKING_ALPHA_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("SEEKING_ALPHA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SEEKING_ALPHA_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)

co = ChromiumOptions()
page = ChromiumPage(co)


def load_processed_articles():
    try:
        with open(PROCESSED_ARTICLES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_processed_articles(articles):
    with open(PROCESSED_ARTICLES_FILE, "w") as f:
        json.dump(articles, f, indent=2)
    log_message("Processed articles saved.", "INFO")


def none_element(element):
    return True if "NoneElement" in str(element) else False


async def send_captcha_notification():
    message = f"<b>Seeking Alpha Login Captcha Detected</b>\n\n"
    message += (
        f"<b>Current Time:</b> {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
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
    """Check if user is logged in by navigating to articles page"""
    try:
        page.get(ARTICLES_PAGE_URL)
        await asyncio.sleep(3)

        if "subscribe" in page.url.lower():
            log_message("Redirected to subscribe page - need to re-login", "WARNING")
            return False
        else:
            return True

    except Exception as e:
        log_message(f"Error checking login status: {e}", "ERROR")
        return False


async def fetch_articles_data():
    """Fetch articles data by navigating directly to API endpoint"""
    try:
        start_time = time.time()

        api_url = f"{API_ENDPOINT}?include=primaryTickers,secondaryTickers,servicePlans,servicePlanArticles,author,secondaryAuthor"
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
            f"Successfully fetched articles data in {fetch_time:.2f} seconds", "INFO"
        )
        return result

    except json.JSONDecodeError as e:
        log_message(f"Failed to parse JSON response: {e}", "ERROR")
        return None
    except Exception as e:
        log_message(f"Error fetching articles data: {e}", "ERROR")
        return None


def extract_tickers_from_article(article_data, included_data):
    tickers = []
    seen_symbols = set()
    try:
        primary_tickers = (
            article_data.get("relationships", {})
            .get("primaryTickers", {})
            .get("data", [])
        )
        for ticker_ref in primary_tickers:
            ticker_id = ticker_ref.get("id")
            for item in included_data:
                if item.get("id") == ticker_id and item.get("type") == "tag":
                    raw_symbol = item.get("attributes", {}).get("name", "")
                    clean_symbol = raw_symbol.split(":")[0]

                    if clean_symbol in seen_symbols:
                        break

                    seen_symbols.add(clean_symbol)
                    ticker_info = {
                        "symbol": clean_symbol,
                        "company": item.get("attributes", {}).get("company", ""),
                        "url": item.get("links", {}).get("self", ""),
                        "equity_type": item.get("attributes", {}).get("equityType", ""),
                    }
                    tickers.append(ticker_info)
                    break
    except Exception as e:
        log_message(f"Error extracting tickers: {e}", "ERROR")
    return tickers


async def handle_paywalled_article(article_url):
    """Navigate to paywalled article to refresh session"""
    try:
        log_message(f"Attempting to access paywalled article: {article_url}", "INFO")
        page.get(article_url)
        await asyncio.sleep(3)

        if "subscribe" in page.url.lower():
            log_message("Paywall still active after navigation", "WARNING")
            return False

        log_message("Successfully accessed paywalled content", "INFO")
        return True

    except Exception as e:
        log_message(f"Error handling paywalled article: {e}", "ERROR")
        return False


async def send_article_notification(article, tickers):
    try:
        current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

        title = article.get("attributes", {}).get("title", "No title")
        published_on = article.get("attributes", {}).get("publishedOn", "")
        is_paywalled = article.get("attributes", {}).get("isPaywalled", False)
        article_url = (
            f"https://seekingalpha.com{article.get('links', {}).get('self', '')}"
        )

        if is_paywalled:
            await handle_paywalled_article(article_url)

        ticker_info = ""
        if tickers:
            ticker_info = "\n<b>Tickers:</b>\n"
            for ticker in tickers[:3]:
                if ticker and "symbol" in ticker:
                    await send_ws_message(
                        {
                            "name": f"Seeking Alpha - Article ",
                            "type": "Buy",
                            "ticker": ticker["symbol"],
                            "sender": "seeking_alpha",
                        },
                    )

                ticker_info += f"• {ticker['symbol']} - {ticker['company']}\n"

        telegram_message = f"<b>New Seeking Alpha Article</b>\n\n"
        telegram_message += f"<b>Title:</b> {title}\n"
        telegram_message += f"<b>Published:</b> {published_on}\n"
        telegram_message += f"<b>Paywalled:</b> {'Yes' if is_paywalled else 'No'}\n"
        telegram_message += f"<b>Fetch Time:</b> {current_time}\n"
        telegram_message += ticker_info
        telegram_message += f"\n<b>URL:</b> {article_url}"

        await send_telegram_message(telegram_message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(f"Article notification sent: {title[:50]}...", "INFO")

    except Exception as e:
        log_message(f"Error sending article notification: {e}", "ERROR")


async def process_articles(api_data):
    """Process fetched articles and send notifications for new ones"""
    if not api_data or "data" not in api_data:
        log_message("No valid article data received", "WARNING")
        return

    processed_articles = load_processed_articles()
    articles_data = api_data["data"]
    included_data = api_data.get("included", [])

    new_articles_count = 0

    for article in articles_data:
        article_id = article.get("id")

        if article_id not in processed_articles:
            tickers = extract_tickers_from_article(article, included_data)
            await send_article_notification(article, tickers)
            processed_articles.append(article_id)
            new_articles_count += 1

            if len(processed_articles) > 200:
                processed_articles = processed_articles[-100:]

    if new_articles_count > 0:
        save_processed_articles(processed_articles)
        log_message(f"Processed {new_articles_count} new articles", "INFO")
    else:
        log_message("No new articles found", "INFO")


async def run_scraper():
    """Main scraper loop"""
    log_message("Starting Seeking Alpha scraper", "INFO")

    if not await login_seeking_alpha():
        log_message("Failed to login to Seeking Alpha", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        await initialize_websocket()
        log_message(
            "Market is open. Starting to monitor Seeking Alpha articles...", "INFO"
        )

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...", "INFO")
                break

            api_data = await fetch_articles_data()
            if api_data:
                await process_articles(api_data)
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
