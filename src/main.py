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
COIN_PRICE_CACHE = {}

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

def update_all_prices():
    global COIN_PRICE_CACHE
    config = load_config()
    coins_available = config.get("coins_available", {})
    
    if not coins_available:
        print("No coins to update.")
        return
    
    coin_ids = ','.join(coins_available.keys())
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': coin_ids,
        'vs_currencies': 'usd'
    }

    headers = {"accept": "application/json"}
    if "geckoapi" in config:
        headers["x-cg-api-key"] = config["geckoapi"]

    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        COIN_PRICE_CACHE = response.json()
        print(f"Prices updated at {datetime.datetime.utcnow().isoformat()} UTC -> {COIN_PRICE_CACHE}")
    except Exception as e:
        print(f"Error fetching coin prices: {e}")

def get_price(coin_id):
    global COIN_PRICE_CACHE
    coin_info = COIN_PRICE_CACHE.get(coin_id)
    if coin_info:
        return coin_info['usd']
    else:
        print(f"No cached price for {coin_id}.")
        return None

# Start command handler
def start(update: Update, context: CallbackContext) -> None:
    config = load_config()
    coins_available = config.get("coins_available", {})

    if not coins_available:
        update.message.reply_text("⚠️ No coins available. Add coins using /addcoin.")
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'select_coin_{coin_id}')]
        for coin_id, name in coins_available.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('Please select a coin:', reply_markup=reply_markup)

# Button callback handler
def button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    config = load_config()
    coins_available = config.get("coins_available", {})

    chat_id = str(query.message.chat_id)

    # Dynamic coin selection handling
    if query.data.startswith('select_coin_'):
        coin_id = query.data.replace('select_coin_', '')
        coin_name = coins_available.get(coin_id)

        if not coin_name:
            query.edit_message_text("⚠️ Selected coin is no longer available.")
            return

        context.user_data['coin'] = coin_id
        context.user_data['coin_name'] = coin_name

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

# Verify and get coin info from CoinGecko
def verify_coin(symbol_or_name):
    config = load_config()
    url = 'https://api.coingecko.com/api/v3/coins/list'
    try:
        headers = {
            "accept": "application/json"
        }

        if "geckoapi" in config.keys():
            headers["x-cg-api-key"] = config["geckoapi"]

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        symbol_or_name_lower = symbol_or_name.lower()
        for coin in data:
            if (coin['id'].lower() == symbol_or_name_lower or
                coin['symbol'].lower() == symbol_or_name_lower or
                coin['name'].lower() == symbol_or_name_lower):
                return coin['id'], coin['name']
        return None, None
    except Exception as e:
        print(f"Error fetching coin list: {e}")
        return None, None

def reset_notification_flags(chat_id, coin_id):
    config = load_config()
    purchases = config.get('purchases', {}).get(chat_id, {}).get(coin_id, [])
    for purchase in purchases:
        purchase["notified"] = False
    save_config(config)

def check_price(context: CallbackContext):
    global COIN_PRICE_CACHE
    config = load_config()
    coins_available = config.get("coins_available", {})
    user_configs = {k: v for k, v in config.items() if k not in ['botid', 'coins_available', 'purchases']}
    user_purchases = config.get('purchases', {})

    if not COIN_PRICE_CACHE:
        print("⚠️ Price cache empty, skipping alert check.")
        return

    # Existing upper/lower price alerts logic unchanged
    for chat_id, coins in user_configs.items():
        for coin_id, thresholds in coins.items():
            coin_info = COIN_PRICE_CACHE.get(coin_id)
            if not coin_info:
                continue

            price = coin_info['usd']
            coin_name = coins_available.get(coin_id, coin_id.capitalize())

            messages = []
            if 'above' in thresholds and price > thresholds['above']:
                messages.append(f'{coin_name} price is above ${thresholds["above"]:.2f}: Current price is ${price:.2f}')
                del thresholds['above']
            if 'below' in thresholds and price < thresholds['below']:
                messages.append(f'{coin_name} price is below ${thresholds["below"]:.2f}: Current price is ${price:.2f}')
                del thresholds['below']

            for msg in messages:
                context.bot.send_message(chat_id=chat_id, text=msg)

        coins_to_delete = [coin_id for coin_id, thresholds in coins.items() if not thresholds]
        for coin_id in coins_to_delete:
            del coins[coin_id]

    chat_ids_to_delete = [chat_id for chat_id, coins in user_configs.items() if not coins]
    for chat_id in chat_ids_to_delete:
        del user_configs[chat_id]

    config.update(user_configs)

    # === New Individual Purchase Profit/Loss Notifications with notified flags ===
    for chat_id, coins in user_purchases.items():
        for coin_id, purchases in coins.items():
            coin_info = COIN_PRICE_CACHE.get(coin_id)
            if not coin_info:
                continue  # skip if price unavailable

            current_price = coin_info['usd']
            coin_name = coins_available.get(coin_id, coin_id.capitalize())

            for purchase in purchases:
                invested_amount = purchase['amount_usd']
                quantity = purchase['quantity']
                purchase_price = purchase['price_per_coin']
                current_value = current_price * quantity
                profit_loss_percent = ((current_value / invested_amount) - 1) * 100

                # Notify only if threshold crossed ±5% and not notified yet
                if (profit_loss_percent >= 5 or profit_loss_percent <= -5) and not purchase.get("notified", False):
                    emoji = "📈" if profit_loss_percent >= 5 else "📉"
                    message = (
                        f"{emoji} **{coin_name} Purchase Alert:**\n"
                        f"• Bought on: {purchase['timestamp']}\n"
                        f"• Bought at: ${purchase_price:.2f}\n"
                        f"• Current price: ${current_price:.2f}\n"
                        f"• Profit/Loss: {profit_loss_percent:.2f}%"
                    )

                    context.bot.send_message(chat_id=chat_id, text=message)
                    purchase["notified"] = True  # Mark as notified

                # Reset notification if back within ±5% range
                elif -5 < profit_loss_percent < 5 and purchase.get("notified", False):
                    purchase["notified"] = False  # Reset flag

    save_config(config)


