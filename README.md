# TickerScraper Setup Guide

Follow these steps to set up and run the TickerScraper project.

## Prerequisites

Make sure you have the following installed:

- **Python** (preferably version 3.6 or higher)
- **pip** (Python package installer)
- **virtualenv** (to create isolated Python environments)

If you don't have Python and pip installed, you can install them using the following command:

```bash
sudo apt update -y
sudo apt install python3 python3-pip python3-venv python3-dev python3-tk -y
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
   WS_SERVER_URL=
   GPT_API_KEY=

   # GPT Analysis Configuration
   GPT_NOTIFY_BOT_TOKEN=
   GPT_NOTIFY_GRP=

   # TCP Client Configuration
   TCP_HOST=
   TCP_PORT=
   TCP_USERNAME=
   TCP_SECRET=

   # Ticker Deck Configuration
   TICKER_DECK_WS_URL=
   TICKER_DECK_AUTH_URL=
   TICKER_DECK_USERNAME=
   TICKER_DECK_PASSWORD=

   # Error Notify bot settings
   ERROR_NOTIFY_BOT_TOKEN=
   ERROR_NOTIFY_GRP=

   # OxfordClub scraper settings
   OXF0RDCLUB_TELEGRAM_BOT_TOKEN=
   OXF0RDCLUB_TELEGRAM_GRP=
   OXF0RDCLUB_USERNAME=
   OXF0RDCLUB_PASSWORD=
   OXFORD_MICROCAP_USERNAME=
   OXFORD_MICROCAP_PASSWORD=
   OXFORD_WS_SERVER_URL=

   # StockNews scraper settings
   STOCKNEWS_TELEGRAM_BOT_TOKEN=
   STOCKNEWS_TELEGRAM_GRP=

   # Gmail scraper settings
   GMAIL_SCRAPER_TELEGRAM_BOT_TOKEN=
   GMAIL_SCRAPER_TELEGRAM_GRP=

   # CNBC scraper settings
   CNBC_SCRAPER_TELEGRAM_BOT_TOKEN=
   CNBC_SCRAPER_TELEGRAM_GRP=
   CNBC_NEWS_TELEGRAM_GRP=
   CNBC_SCRAPER_GMAIL_USERNAME=
   CNBC_SCRAPER_GMAIL_PASSWORD=
   CNBC_SCRAPER_ARTICLE_DATA_SHA=
   CNBC_SCRAPER_LATEST_ASSETS_SHA=
   CNBC_SCRAPER_LATEST_ARTICLE_SHA=
   CNBC_SCRAPER_SESSION_TOKEN=

   # Hedgeye scraper settings
   HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN=
   HEDGEYE_SCRAPER_TELEGRAM_GRP=

   # Motley fool scraper settings
   FOOL_SCRAPER_TELEGRAM_BOT_TOKEN=
   FOOL_SCRAPER_TELEGRAM_GRP=
   FOOL_USERNAME=
   FOOL_PASSWORD=
   FOOL_API_KEY=
   FOOL_GRAPHQL_HASH=

   # Citron Research scraper settings
   CITRON_TELEGRAM_BOT_TOKEN=
   CITRON_TELEGRAM_GRP=

   # Kerrisdale Capital scraper settings
   KERRISDALE_TELEGRAM_BOT_TOKEN=
   KERRISDALE_TELEGRAM_GRP=

   # Hindenburg Research scraper settings
   HINDENBURG_TELEGRAM_BOT_TOKEN=
   HINDENBURG_TELEGRAM_GRP=

   # Bearcave ticker scraper settings
   BEARCAVE_TELEGRAM_BOT_TOKEN=
   BEARCAVE_TELEGRAM_GRP=

   # Grizzly Media scraper settings
   GRIZZLY_TELEGRAM_BOT_TOKEN=
   GRIZZLY_TELEGRAM_GRP=

   # Muddy Waters Media scraper settings
   MUDDY_WATERS_TELEGRAM_BOT_TOKEN=
   MUDDY_WATERS_TELEGRAM_GRP=

   # Altucher ticker scraper settings
   ALTUCHER_TELEGRAM_BOT_TOKEN=
   ALTUCHER_TELEGRAM_GRP=
   ALTUCHER_USERNAME=
   ALTUCHER_PASSWORD=
   ALTUCHER_COOKIE_TID=
   ALTUCHER_COOKIE_ID=

   # Banyan image scraper settings
   BANYAN_TELEGRAM_BOT_TOKEN=
   BANYAN_TELEGRAM_GRP=
   BANYAN_TRADE_ALERT_TELEGRAM_GRP=

   # Navallier Old scraper settings
   INVESTOR_PLACE_TELEGRAM_BOT_TOKEN=
   INVESTOR_PLACE_TELEGRAM_GRP=
   IPA_LOGIN_COOKIE=

   # Zacks Trading Service settings
   ZACKS_TELEGRAM_BOT_TOKEN=
   ZACKS_TELEGRAM_GRP=
   ZACKS_USERNAME=
   ZACKS_PASSWORD=
   ZACKS_WEBSOCKET_URL=

   # Youtube Channel Monitor settings
   YOUTUBE_TELEGRAM_BOT_TOKEN=
   YOUTUBE_TELEGRAM_GRP=
   YOUTUBE_PLAYLIST_ID=

   # Beta Ville Scraper settings
   BETA_VILLE_TELEGRAM_BOT_TOKEN=
   BETA_VILLE_TELEGRAM_GRP=

   # Blue Orca Scraper settings
   BLUEORCA_TELEGRAM_BOT_TOKEN=
   BLUEORCA_TELEGRAM_GRP=

   # Jehoshaphat Reaserch Scraper settings
   JEHOSHAPHAT_TELEGRAM_BOT_TOKEN=
   JEHOSHAPHAT_TELEGRAM_GRP=

   # Friendly Bear Research Scraper settings
   FRIENDLY_BEAR_TELEGRAM_BOT_TOKEN=
   FRIENDLY_BEAR_TELEGRAM_GRP=

   # Viceroy Research Scraper settings
   VICEROY_TELEGRAM_BOT_TOKEN=
   VICEROY_TELEGRAM_GRP=

   # Iceberg Research Scraper settings
   ICEBERG_TELEGRAM_BOT_TOKEN=
   ICEBERG_TELEGRAM_GRP=

   # Hunter Brook Research Scraper settings
   HUNTER_BROOK_TELEGRAM_BOT_TOKEN=
   HUNTER_BROOK_TELEGRAM_GRP=

   # Mariner Research Scraper settings
   MARINER_TELEGRAM_BOT_TOKEN=
   MARINER_TELEGRAM_GRP=

   # White Diamond Research scraper settings
   WDR_TELEGRAM_BOT_TOKEN=
   WDR_TELEGRAM_GRP=

   # Wolfpack Research scraper settings
   WPR_TELEGRAM_BOT_TOKEN=
   WPR_TELEGRAM_GRP=

   # Miner vini scraper settings
   MINERVINI_TELEGRAM_BOT_TOKEN=
   MINERVINI_TELEGRAM_GRP=

   # IBD Swing Trader scraper settings
   IBD_TELEGRAM_BOT_TOKEN=
   IBD_TELEGRAM_GRP=

   # Substack Citrini ticker scraper settings
   CITRINI_TELEGRAM_BOT_TOKEN=
   CITRINI_TELEGRAM_GRP=

   # Morpheus Research scraper settings
   MORPHEUS_API_KEY=
   MORPHEUS_TELEGRAM_BOT_TOKEN=
   MORPHEUS_TELEGRAM_GRP=

   # Spruce Point Capital scraper settings
   SPRUCEPOINT_TELEGRAM_BOT_TOKEN=
   SPRUCEPOINT_TELEGRAM_GRP=

   # Fuzzy Panda scraper settings
   FUZZYPANDA_TELEGRAM_BOT_TOKEN=
   FUZZYPANDA_TELEGRAM_GRP=

   # J Capital Research settings
   JCAPITAL_TELEGRAM_BOT_TOKEN=
   JCAPITAL_TELEGRAM_GRP=

   # Discord settings
   DISCORD_TELEGRAM_BOT_TOKEN=
   DISCORD_TELEGRAM_GRP=
   DISCORD_EMAIL=
   DISCORD_PASSWORD=

   # Seeking Alpha settings
   SEEKING_ALPHA_TELEGRAM_BOT_TOKEN=
   SEEKING_ALPHA_TELEGRAM_GRP=
   SEEKING_ALPHA_EMAIL=
   SEEKING_ALPHA_PASSWORD=

   # Josh brown settings
   JOSH_BROWN_GMAIL_USERNAME=
   JOSH_BROWN_GMAIL_PASSWORD=
   JOSH_BROWN_TELEGRAM_BOT_TOKEN=
   JOSH_BROWN_TELEGRAM_GRP=
   JOSH_BROWN_SESSION_TOKEN=

   # Twitter settings
   TWITTER_USERNAME=
   TWITTER_PASSWORD=
   TWITTER_TELEGRAM_BOT_TOKEN=
   TWITTER_TELEGRAM_GRP=
   DECK_TWEET_TELEGRAM_GRP=

   # Money and Markets settings
   MONEYANDMARKETS_TELEGRAM_BOT_TOKEN=
   MONEYANDMARKETS_TELEGRAM_GRP=
   MONEYANDMARKETS_USERNAME=
   MONEYANDMARKETS_PASSWORD=

   # Prosperity Research settings
   PROSPERITY_TELEGRAM_BOT_TOKEN=
   PROSPERITY_TRADE_ALERT_TELEGRAM_GRP=

   # Culper Research scraper settings
   CULPER_TELEGRAM_BOT_TOKEN=
   CULPER_TELEGRAM_GRP=

   # Scorpion Capital scraper settings
   SCORPION_TELEGRAM_BOT_TOKEN=
   SCORPION_TELEGRAM_GRP=

   # Godel settings
   GODEL_TELEGRAM_BOT_TOKEN=
   GODEL_TELEGRAM_GRP=

   # Ningi research settings
   NINGI_TELEGRAM_BOT_TOKEN=
   NINGI_TELEGRAM_GRP=

   # Snow Cap research settings
   SNOWCAP_TELEGRAM_BOT_TOKEN=
   SNOWCAP_TELEGRAM_GRP=

   # Gothem City Research settings
   GOTHAM_CITY_TELEGRAM_BOT_TOKEN=
   GOTHAM_CITY_TELEGRAM_GRP=
   ```

