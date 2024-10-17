import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime

import pytz
import requests
import schedule
import undetected_chromedriver as uc
from dotenv import load_dotenv
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constant
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("CNBC_SCRAPER_GMAIL_PASSWORD")
LATEST_ARTICLE_SHA = os.getenv("CNBC_SCRAPER_LATEST_ARTICLE_SHA")
ARTICLE_DATE_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATE_SHA")
SESSION_TOKEN = os.getenv("CNBC_SCRAPER_SESSION_TOKEN")
previous_articles = []


# Set up Selenium with Chrome options
options = uc.ChromeOptions()
options.add_argument("--maximize-window")
options.add_argument("--disable-search-engine-choice-screen")
options.add_argument("--blink-settings=imagesEnabled=false")


def mylousyprintfunction(eventdata):
    pass


def capture_login_response(message):
    global SESSION_TOKEN
    try:
        if "https://register.cnbc.com/auth/api/v3/signin" in message.get(
            "params", {}
        ).get("response", {}).get("url", ""):
            request_id = message.get("params", {}).get("requestId")
            if request_id:
                time.sleep(2)
                try:
                    response_body = driver.execute_cdp_cmd(
                        "Network.getResponseBody", {"requestId": request_id}
                    )
                    response_data = response_body.get("body", "")
                    try:
                        response_json = json.loads(response_data)
                    except json.JSONDecodeError:
                        response_json = {}
                    session_token = response_json.get("session_token", SESSION_TOKEN)
                    log_message(f"Intercepted Session Token: {session_token}", "INFO")
                except Exception as e:
                    if "No resource with given identifier found" in str(e):
                        log_message(
                            "Resource not found or cleared, unable to fetch the response body.",
                            "WARNING",
                        )
                    else:
                        raise e
    except Exception as e:
        log_message(f"Error in capture_login_response: {e}", "ERROR")


def get_new_session_token():
    global SESSION_TOKEN
    global driver

    try:
        driver = uc.Chrome(enable_cdp_events=True, options=options)
        driver.add_cdp_listener("Network.requestWillBeSent", mylousyprintfunction)
        driver.add_cdp_listener("Network.responseReceived", capture_login_response)

        driver.get("https://www.cnbc.com/investingclub/trade-alerts/")
        time.sleep(random.uniform(2, 5))

        scroll_pause_time = random.uniform(1, 3)
        for _ in range(3):
            driver.execute_script(f"window.scrollBy(0, {random.uniform(300, 500)});")
            time.sleep(scroll_pause_time)

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(scroll_pause_time)

        action = ActionChains(driver)
        sign_in_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "SignInMenu-signInMenu"))
        )
        action.move_to_element(sign_in_button).perform()
        time.sleep(random.uniform(1, 2))
        sign_in_button.click()

        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )

        if GMAIL_USERNAME is None or GMAIL_PASSWORD is None:
            log_message(f"GMAIL_USERNAME isn't availble in the env", "CRITICAL")
            sys.exit(1)

        email_input.send_keys(GMAIL_USERNAME)
        time.sleep(2)
        password_input = driver.find_element(By.NAME, "password")
        password_input.send_keys(GMAIL_PASSWORD)
        time.sleep(5)

        password_input.send_keys(Keys.ENTER)
        time.sleep(10)

        driver.get("https://www.cnbc.com/investingclub/trade-alerts/")

    except Exception as e:
        log_message(f"Failed to get a new session token: {e}", "ERROR")
        log_message(f"Using existing session token: {SESSION_TOKEN}", "INFO")

    finally:
        time.sleep(5)
        driver.quit()


