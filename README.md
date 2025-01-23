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
sudo apt install python3 python3-pip python3-venv python3-dev python3-tk
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
   GPT_API_KEY= # Api key for gpt

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
   CNBC_SCRAPER_LATEST_ASSETS_SHA= # SHA for latest assets from jim camer
   CNBC_SCRAPER_LATEST_ARTICLE_SHA= # SHA for latest aritcle
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

   # Banyan image scraper settings
   BANYAN_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Banayan image
   BANYAN_TELEGRAM_GRP=  # Telegram group ID for Banayan image

   # Navallier Old scraper settings
   INVESTOR_PLACE_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Navallier Old
   INVESTOR_PLACE_TELEGRAM_GRP= # Telegram group ID for Navallier Old
   IPA_LOGIN_COOKIE= # Cookie for Navallier Old

   # Zacks Trading Service settings
   ZACKS_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Zacks alerts
   ZACKS_TELEGRAM_GRP=  # Telegram group ID for Zacks alerts
   ZACKS_USERNAME=  # Zacks login username
   ZACKS_PASSWORD=  # Zacks login password

   # Youtube Channel Monitor settings
   YOUTUBE_TELEGRAM_BOT_TOKEN= # Telegram bot token for youtube channel monitor
   YOUTUBE_TELEGRAM_GRP= # Telegram group ID for youtube channel monitor
   YOUTUBE_PLAYLIST_ID= # Play list id of the channel you wants to monitor

   # Beta Ville Scraper settings
   BETA_VILLE_TELEGRAM_BOT_TOKEN= # Telegram bot token for beta ville scraper
   BETA_VILLE_TELEGRAM_GRP= # Telegram group ID for beta ville scraper

   # Blue Orca Scraper settings
   BLUEORCA_TELEGRAM_BOT_TOKEN= # Telegram bot token for blue orca scraper
   BLUEORCA_TELEGRAM_GRP= # Telegram group ID for blue orca scraper

   # Jehoshaphat Reaserch Scraper settings
   JEHOSHAPHAT_TELEGRAM_BOT_TOKEN= # Telegram bot token for Jehoshaphat Reaserch scraper
   JEHOSHAPHAT_TELEGRAM_GRP= # Telegram group ID for Jehoshaphat Reaserch scraper

   # Friendly Bear Research Scraper settings
   FRIENDLY_BEAR_TELEGRAM_BOT_TOKEN=  # Telegram bot token for Friendly Bear Research scraper
   FRIENDLY_BEAR_TELEGRAM_GRP=        # Telegram group ID for Friendly Bear Research scraper

   # Viceroy Research Scraper settings
   VICEROY_TELEGRAM_BOT_TOKEN=        # Telegram bot token for Viceroy Research scraper
   VICEROY_TELEGRAM_GRP=              # Telegram group ID for Viceroy Research scraper

   # Iceberg Research Scraper settings
   ICEBERG_TELEGRAM_BOT_TOKEN=        # Telegram bot token for Iceberg Research scraper
   ICEBERG_TELEGRAM_GRP=              # Telegram group ID for Iceberg Research scraper

   # Hunter Brook Research Scraper settings
   HUNTER_BROOK_TELEGRAM_BOT_TOKEN=        # Telegram bot token for Hunter Brook Research scraper
   HUNTER_BROOK_TELEGRAM_GRP=              # Telegram group ID for Hunter Brook Research scraper

   # Mariner Research Scraper settings
   MARINER_TELEGRAM_BOT_TOKEN=        # Telegram bot token for Mariner Research scraper
   MARINER_TELEGRAM_GRP=              # Telegram group ID for Mariner Research scraper
   ```

**Note:** Fill in the values for each variable as needed.

## Step 4: Set Up Credentials

1. **Create a folder named `cred/` in the root directory.**
2. **Place the following files in `cred/`:**

   - `gmail_credentials.json`  # Credentials for Gmail API (download this from Google Cloud API)
   - `cnbc_latest_article_id.json` # Contains {"article_id": xxxxxxxxx}
   - `fool_session.json` # Contains Session data from where you manually logged in with out a headless mode
   - `hedgeye_credentials.json` # Contains login accounts for hedgeye
   - `proxies.json` # Contains all the proxies for all scripts
   - `youtube_api_keys.json` # Contains youtube api keys

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

Here’s a simplified and concise README for running your scripts:

---

# Scraper Instructions

To run any of the scrapers, use the following general command format:

```bash
python <script_name>.py
```

### Available Scripts:

### 1. **Ticker Scrapers**
- **OxfordClub:** `oxfordclub_scraper.py`
- **Oxford Communique:** `oxford_communique_scraper.py`
- **Oxford Income Letter:** `oxford_income_let_scraper.py`
- **StockNews:** `stocknews_scraper.py`, `stocknews_html_scraper.py`
- **CNBC:** `cnbc_scraper.py`, `cnbc_html_scraper.py`
- **Motley Fool:** `motley_fool_scraper.py`
- **Navallier:** `navallier_old_scraper.py`, `navallier_old_csv_scraper.py`
- **Zacks:** `zack_html_ts_scraper.py`, `zack_widget_scraper.py`
- **Hedgeye:** `hedgeye_scraper.py`, `hedgeye_html_scraper.py`

### 2. **PDF & Image Scrapers**
- **Blue Orca:** `blueorca_scraper.py`
- **Hindenburg:** `hindenburg_scraper.py`
- **Jehoshaphat Research:** `jehoshaphat_scraper.py` (*requires GUI setup*)
- **Friendly Bear:** `friendly_bear_scraper.py`
- **HunterBrook Research:** `hunterbrook_scraper.py`
- **HunterBrook Post Scraper:** `hunterbrook_post_scraper.py`
- **Iceberg Research:** `iceberg_scraper.py`
- **Kerrisdale Capital:** `kerrisdale_scraper.py`
- **Mariner Research:** `mariner_scraper.py`
- **Viceroy Research:** `viceroy_scraper.py`
- **Citron Research:** `citron_scraper.py`
- **Muddy Waters Media:** `mudddy_waters_scraper.py` (*requires GUI setup*)
- **Grizzly Media:** `grizzly_scraper.py` (*requires GUI setup*)
- **Bearcave:** `bearcave_scraper.py`, `bearcave_xml_scraper.py`
- **Beta Ville:** `beta_ville_scrper.py`

### 3. **Email Scrapers**
- **Gmail:** `gmail_scraper.py`, `gmail_scraper_a2.py`

### 4. **Image Scrapers**
- **Banyan Image Scraper:** `banyan_image_scraper.py`

### 5. **Specialized Scrapers**
- **YouTube Channel Monitor:** `youtube_channel_monitor.py`

> For GUI-dependent scripts (e.g., Muddy Waters or Grizzly Media, Jehoshaphat Research), ensure you have the GUI environment set up as described in [Here](./gui_setup.md)


Make sure your `.env` file and `cred/` folder are properly set up before running these scripts.

## Step 7: Install GUI

> These are only for grizzly & muddy waters ( the ones that need cloudflare bypass )

  1. Install GUI and setup [gnome for the ubuntu server](./gui_setup.md)
  2. Install google chrome from step 5
  3. Make sure to make the chrome full size after opening via drissionPage
      ```bash
      python
      ```

      ```python
      from DrissionPage import ChromiumPage
      driver = ChromiumPage()
      ```
  4. Open tmux and setup everything in gui then, open it via ssh and run the script there

## File Structure Overview

```plaintext
ticker_scraper/
├── cred/                         # Folder for credential files
│   ├── gmail_credentials.json
│   ├── fool_session.json         # will be created upon first login
│   ├── gmail_token_a1.json
│   ├── gmail_token_a2.json
│   ├── hedgeye_credentials.json
│   ├── youtube_api_keys.json
│   └── proxies.json
├── data/                         # Folder to save scraper data to access later
├── log/                          # Folder for log files
├── utils/                        # Utility functions
│   ├── __init__.py
│   ├── logger.py                 # Logger utility
│   ├── telegram_sender.py        # Telegram sending utility
│   ├── time_utils.py             # Time utility functions
│   └── websocket_sender.py       # WebSocket sending utility
├── .env                          # Environment variables
├── .env.example                  # Environment variables
├── .gitignore                    # Git ignore file
├── README.md                     # Project documentation
├── altucher_scraper.py           # Altucher ticker scraper
├── banyan_image_scraper.py       # Banayan image scraper
├── bearcave_scraper.py           # Bearcave ticker scraper
├── bearcave_xml_scraper.py       # Bearcave xml method ticker scraper
├── beta_ville_scrper.py          # Beta Ville Scraper
├── blueorca_scraper.py           # Blue Orca pdf & image ticker scraper
├── citron_scraper.py             # Citron media scraper
├── cnbc_html_scraper.py          # CNBC ticker scraper html implelementation
├── cnbc_scraper.py               # CNBC ticker scraper
├── friendly_bear_scraper.py      # Friendly Bear Research pdf & image ticker scraper
├── gmail_scraper.py              # Gmail ticker scraper
├── gmail_scraper_a2.py           # Gmail ticker scraper second account
├── grizzly_scraper.py            # Grizzly Media scraper
├── gui_setup.md                  # Setting up gui for ubuntu server
├── hedgeye_html_scraper.py       # Hedgeye html scraper implementation
├── hedgeye_scraper.py            # Hedgeye article scraper
├── hindenburg_scraper.py         # Hindenburg pdf & image ticker scraper
├── hunterbrook_scraper.py        # Hunterbrook Research pdf & image ticker scraper
├── hunterbrook_post_scraper.py   # Hunterbrook Post ticker scraper
├── iceberg_scraper.py            # Iceberg Research pdf & image ticker scraper
├── jehoshaphat_scraper.py        # Jehoshaphat pdf & image ticker scraper
├── kerrisdale_scraper.py         # Kerrisdale pdf & image ticker scraper
├── mariner_scraper.py            # Mariner Research pdf & image ticker scraper
├── motley_fool_scraper.py        # Motley ticker scraper
├── mudddy_waters_scraper.py      # Muddy Waters Media scraper
├── navallier_old_csv_scraper.py  # Navallier protfolio CSV scraper
├── navallier_old_scraper.py      # Navallier old ticker scraper
├── oxfordclub_scraper.py         # OxfordClub ticker scraper
├── oxford_communique_scraper.py  # Oxford Communique scraper
├── oxford_income_let_scraper.py  # Oxford Income Letter scraper
├── requirements.txt              # Project dependencies
├── stocknews_html_scraper.py     # StockNews ticker scraper html implelementation
├── stocknews_scraper.py          # StockNews ticker scraper
├── viceroy_scraper.py            # Viceroy Research pdf & image ticker scraper
├── youtube_channel_monitor.py    # Moon Market channel monitor
├── zack_html_ts_scraper.py       # Zacks trading service html implementation
└── zack_widget_scraper.py        # Zacks widget3 ticker scraper

```

### Important Notes

- Ensure to create and go into the virtual environment before installing dependencies.
- Keep sensitive information secure and avoid sharing your `.env` and `cred/` files.
- Log files will be generated in the `log/` folder with a `.log` extension.