**Note:** Fill in the values for each variable as needed.

## Step 4: Set Up Credentials

1. **Create a folder named `cred/` in the root directory.**
2. **Place the following files in `cred/`:**

   - `gmail_credentials.json`  # Credentials for Gmail API (download this from Google Cloud API)
   - `godel_token.json` # Jwt token for the godel website ( prolly need to replace every month )
   - `fool_session.json` # Contains Session data from where you manually logged in with out a headless mode ( Needs to be updated every Monday )
   - `hedgeye_credentials.json` # Contains login accounts for hedgeye
   - `ibd_creds.json` # Contains cookies and session creds for ibd leaderboard scripts ( Needs to be updated every Year )
   - `proxies.json` # Contains all the proxies for all scripts
   - `substack_cookies.json` # Contains cookies data for substack ( bearcave top domain & and needs to be updated every year )
   - `youtube_api_keys.json` # Contains youtube api keys
   - `zacks_credentials.json` # Zacks account infos

3. **Place the following files in `data/`:**

   - `minervini_access_token.json`  # Contains access csrf and session token for minervini ( Needs to be updated every Monday )
   - `wolfpack_access_token.json`  # Contains svsession and auth token for wolfpack ( Needs to be updated every year )
   - `zacks_tickers.json`  # Contains the 3000 tickers we use on the zacks_widget scraper
   - `hedeye_cookies/` # NOTE: This a folder and contains cookies for all hedgeye accounts

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

   Replace `VERSION` with the version of Chrome you installed (e.g., `133.0.6943.141`):

   ```bash
   cd /tmp/
   sudo wget https://storage.googleapis.com/chrome-for-testing-public/VERSION/linux64/chromedriver-linux64.zip
   sudo unzip chromedriver-linux64.zip
   sudo mv chromedriver-linux64/chromedriver /usr/bin/chromedriver
   cd -
   ```

   ### Verify Installation

   Check that ChromeDriver is installed correctly by running:

   ```bash
   chromedriver --version
   ```

