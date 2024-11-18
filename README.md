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
   CNBC_SCRAPER_ARTICLE_DATA_SHA=  # SHA for the article data in CNBC scraper
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

   # Citron Research scraper settings
   CITRON_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Citron
   CITRON_TELEGRAM_GRP=  # Telegram group ID for Citron

   # Kerrisdale Capital scraper settings
   KERRISDALE_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Kerrisdale
   KERRISDALE_TELEGRAM_GRP=  # Telegram group ID for Kerrisdale

   # Hindenburg Research scraper settings
   HINDENBURG_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Hindenburg
   HINDENBURG_TELEGRAM_GRP=  # Telegram group ID for Hindenburg

   # Bearcave ticker scraper settings
   BEARCAVE_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Hindenburg
   BEARCAVE_TELEGRAM_GRP=  # Telegram group ID for Hindenburg

   # Grizzly Media scraper settings
   GRIZZLY_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Grizzly
   GRIZZLY_TELEGRAM_GRP=  # Telegram group ID for Grizzly
   
   # Muddy Waters Media scraper settings
   MUDDY_WATERS_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Muddy Waters
   MUDDY_WATERS_TELEGRAM_GRP=  # Telegram group ID for Muddy Waters
   
   # Altucher ticker scraper settings
   ALTUCHER_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Altucher
   ALTUCHER_TELEGRAM_GRP=  # Telegram group ID for Altucher
   ALTUCHER_USERNAME= # Username for Altucher
   ALTUCHER_PASSWORD= # Username for Altucher
   ALTUCHER_COOKIE_TID= # Cokies TID grabbed from requests for Altucher
   ALTUCHER_COOKIE_ID= # Cokies ID grabbed from requests for Altucher
   ```

**Note:** Fill in the values for each variable as needed.

## Step 4: Set Up Credentials

1. **Create a folder named `cred/` in the root directory.**
2. **Place the following files in `cred/`:**

   - `gmail_credentials.json`  # Credentials for Gmail API (download this from Google Cloud API)
   - `cnbc_latest_article_id.json` # Contains {"article_id": xxxxxxxxx}
   - `fool_session.json` # Contains Session data from where you manually logged in with out a headless mode
   - `hedgeye_credentials.json` # Contains login accounts and proxies for hedgeye

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

  ```bash
  python stocknews_html_scraper.py
  ```

- To run the **Gmail ticker scraper**, use:

  ```bash
  python gmail_scraper.py
  ```

- To run the **CNBC ticker scraper**, use:

  ```bash
  python cnbc_scraper.py
  ```

  ```bash
  python cnbc_html_scraper.py
  ```

- To run the **Motley scraper**, use:

  ```bash
  python motley_fool_scraper.py
  ```

- To run the **Kerrisdale Capital scraper**, use:

  ```bash
  python kerrisdale_scraper.py
  ```

- To run the **Hindenburg Research scraper**, use:

  ```bash
  python hindenburg_scraper.py
  ```

- To run the **Citron Research scraper**, use:

  ```bash
  python citron_scraper.py
  ```

- To run the **Bearcave Ticker scraper**, use:

  ```bash
  python bearcave_scraper.py
  ```

- To run the **Altucher Ticker scraper**, use:

  ```bash
  python altucher_scraper.py
  ```

- To run the **Muddy Waters Media scraper**, use:

  ```bash
  python mudddy_waters_scraper.py
  ```

- To run the **Grizzly Media  scraper**, use:

  ```bash
  python grizzly_scraper.py
  ```


Make sure your `.env` file and `cred/` folder are properly set up before running these scripts.

## File Structure Overview

```plaintext
ticker_scraper/
├── cred/                       # Folder for credential files
│   ├── gmail_credentials.json
│   ├── gmail_token.json
│   ├── fool_session.json       # will be created upon first login
│   ├── hedgeye_credentials.json
├── data/                       # Folder to save scraper data to access later
├── log/                        # Folder for log files
├── utils/                      # Utility functions
│   ├── __init__.py
│   ├── logger.py               # Logger utility
│   ├── telegram_sender.py      # Telegram sending utility
│   ├── time_utils.py           # Time utility functions
│   ├── websocket_sender.py     # WebSocket sending utility
├── .env                        # Environment variables
├── .env.example                # Environment variables
├── .gitignore                  # Git ignore file
├── README.md                   # Project documentation
├── altucher_scraper.py         # Altucher ticker scraper
├── bearcave_scraper.py         # Bearcave ticker scraper
├── citron_scraper.py           # Citron media scraper
├── cnbc_html_scraper.py        # CNBC ticker scraper html implelementation
├── cnbc_scraper.py             # CNBC ticker scraper
├── gmail_scraper.py            # Gmail ticker scraper
├── grizzly_scraper.py          # Grizzly Media scraper
├── hedgeye_scraper.py          # Hedgeye article scraper
├── hindenburg_scraper.py       # Hindenburg pdf ticker scraper
├── kerrisdale_scraper.py       # Kerrisdale pdf ticker scraper
├── motley_fool_scraper.py      # Motley ticker scraper
├── mudddy_waters_scraper.py    # Muddy Waters Media scraper
├── oxfordclub_scraper.py       # OxfordClub ticker scraper
├── requirements.txt            # Project dependencies
├── stocknews_html_scraper.py   # StockNews ticker scraper html implelementation
└── stocknews_scraper.py        # StockNews ticker scraper
```

### Important Notes

- Ensure to create and go into the virtual environment before installing dependencies.
- Keep sensitive information secure and avoid sharing your `.env` and `cred/` files.
- Log files will be generated in the `log/` folder with a `.log` extension.
