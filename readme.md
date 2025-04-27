# Telegram bot to watch TON coin price

# Coin alert bot

This readme has been created also from chatgpt comment. Only one change:

1. Change `botid` in `config.json`
2. Retrieve on coingecko demo key and put into `geckoapi` in `config.json`

## ChatGPT comments

### Instructions

Install the required libraries:

```bash
pip install requests python-telegram-bot==13.7 apscheduler pytz
```

Replace 'BOT-ID' with your Telegram bot's API token.
Run the script:

```sh
python src/main.py
```

Interact with the bot:
    Start a chat with your bot on Telegram.
    Type /start to see the buttons.
    Press "Set Above Price Alert" or "Set Below Price Alert" and send the desired price.
    The bot will notify you if the TON price goes above or below the set thresholds and save these settings in a JSON config file.

This setup will store user-specific price thresholds in a JSON file and notify users when the TON price crosses these thresholds.

## Filtering output

I prefer to use script with `nohup`, so after left a `nohup.out` file, which is contains lifetime prices of selected coins.
To convert log into json, might be used next command:

```bash
cat nohup.out | python3 ./src/log_parser.py > log.json
```

Which saves log.json with application lifetime coin prices. 

```json
[
  {
    "ts": "2025-03-20T14:29:02.937834",
    "price": {
      "binancecoin": {
        "usd": 628.52
      },
      "bitcoin": {
        "usd": 86423
      },
      "bitget-token": {
        "usd": 4.73
      }
    }
  },
...
]
```

To filter and achieve coin prices per day could be used a next trick:

```bash
jq '[.[] | select(.ts | startswith("2025-04-15")) | {ts, bitcoin: .price.bitcoin.usd}]' log.json > 2025-04-15.json
```