def fetch_latest_articles(uid, session_token):
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {"hasICAccess": True, "uid": uid, "sessionToken": session_token}
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": LATEST_ARTICLE_SHA,
        }
    }
    params = {
        "operationName": "notifications",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    try:
        response = requests.get(base_url, params=params)
        response.raise_for_status()
        response_json = response.json()

        trade_alerts = []
        alert_ids = set()

        dtc_notifications = response_json.get("data", {}).get("dtcNotifications", {})
        if dtc_notifications:
            trade_alerts_raw = dtc_notifications.get("tradeAlerts", [])
            if trade_alerts_raw:
                for alert in trade_alerts_raw:
                    if alert.get("id") not in alert_ids:
                        trade_alerts.append(alert)
                        alert_ids.add(alert.get("id"))

        assets = []
        news_items = dtc_notifications.get("news", [])
        if news_items:
            for item in news_items:
                asset = item.get("asset")
                if asset:
                    section = asset.get("section", {})
                    if section.get("id") == 106983828:
                        asset_id = asset.get("id")
                        if asset_id not in alert_ids:
                            assets.append(
                                {
                                    "id": asset.get("id"),
                                    "title": asset.get("title"),
                                    "type": asset.get("type"),
                                    "tickerSymbols": asset.get("tickerSymbols"),
                                    "dateLastPublished": asset.get("dateLastPublished"),
                                    "url": asset.get("url"),
                                    "contentClassification": asset.get(
                                        "contentClassification"
                                    ),
                                    "section": section.get("title"),
                                }
                            )
                            alert_ids.add(asset_id)

        combined_alerts = trade_alerts + assets
        log_message(f"Parsed combined alerts: {combined_alerts}", "INFO")
        return combined_alerts if combined_alerts else []

    except (requests.RequestException, json.JSONDecodeError) as e:
        log_message(f"Error occurred: {e}", "ERROR")
        return []


async def check_for_new_alerts(prev_articles, current_articles, uid, session_token):
    if not prev_articles:
        return current_articles

    new_articles = []
    prev_ids = {article["id"] for article in prev_articles}
    if current_articles:
        for article in current_articles:
            if (
                article["id"] not in prev_ids
                or article["dateLastPublished"] > prev_articles[0]["dateLastPublished"]
            ):
                article_data = get_article_data(article["id"], uid, session_token)
                if article_data:
                    article["article_data"] = article_data
                    published_date = datetime.strptime(
                        article["dateLastPublished"], "%Y-%m-%dT%H:%M:%S%z"
                    )
                    article_timezone = published_date.tzinfo
                    current_time = datetime.now(pytz.utc).astimezone(article_timezone)
                    message = (
                        f"<b>New Article Alert!</b>\n"
                        f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"<b>Title:</b> {article['title']}\n"
                        f"<b>Content:</b> {article['article_data']}\n"
                    )
                    await send_telegram_message(
                        message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                    )
                    await send_ws_message(
                        {
                            "name": "CNBC",
                            "type": "Buy",
                            "ticker": article["title"],
                            "sender": "cnbc",
                        },
                        WS_SERVER_URL,
                    )
            new_articles.append(article)
    return current_articles


def get_article_data(article_id, uid, session_token):
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {
        "id": article_id,
        "uid": uid,
        "sessionToken": session_token,
        "pid": 33,
        "bedrockV3API": True,
        "sponsoredProExperienceID": "",
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": ARTICLE_DATE_SHA,
        }
    }
    params = {
        "operationName": "getArticleData",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    response = requests.get(base_url, params=params)

    if response.status_code == 200:
        response_json = response.json()
        is_authenticated = (
            response_json.get("data", {})
            .get("article", {})
            .get("body", {})
            .get("isAuthenticated", False)
        )

        if not is_authenticated:
            log_message(
                "Authentication required. Please provide a valid session token.",
                "WARNING",
            )
            return None

        article_body = (
            response_json.get("data", {})
            .get("article", {})
            .get("body", {})
            .get("content", [])
        )
        if article_body:
            for content_block in article_body:
                if content_block.get("tagName") == "div":
                    for child in content_block.get("children", []):
                        if child.get("tagName") == "blockquote":
                            paragraph = child.get("children", [])[0]
                            if paragraph.get("tagName") == "p":
                                text = "".join(
                                    [
                                        (
                                            part
                                            if isinstance(part, str)
                                            else part.get("children", [])[0]
                                        )
                                        for part in paragraph.get("children", [])
                                    ]
                                )
                                return text
    else:
        log_message(f"Request failed with status code {response.status_code}", "ERROR")
        return None


def schedule_daily_task():
    est = pytz.timezone("US/Eastern")
    now = datetime.now(est)
    log_message(f"Current time: {now}", "INFO")

    schedule.every().day.at("00:00").do(get_new_session_token)

    latest_articles = fetch_latest_articles(GMAIL_USERNAME, SESSION_TOKEN)
    if latest_articles:
        for latest_article in latest_articles:
            article_data = get_article_data(
                latest_article["id"], GMAIL_USERNAME, SESSION_TOKEN
            )
            if article_data:
                latest_article["article_data"] = article_data
                published_date = datetime.strptime(
                    latest_article["dateLastPublished"], "%Y-%m-%dT%H:%M:%S%z"
                )
                article_timezone = published_date.tzinfo
                current_time = datetime.now(pytz.utc).astimezone(article_timezone)
                start_time = time.time()

                message = (
                    f"<b>New Article Alert!</b>\n"
                    f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"<b>Title:</b> {latest_article['title']}\n"
                    f"<b>Content:</b> {latest_article['article_data']}\n"
                )
                log_message(message, "INFO")

                end_time = time.time()
                elapsed_time = end_time - start_time
                log_message(
                    f"Time taken to detect and send message: {elapsed_time:.2f} seconds",
                    "INFO",
                )

    global previous_articles
    previous_articles = latest_articles
    first_time = True
    while True:
        try:
            start_time = time.time()
            schedule.run_pending()
            time.sleep(0.5)
            current_time = datetime.now(est)
            current_hour = current_time.hour

            if 8 <= current_hour < 17:
                if first_time:
                    log_message(f"Est time now: {datetime.now(est)}", "INFO")
                    log_message("Inside working hours.", "INFO")
                    first_time = False

                current_articles = fetch_latest_articles(GMAIL_USERNAME, SESSION_TOKEN)
                previous_articles = asyncio.run(
                    check_for_new_alerts(
                        previous_articles,
                        current_articles,
                        GMAIL_USERNAME,
                        SESSION_TOKEN,
                    )
                )

                end_time = time.time()

                if len(previous_articles) != len(current_articles):
                    elapsed_time = end_time - start_time
                    log_message(
                        f"Time taken to detect and send message: {elapsed_time:.2f} seconds",
                        "INFO",
                    )
            else:
                first_time = True
                log_message(f"Est time now: {datetime.now(est)}", "INFO")
                log_message("Outside working hours, sleeping for 60 seconds", "INFO")
                time.sleep(60)

        except Exception as e:
            log_message(f"An error occurred: {e}. Retrying in 3 seconds...", "ERROR")
            time.sleep(3)


if __name__ == "__main__":
    schedule_daily_task()
