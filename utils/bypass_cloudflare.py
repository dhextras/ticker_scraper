import json

import requests
from utils.logger import log_message


def bypasser(api_key, server_url, url, cookies_file_path):
    """
    Bypass Cloudflare protection and save cookies to a JSON file.

    Args:
        url (str): The URL to access
        cookies_file_path (str): Path to save the cookies JSON file

    Returns:
        bool: True if bypass was successful, False otherwise
    """
    try:
        # Clear the initial file with empty data
        try:
            with open(cookies_file_path, "w") as f:
                json.dump({}, f, indent=2)
        except:
            pass

        headers = {"Server-API-Key": api_key, "Content-Type": "application/json"}

        data = json.dumps({"url": url})

        response = requests.post(f"{server_url}/bypass", headers=headers, data=data)
        response_json = response.json()
        cookies_data = {}

        if response.status_code == 200:
            if response_json["status"] == "failed":
                log_message(
                    f"Cloudflare bypass error, message: \n{response_json}",
                    "ERROR",
                )
                return False
            elif response_json["status"] == "error":
                log_message(
                    f"Some thing else broked in server with 200, message: \n{response_json}",
                    "ERROR",
                )
                return False
            elif response_json["status"] == "success":
                cookies_data = response_json.get("cookies", {})
            else:
                log_message(
                    f"Unknown status for the response, message: \n{response_json}",
                    "ERROR",
                )
                return False
        else:
            log_message(
                f"Server error happaned, status code: {response.status_code}, message: \n{response_json}",
                "ERROR",
            )
            return False

        with open(cookies_file_path, "w") as f:
            json.dump(cookies_data, f, indent=2)

        log_message(
            f"Cloudflare bypass successful. Cookies saved to {cookies_file_path}",
            "INFO",
        )
        return True

    except Exception as e:
        log_message(
            f"Unexpected error in sending request to bypasser server: {e}", "ERROR"
        )
        return False
