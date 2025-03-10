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

# Function to get coin price
def get_price(coin_id):
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': coin_id,
        'vs_currencies': 'usd'
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        price = data[coin_id]['usd']
        return price
    except requests.exceptions.HTTPError as http_err:
        print(f'HTTP error occurred: {http_err}')
        return None
    except Exception as err:
        print(f'Other error occurred: {err}')
        return None

# Start command handler
def start(update: Update, context: CallbackContext) -> None:
    keyboard = [
        [InlineKeyboardButton("TON", callback_data='select_coin_ton')],
        [InlineKeyboardButton("Bitcoin", callback_data='select_coin_bitcoin')],
        [InlineKeyboardButton("SUI", callback_data='select_coin_sui')],
        [InlineKeyboardButton("XRP", callback_data='select_coin_xrp')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Please select a coin:', reply_markup=reply_markup)

# Button callback handler
def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    chat_id = str(query.message.chat_id)

    # Handle coin selection
    if query.data == 'select_coin_ton':
        context.user_data['coin'] = 'the-open-network'
        context.user_data['coin_name'] = 'TON'
    elif query.data == 'select_coin_bitcoin':
        context.user_data['coin'] = 'bitcoin'
        context.user_data['coin_name'] = 'Bitcoin'
    elif query.data == 'select_coin_sui':
        context.user_data['coin'] = 'sui'
        context.user_data['coin_name'] = 'SUI'
    elif query.data == 'select_coin_xrp':
        context.user_data['coin'] = 'binance-peg-xrp'
        context.user_data['coin_name'] = 'XRP'

    if query.data in ['select_coin_ton', 'select_coin_bitcoin','select_coin_sui','select_coin_xrp']:
        coin_name = context.user_data['coin_name']
        keyboard = [
            [InlineKeyboardButton(f"Get {coin_name} Price", callback_data='get_price')],
            [InlineKeyboardButton(f"Set Above Price Alert for {coin_name}", callback_data='set_above')],
            [InlineKeyboardButton(f"Set Below Price Alert for {coin_name}", callback_data='set_below')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text(text=f'Selected {coin_name}. Choose an option:', reply_markup=reply_markup)
    elif query.data == 'get_price':
        coin_id = context.user_data.get('coin')
        coin_name = context.user_data.get('coin_name')
        if not coin_id:
            query.edit_message_text(text='Please select a coin first by sending /start')
            return
        price = get_price(coin_id)
        if price is not None:
            query.edit_message_text(text=f'{coin_name} price: ${price:.2f} USD')
        else:
            query.edit_message_text(text=f'Failed to retrieve the {coin_name} price.')
    elif query.data == 'set_above':
        coin_name = context.user_data.get('coin_name')
        query.message.reply_text(f'Please send the price above which you want to get notified for {coin_name}:')
        context.user_data['setting_above'] = True
    elif query.data == 'set_below':
        coin_name = context.user_data.get('coin_name')
        query.message.reply_text(f'Please send the price below which you want to get notified for {coin_name}:')
        context.user_data['setting_below'] = True

# Set price alert handler
def set_price_alert(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    config = load_config()
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id:
        update.message.reply_text('Please select a coin first by sending /start')
        return

    try:
        price = float(update.message.text)
        if 'setting_above' in context.user_data:
            config.setdefault(chat_id, {}).setdefault(coin_id, {})
            config[chat_id][coin_id]['above'] = price
            update.message.reply_text(f'You will be notified if the {coin_name} price goes above ${price:.2f}')
            del context.user_data['setting_above']
        elif 'setting_below' in context.user_data:
            config.setdefault(chat_id, {}).setdefault(coin_id, {})
            config[chat_id][coin_id]['below'] = price
            update.message.reply_text(f'You will be notified if the {coin_name} price goes below ${price:.2f}')
            del context.user_data['setting_below']
        
        save_config(config)
    except ValueError:
        update.message.reply_text('Invalid price. Please enter a valid number.')

def check_price(context: CallbackContext):
    config = load_config()
    original_config = config.copy()  # Copy to preserve 'botid'

    # Exclude 'botid' when processing user IDs
    user_configs = {k: v for k, v in config.items() if k != 'botid'}

    coins_to_check = set()
    for thresholds in user_configs.values():
        coins_to_check.update(thresholds.keys())

    # Fetch current prices for all required coins
    coin_prices = {}
    for coin_id in coins_to_check:
        price = get_price(coin_id)
        if price is not None:
            coin_prices[coin_id] = price

    for chat_id, coins in user_configs.items():
        for coin_id, thresholds in coins.items():
            if coin_id not in coin_prices:
                continue
            price = coin_prices[coin_id]
            if coin_id == 'the-open-network':
                coin_name = 'TON'
            elif coin_id == 'bitcoin':
                coin_name = 'Bitcoin'
            elif coin_id == 'sui':
                coin_name = 'SUI'
            elif coin_id == 'binance-peg-xrp':
                coin_name = 'XRP'
            else:
                coin_name = coin_id.capitalize()

            messages = []
            if 'above' in thresholds and price > thresholds['above']:
                messages.append(f'{coin_name} price is above ${thresholds["above"]:.2f}: Current price is ${price:.2f}')
                del thresholds['above']
            if 'below' in thresholds and price < thresholds['below']:
                messages.append(f'{coin_name} price is below ${thresholds["below"]:.2f}: Current price is ${price:.2f}')
                del thresholds['below']

            # Send messages
            for msg in messages:
                context.bot.send_message(chat_id=chat_id, text=msg)

        # Clean up empty coins for the user
        coins_to_delete = [coin_id for coin_id, thresholds in coins.items() if not thresholds]
        for coin_id in coins_to_delete:
            del coins[coin_id]

    # Clean up users with no coins left
    chat_ids_to_delete = [chat_id for chat_id, coins in user_configs.items() if not coins]
    for chat_id in chat_ids_to_delete:
        del user_configs[chat_id]

    # Update the original config with the modified user configs
    for chat_id in user_configs:
        original_config[chat_id] = user_configs[chat_id]
    for chat_id in chat_ids_to_delete:
        if chat_id in original_config:
            del original_config[chat_id]

    # Save the updated config, including 'botid'
    save_config(original_config)



# Main function
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