## Step 6: Install and Configure AWS CLI (For Oxford Images/S3 Buckets)

1. **Install AWS CLI:**
   ```bash
   cd /tmp/
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
   unzip awscliv2.zip
   sudo ./aws/install
   cd -
   ```

2. **Configure AWS Credentials:**
   ```bash
   aws configure
   ```
   You'll be prompted to enter:
   - AWS Access Key ID
   - AWS Secret Access Key
   - Default region (e.g., us-east-1)
   - Default output format (json)

3. **Verify Installation:**
   ```bash
   aws --version
   aws s3 ls
   ```

## Step 7: Run the Scripts

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
- **Altucher:** `altucher_scraper.py`
- **OxfordClub:** `oxfordclub_scraper.py`, `oxfordclub_post_id.py`, `oxfordclub_search_id.py`, `oxford_favorites.py` 
- **Oxford Communique:** `oxford_communique_scraper.py`
- **Oxford Microcap Trader:** `oxford_microcap_trader.py`
- **Oxford Income Letter:** `oxford_income_let_scraper.py`
- **StockNews:** `stocknews_scraper.py`, `stocknews_html_scraper.py`, `stocknews_author_html_scraper.py`, `stocknews_secret_html_scraper.py`
- **CNBC:** `cnbc_scraper.py`, `cnbc_news.py`, `josh_brown_scraper.py` (*requires [GUI](./gui_setup.md) setup*)
- **Motley Fool:** `motley_accessible_rec.py`, `motley_fool_scraper.py`, `motley_image_scraper.py`, `motley_instrument_scraper.py`, `motley_rec_scraper.py`, `motley_video_scraper.py`
- **Navallier:** `navallier_old_scraper.py`, `navallier_old_csv_scraper.py`,  `navallier_new_scraper.py`
- **Zacks:** `zack_html_ts_scraper.py`, `zack_widget_scraper.py`, `zack_commentary_scraper.py` (*requires [GUI](./gui_setup.md) setup*)
- **Zacks Multi server method:** `zack_comm_client.py`, `zack_comm_server.py` (*requires [GUI](./gui_setup.md) setup for the clients*)
- **Hedgeye:** `hedgeye_scraper.py`, `hedgeye_html_scraper.py`, `hedgeye_new_scraper.py`
- **White Diamond:** `wdr_ticker_scraper.py`, `wdr_article_scraper.py`
- **Wolfpack:** `wolfpack_scraper.py`, `wolfpack_xml_scraper.py`
- **Banyan Article Scraper:** `banyan_article_scraper.py`
- **MinerVini:** `minervini_live_id.py`, `minervini_live_scraper.py`, `minervini_livestream_id.py`, `minervini_post_id.py`, `minervini_post_scraper.py`
- **IBD Swing Trader:** `ibd_api_scraper.py`, `ibd_history_scraper.py`, `ibd_stock_id_scraper.py`
- **Fuzzy Panda:** `fuzzy_panda_scraper.py`
- **Ningi:** `ningi_research.py`
- **Prosperity:** `prosperity_research.py`
- **Scorpian:** `scorpian_research.py`
- **Snowcap:** `snowcap_research.py`
- **IBD Leaderboard:** `ibd_leaderboard.py`
- **Spruce point:** `sprucepoint_api.py`, `sprucepoint_press_api.py`, `sprucepoint_research.py`, `sprucepoint_sitemap.py`
- **Seeking Alpha:** `seeking_alpha_article.py`, `seeking_alpha_picks.py` (*requires [GUI](./gui_setup.md) setup*)
- **Jcapital:** `jcapital_api.py`, `jcapital_company_reports.py`
- **Morpheus Ghost:** `morpheus_ghost_scraper.py`

