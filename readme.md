# Telegram bot to watch TON coin price

** This bot was created by chatgpt, it may not work **

This readme has been created also from chatgpt comment. Only one change:

1. Change `BOT-ID` in `config.json`

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
