import os
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from utils.logger import log_message

load_dotenv()


class TickerAnalysis(BaseModel):
    """Pydantic model for ticker analysis response"""

    found: bool
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    confidence: Optional[int] = None


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
            model="gpt-4o-mini",
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
            return TickerAnalysis(found=False)

        return parsed_result

    except Exception as e:
        log_message(f"Error analyzing image '{image_url}' for ticker: {e}", "ERROR")
        return TickerAnalysis(found=False)


async def analyze_company_name_for_ticker(
    company_names: List[str], title: str
) -> TickerAnalysis:
    """
    Analyzes company names using GPT to extract stock ticker information.

    Args:
        company_names (List[str]): List of company names to analyze
        title (str): Title or headline containing additional context

    Returns:
        TickerAnalysis: Object containing ticker information and confidence score
    """
    gpt_api_key = os.getenv("GPT_API_KEY")
    if not gpt_api_key:
        log_message("GPT API key not found in environment variables", "ERROR")
        return TickerAnalysis(found=False)

    try:
        client = OpenAI(api_key=gpt_api_key)

        # System prompt to guide the analysis
        system_prompt = """
        Analyze the company names and title to extract stock ticker information. 
        Focus on finding the most likely publicly traded company and its ticker.
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
        Company Names: {', '.join(company_names)}
        """

        response = client.beta.chat.completions.parse(
            model="gpt-4-mini",
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
            log_message(f"No ticker found for companies: {company_names}", "INFO")
            return TickerAnalysis(found=False)

        return parsed_result

    except Exception as e:
        log_message(
            f"Error analyzing companies {company_names} for ticker: {e}", "ERROR"
        )
        return TickerAnalysis(found=False)
