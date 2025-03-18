import datetime
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
    config = load_config()
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': coin_id,
        'vs_currencies': 'usd'
    }

    headers = {
        "accept": "application/json"
    }
    
    if "geckoapi" in config.keys():
        headers["x-cg-api-key"] = config["geckoapi"]

    try:
        response = requests.get(url, params=params, headers=headers)
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

def buy(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id:
        update.message.reply_text('❌ Please select a coin first with /start')
        return

    args = context.args
    if len(args) != 1:
        update.message.reply_text('Usage: /buy <amount_usd>\nExample: /buy 100')
        return

    try:
        amount_usd = float(context.args[0])
    except ValueError:
        update.message.reply_text('Invalid amount. Please enter a numeric value.\nExample: /buy 100')
        return

    try:
        current_price = get_price(coin_id)

        if current_price is None:
            update.message.reply_text(f'Failed to fetch current {coin_name} price.')
            return

        quantity = amount_usd / current_price
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        config = load_config()
        purchase_entry = {
            "coin": coin_name,
            "coin_id": coin_id,
            "amount_usd": amount_usd,
            "price_per_coin": current_price,
            "quantity": quantity,
            "timestamp": timestamp
        }

        config.setdefault('purchases', {}).setdefault(chat_id, []).append(purchase_entry)
        save_config(config)

        update.message.reply_text(
            f"✅ Logged Buy:\n"
            f"Coin: {coin_name}\n"
            f"Amount: ${amount_usd:.2f}\n"
            f"Price: ${current_price:.2f}\n"
            f"Quantity: {quantity:.4f}\n"
            f"Date: {timestamp} UTC"
        )

    except ValueError:
        update.message.reply_text('Usage: /buy <amount_in_usd>\nExample: /buy 100')


def sell(update: Update, context: CallbackContext) -> None:
    if update is None or update.message is None:
        print('Update is none')
        return

    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')  # explicitly set coin_name here

    if not coin_id or not coin_name:
        update.message.reply_text('❌ Select a coin first using /start.')
        return
    
    if len(context.args) != 1:
        update.message.reply_text('Usage: /sell <quantity>\nExample: /sell 10')
        return
    
    

    try:
        config = load_config()
        user_purchases = config.get('purchases', {}).get(chat_id, [])
        coin_purchases = [p for p in user_purchases if p['coin_id'] == coin_id]
        total_available = sum(p['quantity'] for p in coin_purchases)

        try:
            if str(context.args[0]).lower() == 'max':
                sell_quantity = total_available
            else:
                sell_quantity = float(context.args[0])
        except ValueError:
            update.message.reply_text('❌ Invalid quantity. Enter a numeric value.\nExample: /sell 10')
            return
        
        total_available = sum(p['quantity'] for p in coin_purchases)
        if sell_quantity > total_available:
            update.message.reply_text(f"❌ You don't have enough {coin_name}. You have {total_available:.4f}, but tried selling {sell_quantity:.4f}.")
            return

        current_price = get_price(coin_id)
        if current_price is None:
            update.message.reply_text('❌ Failed to retrieve the current price. Try again later.')
            return

        remaining_to_sell = sell_quantity

        # FIFO logic
        new_purchases = []
        for p in coin_purchases:
            if remaining_to_sell >= p['quantity']:
                remaining_to_sell -= p['quantity']
                continue  # remove fully sold entry
            else:
                p['quantity'] -= remaining_to_sell
                remaining_to_sell = 0
                new_purchases.append(p)

            if remaining_to_sell == 0:
                new_purchases_after_current = coin_purchases[coin_purchases.index(p)+1:]
                new_purchases_list = new_purchases_after_current
                new_purchases_list.extend(new_purchases)
                break

        # Update and save the configuration
        config['purchases'][chat_id] = [p for p in user_purchases if p['coin_id'] != coin_id] + new_purchases
        save_config(config)

        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        update.message.reply_text(
            f"🔴 Sold {sell_quantity:.4f} {coin_name}\n"
            f"At price: ${current_price:.2f} per coin\n"
            f"Total: ${sell_quantity * current_price:.2f}\n"
            f"Date: {timestamp} UTC"
        )

    except (ValueError, IndexError):
        update.message.reply_text('Invalid input. Usage: /sell <quantity>\nExample: /sell 10')



def history(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    config = load_config()
    purchases = config.get('purchases', {}).get(chat_id, [])
    
    if not purchases:
        update.message.reply_text("🗒 Your purchase diary is empty.")
        return
    
    history_text = "📗 Your Purchase Diary:\n\n"
    holdings_summary = {}

    for entry in purchases:
        coin_name = entry['coin']  # explicitly use coin_name
        quantity = entry['quantity']
        history_text += (
            f"Coin: {coin_name}\n"
            f"Bought for: ${entry['amount_usd']:.2f}\n"
            f"Price per coin: ${entry['price_per_coin']:.2f}\n"
            f"Quantity: {quantity:.4f}\n"
            f"Date: {entry['timestamp']} UTC\n\n"
        )

        # Summarize holdings
        holdings_summary[coin_name] = holdings_summary.get(coin_name, 0) + quantity

    holdings_text = "📊 **Current Holdings:**\n"
    for coin, qty in holdings_summary.items():
        holdings_text += f"{coin}: {qty:.4f}\n"

    update.message.reply_text(history_text + holdings_text)



# Main function
def main():
    config = load_config()
    updater = Updater(config["botid"], use_context=True)

    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(CallbackQueryHandler(button))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, set_price_alert))
    dispatcher.add_handler(CommandHandler('buy', buy))
    dispatcher.add_handler(CommandHandler('sell', sell))
    dispatcher.add_handler(CommandHandler('history', history))

    scheduler = BackgroundScheduler(timezone=utc)
    trigger = IntervalTrigger(minutes=1, timezone=utc)
    scheduler.add_job(check_price, trigger, args=[dispatcher])
    scheduler.start()

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
