# TickerScraper Setup Guide

Follow these steps to set up and run the TickerScraper project.

## Prerequisites

Make sure you have the following installed:

- **Python** (preferably version 3.6 or higher)
- **pip** (Python package installer)
- **virtualenv** (to create isolated Python environments)

If you don't have Python and pip installed, you can install them using the following command:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv
```

## Step 1: Setup the Virtual Environment

1. **Create the virtual environment:**

   ```bash
   python3 -m venv venv
   ```

2. **Activate the virtual environment:**

   ```bash
   source venv/bin/activate
   ```

## Step 2: Install Dependencies

Install the required Python packages using:

```bash
pip install -r requirements.txt
```

## Step 3: Create a `.env` File

1. **Create a file named `.env` or copy the `.env.example` in the root directory.**
2. **Add the following details:**

   ```plaintext
   WS_SERVER_URL=  # WebSocket server URL

   # OxfordClub scraper settings
   OXFORDCLUB_TELEGRAM_BOT_TOKEN=  # Telegram bot token for OxfordClub
   OXFORDCLUB_TELEGRAM_GRP=  # Telegram group ID for OxfordClub
   OXFORDCLUB_USERNAME=  # Username for OxfordClub login
   OXFORDCLUB_PASSWORD=  # Password for OxfordClub login

   # StockNews scraper settings
   STOCKNEWS_TELEGRAM_BOT_TOKEN=  # Telegram bot token for StockNews
   STOCKNEWS_TELEGRAM_GRP=  # Telegram group ID for StockNews

   # Gmail scraper settings
   GMAIL_SCRAPER_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Gmail scraper
   GMAIL_SCRAPER_TELEGRAM_GRP=  # Telegram group ID for Gmail scraper

   # CNBC scraper settings
   CNBC_SCRAPER_TELEGRAM_BOT_TOKEN=  # Telegram bot token for CNBC scraper
   CNBC_SCRAPER_TELEGRAM_GRP=  # Telegram group ID for CNBC scraper
   CNBC_SCRAPER_GMAIL_USERNAME=  # Gmail username for CNBC scraper
   CNBC_SCRAPER_GMAIL_PASSWORD=  # Gmail password for CNBC scraper
   CNBC_SCRAPER_LATEST_ARTICLE_SHA=  # SHA for the latest article in CNBC scraper
   CNBC_SCRAPER_ARTICLE_DATE_SHA=  # SHA for the article date in CNBC scraper
   CNBC_SCRAPER_SESSION_TOKEN=  # Session token for CNBC scraper

   # Hedgeye scraper settings
   HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Hedgeye scraper
   HEDGEYE_SCRAPER_TELEGRAM_GRP=  # Telegram group ID for Hedgeye scraper

   # Motley fool scraper settings
   FOOL_SCRAPER_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Motley fool scraper
   FOOL_SCRAPER_TELEGRAM_GRP=  # Telegram group ID for Motley fool scraper
   FOOL_USERNAME= # Username for Motley fool
   FOOL_PASSWORD= # password for Motley fool
   FOOL_API_KEY= # API key for the grphql request
   FOOL_GRAPHQL_HASH= # Article latestt sha
   ```

**Note:** Fill in the values for each variable as needed.

## Step 4: Set Up Credentials

1. **Create a folder named `cred/` in the root directory.**
2. **Place the following files in `cred/`:**

   - `gmail_credentials.json`  # Credentials for Gmail API (download this from Google Cloud API)

Ensure that these files are named exactly as specified.

## Step 5: Install Google Chrome and ChromeDriver (Optional - Needed for Hedgeye, Motley)

These steps are only needed if you are using the **Hedgeye** scraper.

1. **Install Google Chrome:**

   ```bash
   cd /tmp/
   wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
   sudo dpkg -i google-chrome-stable_current_amd64.deb
   sudo apt-get install -f
   ```

2. **Install ChromeDriver:**

   Replace `VERSION` with the version of Chrome you installed (e.g., `130.0.6723.58`):

   ```bash
   cd /tmp/
   sudo wget https://chromedriver.storage.googleapis.com/VERSION/chromedriver_linux64.zip
   sudo unzip chromedriver_linux64.zip
   sudo mv chromedriver /usr/bin/chromedriver
   ```

   ### Verify Installation

   Check that ChromeDriver is installed correctly by running:

   ```bash
   chromedriver --version
   ```

## Step 6: Run the Scripts

You can run each of the scripts based on your needs:

- To run the **OxfordClub ticker scraper**, use:

  ```bash
  python oxfordclub_scraper.py
  ```

- To run the **StockNews ticker scraper**, use:

  ```bash
  python stocknews_scraper.py
  ```

- To run the **Gmail ticker scraper**, use:

  ```bash
  python gmail_scraper.py
  ```

- To run the **CNBC ticker scraper**, use:

  ```bash
  python cnbc_scraper.py
  ```

- To run the **Motley scraper**, use:

  ```bash
  python motley_fool_scraper.py
  ```


Make sure your `.env` file and `cred/` folder are properly set up before running these scripts.

## File Structure Overview

```plaintext
your_project/
├── cred/                    # Folder for credential files
│   ├── gmail_credentials.json
│   ├── gmail_token.json
│   ├── fool_session.json    # will be created upon first login
│   ├── hedgeye_credentials.json
├── data/                    # Folder to save scraper data to access later
├── log/                     # Folder for log files
├── utils/                   # Utility functions
│   ├── __init__.py
│   ├── logger.py            # Logger utility
│   ├── telegram_sender.py   # Telegram sending utility
│   ├── time_utils.py        # Time utility functions
│   ├── websocket_sender.py  # WebSocket sending utility
├── .env                     # Environment variables
├── .env.example             # Environment variables
├── .gitignore               # Git ignore file
├── README.md                # Project documentation
├── cnbc_scraper.py          # CNBC ticker scraper
├── gmail_scraper.py         # Gmail ticker scraper
├── oxfordclub_scraper.py    # OxfordClub ticker scraper
├── requirements.txt         # Project dependencies
├── stocknews_scraper.py     # StockNews ticker scraper
├── hedgeye_scraper.py       # Hedgeye article scraper
└── motley_fool_scraper.py   # Motley ticker scraper
```

### Important Notes

- Ensure to create and go into the virtual environment before installing dependencies.
- Keep sensitive information secure and avoid sharing your `.env` and `cred/` files.
- Log files will be generated in the `log/` folder with a `.log` extension.
