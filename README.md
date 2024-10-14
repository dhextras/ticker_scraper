# TickerScraper Setup Guide

Follow these steps to set up and run the TickerScraper project.

## Step 1: Install Dependencies

Install the required Python packages using:

```bash
pip install -r requirements.txt
```

## Step 2: Create a `.env` File

Create a file named `.env` in the root directory and add the following details:

```plaintext
OXFURDCLUB_TELEGRAM_BOT_TOKEN=
OXFURDCLUB_TELEGRAM_GRP=
OXFURDCLUB_USERNAME=
OXFURDCLUB_PASSWORD=

STOCKNEWS_TELEGRAM_BOT_TOKEN=
STOCKNEWS_TELEGRAM_GRP=
```

Fill in the values for each variable as needed.

## Step 3: Run the Scripts

You can run each of the scripts based on your needs:

- To run the **OxfurdClub ticker scraper**, use:
  ```bash
  python oxfurdclub_ticker_scraper.py
  ```

- To run the **StockNews ticker scraper**, use:
  ```bash
  python stocknews_ticker_scraper.py
  ```

Make sure your `.env` file is properly set up before running these scripts.
