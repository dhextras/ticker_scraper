import json
import time

from DrissionPage import ChromiumOptions, ChromiumPage

from utils.logger import log_message


def bypasser(url, cookies_file_path):
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

        options = ChromiumOptions()
        # options.set_argument("--headless")

        driver = ChromiumPage(addr_or_opts=options)
        driver.get(url)
        time.sleep(5)

        max_retries = 10
        try_count = 0

        while "just a moment" in driver.title.lower():
            if try_count >= max_retries:
                log_message(
                    f"Failed to bypass Cloudflare after {max_retries} attempts", "ERROR"
                )
                return False

            log_message(
                f"Attempt {try_count + 1}: Cloudflare protection detected", "INFO"
            )

            # Locate and click the Cloudflare verification button
            try:
                button = _find_cloudflare_button(driver)

                if button:
                    button.click()
                    log_message("Verification button clicked", "INFO")
                else:
                    log_message("Verification button not found", "WARNING")
                    return False

                for _ in range(30):
                    time.sleep(1)
                    if not "just a moment" in driver.title.lower():
                        break

                try_count += 1

            except Exception as e:
                log_message(f"Error during bypass attempt: {e}", "ERROR")
                return False

        cookies_data = driver.cookies()
        cookies_dict = {cookie["name"]: cookie["value"] for cookie in cookies_data}
        cookies = {
            "cf_clearance": cookies_dict.get("cf_clearance", None),
            "user_agent": driver.user_agent,
        }

        with open(cookies_file_path, "w") as f:
            json.dump(cookies, f, indent=2)

        log_message(
            f"Cloudflare bypass successful. Cookies saved to {cookies_file_path}",
            "INFO",
        )
        return True

    except Exception as e:
        log_message(f"Unexpected error in bypass_cloudflare: {e}", "ERROR")
        return False
    finally:
        if "driver" in locals():
            driver.close()


def _find_cloudflare_button(driver):
    """
    Directly / Recursively search for Cloudflare verification button through shadow roots.

    Args:
        driver (ChromiumPage): The Chromium webdriver

    Returns:
        WebElement or None: The Cloudflare verification button
    """

    def search_shadow_root_recursively(element):
        # Check shadow root for iframe or input
        log_message(f"Basic input search failed seraching recursively in body", "ERROR")
        if element.shadow_root:
            if element.shadow_root.child().tag == "iframe":
                return element.shadow_root.child()

            input_ele = element.shadow_root.ele("tag:input")
            if input_ele and not "NoneElement" in str(type(input_ele)):
                return input_ele

        # Recursively search children
        for child in element.children():
            result = search_shadow_root_recursively(child)
            if result and not "NoneElement" in str(type(result)):
                return result

        return None

    # Search for the first direct input
    eles = driver.eles("tag:input")
    for ele in eles:
        if "name" in ele.attrs.keys() and "type" in ele.attrs.keys():
            if "turnstile" in ele.attrs["name"] and ele.attrs["type"] == "hidden":
                button = (
                    ele.parent()
                    .shadow_root.child()("tag:body")
                    .shadow_root("tag:input")
                )
                if not "NoneElement" in str(type(button)):
                    return button

    # Search from body element recursively
    body_ele = driver.ele("tag:body")
    button = search_shadow_root_recursively(body_ele)

    return button
