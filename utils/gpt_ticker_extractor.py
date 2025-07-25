import asyncio
import inspect
import os
import threading
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message

load_dotenv()

GPT_NOTIFY_BOT_TOKEN = os.getenv("GPT_NOTIFY_BOT_TOKEN")
GPT_NOTIFY_GRP = os.getenv("GPT_NOTIFY_GRP")


class TickerAnalysis(BaseModel):
    """Pydantic model for ticker analysis response"""

    found: bool
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    confidence: Optional[int] = None


def get_calling_script():
    """Get the name of the script that called this function"""
    try:
        frame = inspect.currentframe()
        caller_frame = frame.f_back.f_back if frame and frame.f_back else None
        if caller_frame and caller_frame.f_globals.get("__file__"):
            script_path = caller_frame.f_globals["__file__"]
            script_name = os.path.basename(script_path)
            return os.path.splitext(script_name)[0]
    except:
        pass
    return "Unknown Script"


async def send_analysis_notification(
    analysis_type: str, input_data: str, result: TickerAnalysis, script_name: str
):
    """Send Telegram notification about the analysis result"""
    if not GPT_NOTIFY_BOT_TOKEN or not GPT_NOTIFY_GRP:
        return

    try:
        current_time = datetime.now()

        if analysis_type == "image":
            alert_message = f"<b>GPT Image Analysis</b>\n\n"
            alert_message += f"<b>Script:</b> {script_name}\n"
            alert_message += (
                f"<b>Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            alert_message += f"<b>Image URL:</b> {input_data}\n\n"
        else:
            alert_message = f"<b>GPT Company Analysis</b>\n\n"
            alert_message += f"<b>Script:</b> {script_name}\n"
            alert_message += (
                f"<b>Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            alert_message += f"<b>Input Data:</b> {input_data}\n\n"

        alert_message += "<b>Analysis Result:</b>\n"
        if result.found:
            alert_message += f"<b>Found:</b> Yes\n"
            alert_message += f"<b>Ticker:</b> {result.ticker}\n"
            alert_message += f"<b>Company:</b> {result.company_name}\n"
            alert_message += f"<b>Confidence:</b> {result.confidence}%"
        else:
            alert_message += "<b>Found:</b> No ticker identified"

        message_id = f"{analysis_type}_{int(current_time.timestamp())}"
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Good Analysis",
                        "callback_data": f"rate_good_{message_id}",
                    },
                    {
                        "text": "Poor Analysis",
                        "callback_data": f"rate_bad_{message_id}",
                    },
                ]
            ]
        }

        await send_telegram_message(
            alert_message,
            GPT_NOTIFY_BOT_TOKEN,
            GPT_NOTIFY_GRP,
            reply_markup=inline_keyboard,
        )

    except Exception as e:
        log_message(f"Error sending Telegram notification: {e}", "ERROR")


def run_notification_async(
    analysis_type: str, input_data: str, result: TickerAnalysis, script_name: str
):
    """Run the notification in a separate thread with its own event loop"""

    def notification_thread():
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            new_loop.run_until_complete(
                send_analysis_notification(
                    analysis_type, input_data, result, script_name
                )
            )
        except Exception as e:
            log_message(f"Error in notification thread: {e}", "ERROR")
        finally:
            try:
                new_loop.close()
            except:
                pass

    thread = threading.Thread(target=notification_thread, daemon=True)
    thread.start()


async def analyze_image_for_ticker(image_url: str) -> TickerAnalysis:
    """
    Analyzes an image using GPT-4 Vision to extract stock ticker and company information.

    Args:
        image_url (str): URL of the image to analyze

    Returns:
        TickerAnalysis: Object containing ticker information and confidence score
    """
    gpt_api_key = os.getenv("GPT_API_KEY")
    if not gpt_api_key:
        log_message("GPT API key not found in environment variables", "ERROR")
        return TickerAnalysis(found=False)

    script_name = get_calling_script()

    try:
        client = OpenAI(api_key=gpt_api_key)

        # System prompt to guide the analysis
        system_prompt = """
        Analyze the image and extract stock ticker information. Respond in JSON format like:
        {
            "found": true,
            "ticker": "AAPL",
            "company_name": "Apple Inc.",
            "confidence": 0 # How confident you are on the ticker you found 0 to 100
        }
        If no ticker is found, respond with:
        {
            "found": false,
            "ticker": null,
            "company_name": null,
            "confidence": 0
        }
        """

        response = client.beta.chat.completions.parse(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        }
                    ],
                },
            ],
            max_tokens=300,
            response_format=TickerAnalysis,
        )

        # Parse the response
        parsed_result = response.choices[0].message.parsed

        if not parsed_result or not parsed_result.found:
            log_message(f"No ticker found in image: {image_url}", "INFO")
            result = TickerAnalysis(found=False)
        else:
            result = parsed_result

        run_notification_async("image", image_url, result, script_name)

        return result

    except Exception as e:
        log_message(f"Error analyzing image '{image_url}' for ticker: {e}", "ERROR")
        result = TickerAnalysis(found=False)

        run_notification_async(
            "image", f"{image_url} (ERROR: {str(e)})", result, script_name
        )

        return result


async def analyze_company_name_for_ticker(
    tags: List[str], title: str
) -> TickerAnalysis:
    """
    Analyzes tags and title using GPT to extract stock ticker information.

    Args:
        tags (List[str]): List of tags to analyze
        title (str): Title or headline containing additional context

    Returns:
        TickerAnalysis: Object containing ticker information and confidence score
    """
    gpt_api_key = os.getenv("GPT_API_KEY")
    if not gpt_api_key:
        log_message("GPT API key not found in environment variables", "ERROR")
        return TickerAnalysis(found=False)

    script_name = get_calling_script()

    try:
        client = OpenAI(api_key=gpt_api_key)

        # System prompt to guide the analysis
        system_prompt = """
        Analyze the Tags and title to extract stock ticker information. 
        Focus on finding the most likely publicly traded company from the title and its ticker.
        Respond in JSON format like:
        {
            "found": true,
            "ticker": "AAPL",
            "company_name": "Apple Inc.",
            "confidence": 0 # How confident you are on the ticker you found 0 to 100
        }
        If no ticker can be confidently determined, respond with:
        {
            "found": false,
            "ticker": null,
            "company_name": null,
            "confidence": 0
        }
        """

        # Prepare the context for analysis
        analysis_context = f"""
        Title: {title}
        Tags: {', '.join(tags)}
        """

        response = client.beta.chat.completions.parse(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": analysis_context},
            ],
            max_tokens=300,
            response_format=TickerAnalysis,
        )

        # Parse the response
        parsed_result = response.choices[0].message.parsed
        if not parsed_result or not parsed_result.found:
            log_message(f"No ticker found for title: {title}", "INFO")
            result = TickerAnalysis(found=False)
        else:
            result = parsed_result

        input_data = f"\n   Title: {title}"
        if len(tags) > 0:
            input_data += f"\n   Tags: {', '.join(tags)}"

        run_notification_async("company", input_data, result, script_name)

        return result

    except Exception as e:
        log_message(f"Error analyzing title: {title} for ticker: {e}", "ERROR")
        result = TickerAnalysis(found=False)

        input_data = f"\n   Title: {title}"
        if len(tags) > 0:
            input_data += f"\n   Tags: {', '.join(tags)}"
        input_data += f"\n   (ERROR: {str(e)})"

        run_notification_async("company", input_data, result, script_name)

        return result