### 2. **PDF & Image Scrapers**
- **Blue Orca:** `blueorca_scraper.py`, `blue_orca_report.py`, `blue_orca_sitemap.py`
- **Oxford S3 Image:** `oxford_s3_image_scraper.py`
- **Hindenburg:** `hindenburg_scraper.py`
- **Jehoshaphat Research:** `jehoshaphat_scraper.py`, `jehoshaphat_author_feed.py`, `jehoshaphat_media_id.py`, `jehoshaphat_post_id.py`, `jehoshaphat_post_sitemap.py`, `jehoshaphat_research_feed.py` (*requires [GUI](./gui_setup.md) setup*)
- **Friendly Bear:** `friendly_bear_scraper.py`
- **HunterBrook Research:** `hunterbrook_scraper.py`, `hunterbrook_post_scraper.py`
- **Iceberg Research:** `iceberg_scraper.py` (*requires [GUI](./gui_setup.md) setup*)
- **Kerrisdale Capital:** `kerrisdale_scraper.py`, `kerrisdale_investment_feed.py`, `kerrisdale_tag_scraper.py`
- **Mariner Research:** `mariner_scraper.py`
- **Viceroy Research:** `viceroy_scraper.py`
- **Citron Research:** `citron_scraper.py`, `citron_attachment_sitemap.py`
- **Muddy Waters Media:** `mudddy_waters_scraper.py`, `muddy_waters_pdf_scraper.py`, `muddy_waters_research.py` (*requires [GUI](./gui_setup.md) setup*)
- **Grizzly Media:** `grizzly_scraper.py` (*requires [GUI](./gui_setup.md) setup*)
- **Bearcave:** `bearcave_scraper.py`, `bearcave_xml_scraper.py`, `bearcave_html_scraper.py`
- **Substack:** `substack_citrini_scraper.py`, `substack_post_scraper.py`
- **Culper:** `culper_research.py`
- **Beta Ville:** `beta_ville_scrper.py`

