import json
import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pytz import utc

CONFIG_FILE = 'config.json'

# Load the config file
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    return {}

# Save the config file
def save_config(config):
    with open(CONFIG_FILE, 'w') as file:
        json.dump(config, file, indent=4)

# Function to get TON price
def get_ton_price():
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': 'the-open-network',
        'vs_currencies': 'usd'
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()  # Raise an exception for HTTP errors
        data = response.json()
        ton_price = data['the-open-network']['usd']
        return ton_price
    except requests.exceptions.HTTPError as http_err:
        print(f'HTTP error occurred: {http_err}')
        return None
    except Exception as err:
        print(f'Other error occurred: {err}')
        return None

# Start command handler
def start(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    keyboard = [
        [InlineKeyboardButton("Get TON Price", callback_data='get_price')],
        [InlineKeyboardButton("Set Above Price Alert", callback_data='set_above')],
        [InlineKeyboardButton("Set Below Price Alert", callback_data='set_below')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Press a button to set price alerts or get the current TON price.', reply_markup=reply_markup)

# Button callback handler
def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    chat_id = str(query.message.chat_id)

    if query.data == 'get_price':
        ton_price = get_ton_price()
        if ton_price is not None:
            query.edit_message_text(text=f'TON price: ${ton_price:.2f} USD')
        else:
            query.edit_message_text(text='Failed to retrieve the TON price.')
    elif query.data == 'set_above':
        query.message.reply_text('Please send the price above which you want to get notified:')
        context.user_data['setting_above'] = chat_id
    elif query.data == 'set_below':
        query.message.reply_text('Please send the price below which you want to get notified:')
        context.user_data['setting_below'] = chat_id

# Set price alert handler
def set_price_alert(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    config = load_config()

    try:
        price = float(update.message.text)
        if 'setting_above' in context.user_data:
            if chat_id not in config:
                config[chat_id] = {}
            config[chat_id]['above'] = price
            update.message.reply_text(f'You will be notified if the TON price goes above ${price:.2f}')
            del context.user_data['setting_above']
        elif 'setting_below' in context.user_data:
            if chat_id not in config:
                config[chat_id] = {}
            config[chat_id]['below'] = price
            update.message.reply_text(f'You will be notified if the TON price goes below ${price:.2f}')
            del context.user_data['setting_below']

        save_config(config)
    except ValueError:
        update.message.reply_text('Invalid price. Please enter a valid number.')

# Check price function
def check_price(context: CallbackContext):
    config = load_config()
    ton_price = get_ton_price()
    if ton_price is None:
        return

    print('Current price: ', ton_price)

    for chat_id, thresholds in config.items():
        if 'above' in thresholds and ton_price > thresholds['above']:
            context.bot.send_message(chat_id=chat_id, text=f'TON price is above ${thresholds["above"]:.2f}: Current price is ${ton_price:.2f}')
            del config[chat_id]['above']

        if 'below' in thresholds and ton_price < thresholds['below']:
            context.bot.send_message(chat_id=chat_id, text=f'TON price is below ${thresholds["below"]:.2f}: Current price is ${ton_price:.2f}')
            del config[chat_id]['below']

        save_config(config)

def main():
    config = load_config()
    # Replace 'YOUR_TOKEN_HERE' with your bot's token
    updater = Updater(config["botid"], use_context=True)

    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(button))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, set_price_alert))

    # Set up the price checking job
    scheduler = BackgroundScheduler(timezone=utc)
    trigger = IntervalTrigger(minutes=1, timezone=utc)
    scheduler.add_job(check_price, trigger, args=[dispatcher])
    scheduler.start()

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