def buy(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id or not coin_name:
        update.message.reply_text('❌ Please select a coin first with /start.')
        return

    if len(context.args) != 1:
        update.message.reply_text('Usage: /buy <amount_usd>\nExample: /buy 100')
        return

    try:
        amount_usd = float(context.args[0])
    except ValueError:
        update.message.reply_text('Invalid amount. Please enter a numeric value.\nExample: /buy 100')
        return

    current_price = get_price(coin_id)
    if current_price is None:
        update.message.reply_text(f'❌ Failed to fetch current {coin_name} price.')
        return

    quantity = amount_usd / current_price
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    config = load_config()
    coin_purchases = config.setdefault('purchases', {}).setdefault(chat_id, {}).setdefault(coin_id, [])

    purchase_entry = {
        "amount_usd": amount_usd,
        "price_per_coin": current_price,
        "quantity": quantity,
        "timestamp": timestamp
    }

    coin_purchases.append(purchase_entry)
    save_config(config)
    reset_notification_flags(chat_id, coin_id)
    update.message.reply_markdown(
        f"✅**Logged Buy**:\n"
        f"Coin: {coin_name}\n"
        f"Invested: ${amount_usd:.2f}\n"
        f"Price per Coin: ${current_price:.2f}\n"
        f"Quantity Bought: {quantity:.4f}\n"
        f"Date: {timestamp}"
    )

def sell(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id or not coin_name:
        update.message.reply_text('❌ Please select a coin first with /start.')
        return

    if len(context.args) != 1:
        update.message.reply_text('Usage: /sell <quantity|max>\nExample: /sell 0.5 or /sell max')
        return

    config = load_config()
    coin_purchases = config.get('purchases', {}).get(chat_id, {}).get(coin_id, [])

    if not coin_purchases:
        update.message.reply_text(f"⚠️ You don't have any purchases recorded for {coin_name}.")
        return

    total_available = sum(p['quantity'] for p in coin_purchases)

    if context.args[0].lower() == 'max':
        sell_quantity = total_available
        if sell_quantity == 0:
            update.message.reply_text(f"⚠️ You have no {coin_name} to sell.")
            return
    else:
        try:
            sell_quantity = float(context.args[0])
        except ValueError:
            update.message.reply_text('❌ Invalid quantity. Enter a numeric value.\nExample: /sell 0.5')
            return

    if sell_quantity > total_available:
        update.message.reply_text(f"❌ You don't have enough {coin_name}. You have {total_available:.4f}, but tried selling {sell_quantity:.4f}.")
        return

    current_price = get_price(coin_id)
    if current_price is None:
        update.message.reply_text('❌ Failed to retrieve the current price. Try again later.')
        return

    remaining_to_sell = sell_quantity
    realized_amount_usd = 0.0

    # LIFO selling logic: start from most recent purchase
    updated_purchases = coin_purchases.copy()

    for purchase in reversed(coin_purchases):
        if remaining_to_sell == 0:
            break  # Done selling required amount

        if purchase['quantity'] <= remaining_to_sell:
            # Fully sell this purchase entry
            realized_amount_usd += purchase['quantity'] * current_price
            remaining_to_sell -= purchase['quantity']
            updated_purchases.pop()  # remove last purchase
        else:
            # Partially sell this entry
            realized_amount_usd += remaining_to_sell * current_price
            purchase['quantity'] -= remaining_to_sell
            purchase['amount_usd'] = purchase['quantity'] * purchase['price_per_coin']
            remaining_to_sell = 0

    # Update purchases in config
    if updated_purchases:
        config['purchases'][chat_id][coin_id] = updated_purchases
    else:
        del config['purchases'][chat_id][coin_id]  # remove coin entry if no purchases left

    save_config(config)
    reset_notification_flags(chat_id, coin_id)
    timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    update.message.reply_markdown(
        f"🔴 **Sold {sell_quantity:.4f} {coin_name} (LIFO)**\n"
        f"Price: ${current_price:.2f} per coin\n"
        f"Total Received: ${realized_amount_usd:.2f}\n"
        f"Date: {timestamp}"
    )


def history(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    config = load_config()
    user_purchases = config.get('purchases', {}).get(chat_id, {})

    if not user_purchases:
        update.message.reply_text("🗒 Your purchase diary is empty.")
        return

    history_text = "📗 **Your Purchase Diary:**\n\n"
    overall_summary = "📊 **Current Holdings Summary:**\n"
    total_invested_overall = 0
    total_current_value_overall = 0

    for coin_id, purchases in user_purchases.items():
        coin_name = config.get('coins_available', {}).get(coin_id, coin_id.capitalize())
        current_price = get_price(coin_id)

        if current_price is None:
            history_text += f"⚠️ Failed to retrieve current price for {coin_name}.\n\n"
            continue

        coin_total_quantity = sum(p['quantity'] for p in purchases)
        coin_total_invested = sum(p['amount_usd'] for p in purchases)
        coin_current_value = coin_total_quantity * current_price
        coin_profit_loss_percent_overall = ((coin_current_value / coin_total_invested) - 1) * 100

        total_invested_overall += coin_total_invested
        total_current_value_overall += coin_current_value

        history_text += f"🪙 **{coin_name}** - Current: ${current_price:.2f}\n"
        for idx, purchase in enumerate(reversed(purchases), start=1):
            purchase_invested = purchase['amount_usd']
            purchase_quantity = purchase['quantity']
            purchase_price = purchase['price_per_coin']
            purchase_current_value = purchase_quantity * current_price
            profit_loss_percent = ((purchase_current_value / purchase_invested) - 1) * 100

            emoji = "📈" if profit_loss_percent >= 0 else "📉"
            history_text += (
                f"{idx}. Bought on: {purchase['timestamp']}\n"
                f"   • Bought at: ${purchase_price:.2f}\n"
                f"   • Quantity: {purchase_quantity:.4f}\n"
                f"   • Invested: ${purchase_invested:.2f}\n"
                f"   • Current Value: ${purchase_current_value:.2f}\n"
                f"   • {emoji} P/L: {profit_loss_percent:.2f}%\n\n"
            )

        emoji_overall = "📈" if coin_profit_loss_percent_overall >= 0 else "📉"
        overall_summary += (
            f"{coin_name}:\n"
            f"   • Quantity: {coin_total_quantity:.4f}\n"
            f"   • Invested: ${coin_total_invested:.2f}\n"
            f"   • Current Value: ${coin_current_value:.2f}\n"
            f"   • {emoji_overall} Overall P/L: {coin_profit_loss_percent_overall:.2f}%\n\n"
        )

    total_profit_loss_percent_overall = ((total_current_value_overall / total_invested_overall) - 1) * 100
    emoji_total = "📈" if total_profit_loss_percent_overall >= 0 else "📉"

    overall_summary += (
        f"💰 **Total Invested**: ${total_invested_overall:.2f}\n"
        f"💵 **Current Portfolio Value**: ${total_current_value_overall:.2f}\n"
        f"{emoji_total} **Overall Profit/Loss**: {total_profit_loss_percent_overall:.2f}%\n"
    )

    final_message = history_text + overall_summary
    update.message.reply_markdown(final_message)



def addcoin(update: Update, context: CallbackContext):
    if update is None or update.message is None:
        return 

    if len(context.args) != 1:
        update.message.reply_text("Usage: /addcoin <coin_symbol>\nExample: /addcoin BCH")
        return

    coin_input = context.args[0].strip()
    coin_id, coin_name = verify_coin(coin_input)

    if not coin_id:
        update.message.reply_text(f"❌ Coin '{coin_input}' not found on CoinGecko.")
        return

    config = load_config()
    coins_available = config.setdefault("coins_available", {})

    if coin_id in coins_available:
        update.message.reply_text(f"⚠️ {coin_name} is already available.")
        return

    coins_available[coin_id] = coin_name
    save_config(config)

    update.message.reply_text(f"✅ Added {coin_name} ({coin_id}) successfully!")

def removecoin(update: Update, context: CallbackContext):
    if update is None or update.message is None:
        return 

    if len(context.args) != 1:
        update.message.reply_text("Usage: /removecoin <coin_symbol>\nExample: /removecoin BCH")
        return

    coin_input = context.args[0].strip().lower()
    config = load_config()
    coins_available = config.get("coins_available", {})

    coin_to_remove = None
    for coin_id, coin_name in coins_available.items():
        if coin_input in [coin_id.lower(), coin_name.lower()]:
            coin_to_remove = coin_id
            break

    if not coin_to_remove:
        update.message.reply_text(f"❌ Coin '{coin_input}' isn't in your list.")
        return

    del coins_available[coin_to_remove]
    save_config(config)

    update.message.reply_text(f"🗑️ Removed {coin_name} successfully.")


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
    dispatcher.add_handler(CommandHandler('addcoin', addcoin))
    dispatcher.add_handler(CommandHandler('removecoin', removecoin))


    scheduler = BackgroundScheduler(timezone=utc)
    trigger = IntervalTrigger(minutes=1, timezone=utc)
    scheduler.add_job(update_all_prices, trigger)
    scheduler.add_job(check_price, trigger, args=[dispatcher])
    update_all_prices()

    scheduler.start()

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