### 3. **Email Scrapers**
- **Gmail:** `gmail_scraper.py`, `gmail_scraper_a2.py`

### 4. **Image Scrapers**
- **Banyan Image Scraper:** `banyan_image_scraper.py`

### 5. **Chat scrapers**
- **Discord Channels:** `discord_scraper.py` (*requires [GUI](./gui_setup.md) setup*)
- **Godel Chat:** `godel.py` (*requires [GUI](./gui_setup.md) setup*)

### 6. **Specialized Scrapers**
- **YouTube Channel Monitor:** `youtube_channel_monitor.py`
- **Gpt poll responder:** `telegram_gpt_poll_updater.py`
- **Mobile Sms (Wolfpack & Oxford):** `sms_scraper.py`
- **Twitter:** `twitter_scraper.py` (*requires [GUI](./gui_setup.md) setup*)

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
│   ├── fool_session.json         # will be created upon first login
│   ├── gmail_credentials.json
│   ├── gmail_token_a1.json
│   ├── gmail_token_a2.json
│   ├── hedgeye_credentials.json
│   ├── ibd_creds.json
│   ├── proxies.json
│   ├── substack_cookies.json
│   ├── youtube_api_keys.json
│   └── zacks_credentials.json
├── data/                         # Folder to save scraper data to access later
├── log/                          # Folder for log files
├── utils/                        # Utility functions
│   ├── __init__.py
│   ├── base_logger.py            # logger ghost
│   ├── bearcave_draft_monitor.py # Monitor bearcave drafts separately
│   ├── bypass_cloudflare.py      # Bypasser cloudflare captchas
│   ├── error_notifier.py         # Notify critilca error to telegram
│   ├── gpt_ticker_extractor.py   # Extract ticker from Image or Text
│   ├── logger.py                 # Logger utility
│   ├── oxford_fetch_client.py
│   ├── oxford_fetch_server.py
│   ├── telegram_sender.py        # Telegram sending utility
│   └── ticker_deck_sender.py     # Ticker deck message sending utility
│   ├── time_utils.py             # Time utility functions
│   └── websocket_sender.py       # WebSocket sending utility
├── .env                          # Environment variables
├── .env.example                  # Environment variables
├── .gitignore                    # Git ignore file
├── README.md                     # Project documentation
├── altucher_scraper.py
├── banyan_article_scraper.py
├── banyan_image_scraper.py
├── bearcave_html_scraper.py
├── bearcave_scraper.py
├── bearcave_xml_scraper.py
├── beta_ville_scraper.py
├── blue_orca_report.py
├── blue_orca_sitemap.py
├── blueorca_scraper.py
├── citron_attachment_sitemap.py
├── citron_scraper.py
├── cnbc_news.py
├── cnbc_scraper.py
├── culper_research.py
├── discord_scraper.py
├── friendly_bear_scraper.py
├── fuzzy_panda_scraper.py
├── gmail_scraper.py
├── gmail_scraper_a2.py
├── godel.py
├── grizzly_scraper.py
├── gui_setup.md                        # Setting up gui for ubuntu server
├── hedgeye_html_scraper.py
├── hedgeye_new_scraper.py
├── hedgeye_scraper.py
├── hindenburg_scraper.py
├── hunterbrook_post_scraper.py
├── hunterbrook_scraper.py
├── ibd_api_scraper.py
├── ibd_history_scraper.py
├── ibd_leaderboard.py
├── ibd_stock_id_scraper.py
├── iceberg_scraper.py
├── jcapital_api.py
├── jcapital_company_reports.py
├── jehoshaphat_author_feed.py
├── jehoshaphat_media_id.py
├── jehoshaphat_post_id.py
├── jehoshaphat_post_sitemap.py
├── jehoshaphat_research_feed.py
├── jehoshaphat_scraper.py
├── josh_brown_scraper.py
├── kerrisdale_investment_feed.py
├── kerrisdale_scraper.py
├── kerrisdale_tag_scraper.py
├── mariner_scraper.py
├── minervini_live_id.py
├── minervini_live_scraper.py
├── minervini_livestream_id.py
├── minervini_post_id.py
├── minervini_post_scraper.py
├── morpheus_ghost_scraper.py
├── motley_accessible_rec.py
├── motley_fool_scraper.py
├── motley_image_scraper.py
├── motley_instrument_scraper.py
├── motley_rec_scraper.py
├── motley_video_scraper.py
├── mudddy_waters_scraper.py
├── muddy_waters_pdf_scraper.py
├── muddy_waters_research.py
├── navallier_new_scraper.py
├── navallier_old_csv_scraper.py
├── navallier_old_scraper.py
├── ningi_research.py
├── oxford_communique_scraper.py
├── oxford_favorites.py
├── oxford_income_let_scraper.py
├── oxford_microcap_trader.py
├── oxford_s3_image_scraper.py
├── oxfordclub_post_id.py
├── oxfordclub_scraper.py
├── oxfordclub_search_id.py
├── oxfordclub_tradesmith.py
├── prosperity_research.py
├── requirements.txt                    # Project dependencies
├── scorpian_research.py
├── seeking_alpha_article.py
├── seeking_alpha_picks.py
├── sms_scraper.py
├── snowcap_research.py
├── sprucepoint_api.py
├── sprucepoint_press_api.py
├── sprucepoint_research.py
├── sprucepoint_sitemap.py
├── stocknews_author_html_scraper.py
├── stocknews_html_scraper.py
├── stocknews_scraper.py
├── stocknews_secret_html_scraper.py
├── substack_citrini_scraper.py
├── substack_post_scraper.py
├── telegram_gpt_poll_updater.py
├── twitter_scraper.py
├── viceroy_scraper.py
├── wdr_article_scraper.py
├── wdr_ticker_scraper.py
├── wolfpack_scraper.py
├── wolfpack_xml_scraper.py
├── youtube_channel_monitor.py
├── zack_comm_client.py
├── zack_comm_server.py
├── zack_commentary_scraper.py
├── zack_html_ts_scraper.py
└── zack_widget_scraper.py
```

### Important Notes

- Ensure to create and go into the virtual environment before installing dependencies.
- Keep sensitive information secure and avoid sharing your `.env` and `cred/` files.
- Log files will be generated in the `log/` folder with a `.log` extension.
