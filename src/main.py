__version__ = 'rev14'

import asyncio
import datetime
import json
import os
import textwrap
import threading
import time

import requests
import db
import trend
from market import MarketFeed
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, CallbackContext


CONFIG_FILE = 'config.json'
COINGECKO_PRICE_CACHE = {}
COINGECKO_CACHE_LOCK = threading.RLock()
COINGECKO_REFRESH_LOCK = threading.Lock()
MARKET_FEED = None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    return {}


def refresh_fallback_prices():
    global COINGECKO_PRICE_CACHE

    with COINGECKO_REFRESH_LOCK:
        config = load_config()
        coin_ids = db.get_required_coin_ids()

        if not coin_ids:
            print('No coins to update.')
            return

        url = 'https://api.coingecko.com/api/v3/simple/price'
        params = {
            'ids': ','.join(coin_ids),
            'vs_currencies': 'usd',
        }
        headers = {'accept': 'application/json'}
        if 'geckoapi' in config:
            headers['x-cg-api-key'] = config['geckoapi']

        try:
            response = requests.get(url, params=params, headers=headers, timeout=(5, 15))
            response.raise_for_status()
            prices = response.json()
            now = utc_now().isoformat()

            with COINGECKO_CACHE_LOCK:
                COINGECKO_PRICE_CACHE = prices

            print(f'CoinGecko fallback prices updated at {now} UTC ({len(prices)} coins)')
        except Exception as e:
            print(f'Error fetching coin prices: {e}')


def _current_prices():
    prices = {}
    with COINGECKO_CACHE_LOCK:
        prices.update({
            coin_id: info.get('usd')
            for coin_id, info in COINGECKO_PRICE_CACHE.items()
            if isinstance(info, dict) and info.get('usd') is not None
        })
    if MARKET_FEED is not None:
        prices.update(MARKET_FEED.get_prices())
    return prices


def get_price(coin_id, get_missing=True):
    if MARKET_FEED is not None:
        price = MARKET_FEED.get_price(coin_id)
        if price is not None:
            return price

    with COINGECKO_CACHE_LOCK:
        coin_info = COINGECKO_PRICE_CACHE.get(coin_id)
        if coin_info:
            return coin_info.get('usd')

    print(f'No live/cached price for {coin_id}.')
    if get_missing:
        refresh_fallback_prices()
        return get_price(coin_id, False)
    return None


async def start(update: Update, context: CallbackContext) -> None:
    coins_available = db.get_tracked_coins()

    if not coins_available:
        await update.message.reply_text('⚠️ No coins available. Add coins using /addcoin.')
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'select_coin_{coin_id}')]
        for coin_id, name in coins_available.items()
    ]
    keyboard.insert(0, [InlineKeyboardButton('Show wallet diary', callback_data='history')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please select a coin or command:', reply_markup=reply_markup)


_INPUT_MODE_KEYS = ('setting_above', 'setting_below', 'setting_buy', 'setting_sell')


def _clear_input_modes(context):
    for key in _INPUT_MODE_KEYS:
        context.user_data.pop(key, None)


def _set_input_mode(context, mode):
    _clear_input_modes(context)
    context.user_data[mode] = True


async def button_cb(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    coins_available = db.get_tracked_coins()

    if query.data == 'history':
        await history(update, context)
        return

    if query.data.startswith('select_coin_'):
        coin_id = query.data.replace('select_coin_', '')
        coin_name = coins_available.get(coin_id)

        if not coin_name:
            await query.edit_message_text('⚠️ Selected coin is no longer available.')
            return

        context.user_data['coin'] = coin_id
        context.user_data['coin_name'] = coin_name
        _clear_input_modes(context)

        keyboard = [
            [InlineKeyboardButton(f'Get {coin_name} Price', callback_data='get_price')],
            [InlineKeyboardButton(f'🔎 Analyze {coin_name}', callback_data='analyze_now')],
            [
                InlineKeyboardButton(f'🟢 Buy {coin_name}', callback_data='buy_prompt'),
                InlineKeyboardButton(f'🔴 Sell {coin_name}', callback_data='sell_prompt'),
            ],
            [InlineKeyboardButton(f'Set Above Price Alert for {coin_name}', callback_data='set_above')],
            [InlineKeyboardButton(f'Set Below Price Alert for {coin_name}', callback_data='set_below')],
        ]
        await query.edit_message_text(
            text=f'Selected {coin_name}. Choose an option:',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif query.data == 'get_price':
        coin_id = context.user_data.get('coin')
        coin_name = context.user_data.get('coin_name')
        if not coin_id:
            await query.edit_message_text(text='Please select a coin first by sending /start')
            return
        price = await asyncio.to_thread(get_price, coin_id)
        if price is not None:
            await query.edit_message_text(text=f'{coin_name} price: ${price:.2f} USD')
        else:
            await query.edit_message_text(text=f'Failed to retrieve the {coin_name} price.')
    elif query.data == 'analyze_now':
        coin_id = context.user_data.get('coin')
        coin_name = context.user_data.get('coin_name')
        if not coin_id or not coin_name:
            await query.message.reply_text('Please select a coin first by sending /start')
            return
        for chunk in await _run_manual_analysis(str(query.message.chat_id), coin_id, coin_name):
            await query.message.reply_text(chunk)
    elif query.data == 'buy_prompt':
        coin_name = context.user_data.get('coin_name')
        _set_input_mode(context, 'setting_buy')
        await query.message.reply_text(
            f'Enter BUY for {coin_name}:\n'
            '<amount_usd> [execution_price] [fee_usd]\n'
            'Examples: 100  or  100 62340.50 0.50'
        )
    elif query.data == 'sell_prompt':
        coin_name = context.user_data.get('coin_name')
        _set_input_mode(context, 'setting_sell')
        await query.message.reply_text(
            f'Enter SELL for {coin_name}:\n'
            '<quantity|max> [execution_price] [fee_usd]\n'
            'Examples: 0.5  or  max 68000 1.25'
        )
    elif query.data == 'set_above':
        coin_name = context.user_data.get('coin_name')
        _set_input_mode(context, 'setting_above')
        await query.message.reply_text(f'Please send the price above which you want to get notified for {coin_name}:')
    elif query.data == 'set_below':
        coin_name = context.user_data.get('coin_name')
        _set_input_mode(context, 'setting_below')
        await query.message.reply_text(f'Please send the price below which you want to get notified for {coin_name}:')


async def handle_text_input(update: Update, context: CallbackContext) -> None:
    if 'setting_buy' in context.user_data:
        if await _execute_buy(update, context, update.message.text.split()):
            context.user_data.pop('setting_buy', None)
        return

    if 'setting_sell' in context.user_data:
        if await _execute_sell(update, context, update.message.text.split()):
            context.user_data.pop('setting_sell', None)
        return

    setting_above = 'setting_above' in context.user_data
    setting_below = 'setting_below' in context.user_data
    if not setting_above and not setting_below:
        return

    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id:
        await update.message.reply_text('Please select a coin first by sending /start')
        return

    try:
        price = float(update.message.text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text('Invalid price. Please enter a positive number.')
        return

    if setting_above:
        db.set_price_alert(chat_id, coin_id, 'above', price)
        context.user_data.pop('setting_above', None)
        await update.message.reply_text(f'You will be notified if the {coin_name} price goes above ${price:.2f}')
    else:
        db.set_price_alert(chat_id, coin_id, 'below', price)
        context.user_data.pop('setting_below', None)
        await update.message.reply_text(f'You will be notified if the {coin_name} price goes below ${price:.2f}')


def _fetch_coingecko_coin_list():
    config = load_config()
    url = 'https://api.coingecko.com/api/v3/coins/list'
    headers = {'accept': 'application/json'}
    if 'geckoapi' in config:
        headers['x-cg-api-key'] = config['geckoapi']
    response = requests.get(url, headers=headers, timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def verify_coin(symbol_or_name):
    try:
        data = _fetch_coingecko_coin_list()
        value = symbol_or_name.strip().casefold()

        # Exact name -> exact CoinGecko ID -> exact symbol.
        for coin in data:
            if coin['name'].strip().casefold() == value:
                return coin['id'], coin['name'], coin['symbol'].upper()
        for coin in data:
            if coin['id'].strip().casefold() == value:
                return coin['id'], coin['name'], coin['symbol'].upper()
        for coin in data:
            if coin['symbol'].strip().casefold() == value:
                return coin['id'], coin['name'], coin['symbol'].upper()
        return None, None, None
    except Exception as e:
        print(f'Error fetching coin list: {e}')
        return None, None, None


def _fetch_exchange_markets():
    """Return public spot markets available on each venue.

    None means that venue discovery failed, in which case mappings stay enabled and
    the WebSocket itself remains the fallback validator.
    """
    markets = {'binance': None, 'kraken': None, 'coinbase': None}

    try:
        response = requests.get(
            'https://api.binance.com/api/v3/exchangeInfo', timeout=(5, 15)
        )
        response.raise_for_status()
        markets['binance'] = {
            str(item.get('symbol', '')).upper()
            for item in response.json().get('symbols', [])
            if item.get('status') == 'TRADING'
        }
    except Exception as e:
        print(f'Binance market discovery failed: {e}')

    try:
        response = requests.get(
            'https://api.kraken.com/0/public/AssetPairs', timeout=(5, 15)
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('error'):
            raise RuntimeError(', '.join(payload['error']))
        def kraken_v2_symbol(wsname):
            parts = str(wsname or '').upper().split('/')
            aliases = {'XBT': 'BTC', 'XDG': 'DOGE'}
            return '/'.join(aliases.get(part, part) for part in parts)

        markets['kraken'] = {
            kraken_v2_symbol(item.get('wsname'))
            for item in payload.get('result', {}).values()
            if item.get('wsname')
        }
    except Exception as e:
        print(f'Kraken market discovery failed: {e}')

    try:
        response = requests.get(
            'https://api.exchange.coinbase.com/products',
            headers={'accept': 'application/json'}, timeout=(5, 15)
        )
        response.raise_for_status()
        markets['coinbase'] = {
            str(item.get('id', '')).upper()
            for item in response.json()
            if item.get('id') and not item.get('trading_disabled', False)
        }
    except Exception as e:
        print(f'Coinbase market discovery failed: {e}')

    return markets


def sync_market_symbols():
    """Refresh CoinGecko symbols and resolve each venue market independently."""
    try:
        by_id = {coin['id']: coin for coin in _fetch_coingecko_coin_list()}
    except Exception as e:
        print(f'Coin symbol synchronization failed: {e}')
        by_id = {}

    # CoinGecko ID is our stable identity; the display ticker may change over time.
    for row in db.get_coin_catalog():
        coin = by_id.get(row['coin_id'])
        if coin and coin.get('symbol'):
            db.set_coin_symbol(row['coin_id'], coin['symbol'])

    available = _fetch_exchange_markets()
    existing = {
        (row['coin_id'], row['exchange']): row['symbol']
        for row in db.get_market_symbols()
    }

    # Some assets intentionally use different tickers on different exchanges.
    # Keep these exceptional identities explicit rather than treating another asset
    # (for example SOL for WSOL) as an equivalent market silently.
    overrides = {
        'the-open-network': {
            'binance': 'GRAMUSDT',
            'kraken': 'TON/USD',
            'coinbase': 'TON-USD',
        },
    }

    for row in db.get_coin_catalog():
        coin_id = row['coin_id']
        symbol = (row.get('symbol') or '').strip().upper()
        if not symbol:
            continue
        defaults = {
            'binance': f'{symbol}USDT',
            'kraken': f'{symbol}/USD',
            'coinbase': f'{symbol}-USD',
        }

        for exchange, default_symbol in defaults.items():
            venue_markets = available.get(exchange)
            candidates = []
            override = overrides.get(coin_id, {}).get(exchange)
            old_symbol = existing.get((coin_id, exchange))
            for candidate in (override, old_symbol, default_symbol):
                if candidate and candidate.upper() not in candidates:
                    candidates.append(candidate.upper())

            if venue_markets is None:
                # Discovery failure is not evidence that a market is unavailable.
                chosen = candidates[0]
                enabled = True
            else:
                chosen = next((candidate for candidate in candidates if candidate in venue_markets), None)
                enabled = chosen is not None
                if chosen is None:
                    chosen = candidates[0]

            db.set_market_symbol(coin_id, exchange, chosen, enabled=enabled)
            if not enabled:
                print(f'Market unavailable: {coin_id} on {exchange}: {chosen} (disabled)')


def _build_trend_analytics(coin_ids):
    analytics = {}
    if not coin_ids:
        return analytics

    now = utc_now()
    since = (now - datetime.timedelta(hours=24)).isoformat()
    candle_history = db.get_market_candles(list(coin_ids), since, exchange='aggregate')

    for coin_id in coin_ids:
        candles = candle_history.get(coin_id, [])
        if len(candles) < 2:
            continue

        try:
            df = trend.convert_candles(candles)
            if len(df) < 2:
                continue

            df['vwap_reference'] = trend.calc_candle_vwap(df)
            df['atr'] = trend.calc_atr_pct_from_ohlcv(df, period=14, freq='1h')

            slope_sec = trend.calc_theil_sen_slope(df)
            horizons = trend.calc_horizon_trends(df, horizons=(1, 4, 24))
            quality = trend.analyze_data_quality(
                df, now=now, expected_interval_seconds=60.0, horizon_hours=24.0
            )

            open_price = float(df['open'].iloc[0])
            close_price = float(df['close'].iloc[-1])
            min_idx = df['low'].idxmin()
            max_idx = df['high'].idxmax()
            min_price = float(df['low'].iloc[min_idx])
            max_price = float(df['high'].iloc[max_idx])
            if open_price <= 0:
                continue

            pct_change = (close_price - open_price) / open_price * 100.0
            drawdown_pct = (close_price - max_price) / max_price * 100.0 if max_price > 0 else 0.0
            rebound_pct = (close_price - min_price) / min_price * 100.0 if min_price > 0 else 0.0

            summary = {
                'open_price': open_price,
                'close_price': close_price,
                'min_price': min_price,
                'max_price': max_price,
                'percent_change': float(pct_change),
                'drawdown_from_24h_high_pct': float(drawdown_pct),
                'rebound_from_24h_low_pct': float(rebound_pct),
            }
            signals = trend.detect_signals(df, horizons=horizons, data_quality=quality)
            analytics[coin_id] = {
                'summary': summary,
                'signals': signals,
                'quality': quality,
                'horizons': horizons,
                'source_count': int(df['source_count'].iloc[-1]) if 'source_count' in df.columns else 1,
                'avg_source_count': float(df['source_count'].mean()) if 'source_count' in df.columns else 1.0,
            }
        except Exception as e:
            print(f'Error calculating trend for {coin_id}: {e}')

    return analytics


def _contextual_assessment(market_assessment, day_move, pnl_pct):
    if 'SELL' in market_assessment:
        if pnl_pct >= 5.0:
            return 'PROTECT_PROFIT'
        if pnl_pct < 0.0:
            return 'SELL_SIGNAL_WITH_UNREALIZED_LOSS'
        return 'SELL_SIGNAL_WITH_OPEN_POSITION'
    if 'BUY' in market_assessment:
        if pnl_pct < 0.0:
            return 'BUY_SIGNAL_WITH_UNREALIZED_LOSS'
        return 'BUY_SIGNAL_WITH_OPEN_POSITION'
    if day_move == 'DOWN' and pnl_pct < 0.0:
        return 'DOWNTREND_WITH_UNREALIZED_LOSS'
    if day_move == 'UP' and pnl_pct > 0.0:
        return 'UPTREND_SUPPORTING_POSITION'
    return 'OPEN_POSITION_CONTEXT'


def _analysis_message(coin_name, coin_analytics, current_price, position=None, stop=None,
                      title='Confirmed Analysis Alert', confirmation='3/3'):
    summary = coin_analytics['summary']
    signals = coin_analytics['signals']
    quality = coin_analytics['quality']
    horizons = coin_analytics['horizons']

    if summary['percent_change'] >= 2.0:
        day_move = 'UP'
    elif summary['percent_change'] <= -2.0:
        day_move = 'DOWN'
    else:
        day_move = 'NONE'

    position = position or {}
    quantity = float(position.get('quantity', 0.0))
    cost_basis = float(position.get('cost_basis_usd', 0.0))
    current_value = quantity * float(current_price or 0.0)
    pnl_pct = ((current_value - cost_basis) / cost_basis * 100.0) if cost_basis > 0 else 0.0
    contextual = (
        _contextual_assessment(signals['assessment'], day_move, pnl_pct)
        if quantity > 0 and cost_basis > 0 else 'NO_OPEN_POSITION'
    )

    def horizon_text(key):
        item = horizons.get(key, {})
        return f'{item.get("direction", "UNKNOWN")} ({item.get("slope_pct_hour", 0.0):+.4f}%/h)'

    deviation_atr = signals.get('reference_deviation_atr')
    deviation_atr_text = 'N/A' if deviation_atr is None else f'{deviation_atr:+.2f} ATR'
    q_max_gap = quality.get('max_gap_seconds')
    q_gap_text = 'N/A' if q_max_gap is None else f'{q_max_gap / 60.0:.1f} min'
    stop_text = 'N/A'
    high_water_text = 'N/A'
    if stop is not None:
        if stop.get('trailing_stop_price') is not None:
            stop_text = f'${stop["trailing_stop_price"]:.2f}'
        if stop.get('high_water_price') is not None:
            high_water_text = f'${stop["high_water_price"]:.2f}'
    atr_pct = signals.get('atr_pct')
    atr_text = 'N/A' if atr_pct is None else f'{atr_pct:.2f}%'

    return textwrap.dedent(f'''
        🔎 {coin_name} {title}:
        assessment = {signals['assessment']}
        position_context = {contextual}
        confirmation = {confirmation}
        venue_sources = {coin_analytics.get('source_count', 0)} latest / {coin_analytics.get('avg_source_count', 0.0):.1f} avg
        data_quality = {quality['state']} ({quality['coverage'] * 100.0:.1f}% samples, max gap {q_gap_text})

        Market regime:
        structure = {signals['market_regime']['structure']}
        volatility = {signals['market_regime']['volatility']}
        efficiency_ratio = {signals['efficiency_ratio']:.3f}

        Multi-timeframe trend:
        1h  = {horizon_text('1h')}
        4h  = {horizon_text('4h')}
        24h = {horizon_text('24h')}
        slope_acceleration = {signals['slope_acceleration']} ({signals['slope_acceleration_pct_hour']:+.4f}%/h²-like delta)

        24h market context:
        change = {summary['percent_change']:+.2f}%
        high = ${summary['max_price']:.2f}
        low = ${summary['min_price']:.2f}
        drawdown_from_high = {summary['drawdown_from_24h_high_pct']:+.2f}%
        rebound_from_low = {summary['rebound_from_24h_low_pct']:+.2f}%
        reference = {signals['reference_name']} ${signals['reference_price']:.2f}
        ATR(1h,14) = {atr_text}

        Signal chain:
        {signals['reference_name']}_deviation = {signals['reference_deviation_pct']:+.2f}% ({deviation_atr_text})
        candidate = {signals['mean_reversion_candidate']}
        momentum = {signals['momentum']} ({signals['momentum_pct_hour']:+.4f}%/h)
        filter = {signals['signal_filter']}
        mean_reversion = {signals['mean_reversion']}
        24h_move = {day_move}

        Position:
        unrealized_P/L = {pnl_pct:+.2f}%
        high_water = {high_water_text}
        ATR_trailing_stop = {stop_text}
        position_size_multiplier = {signals['position_size_multiplier']}
    ''').strip()


async def _run_manual_analysis(chat_id, coin_id, coin_name):
    current_price = get_price(coin_id, get_missing=False)
    if current_price is None:
        await asyncio.to_thread(refresh_fallback_prices)
        current_price = get_price(coin_id, get_missing=False)
    if current_price is None:
        return ['❌ Current price is unavailable.']

    analytics = await asyncio.to_thread(_build_trend_analytics, {coin_id})
    coin_analytics = analytics.get(coin_id)
    if coin_analytics is None:
        return ['⚠️ Not enough price history yet to run the analysis.']

    atr_value = coin_analytics['signals'].get('atr_pct')
    stop = None
    if atr_value is not None:
        stops = db.update_position_trailing_stops(
            {coin_id: current_price}, {coin_id: float(atr_value)}, multiplier=2.0
        )
        stop = stops.get((chat_id, coin_id))

    position = db.get_position_contexts().get((chat_id, coin_id), {})
    message = _analysis_message(
        coin_name, coin_analytics, current_price, position=position, stop=stop,
        title='Manual Analysis', confirmation='manual (confirmation/cooldown bypassed)',
    )
    return _split_message(message)


async def analyze(update: Update, context: CallbackContext) -> None:
    if len(context.args) > 1:
        await update.message.reply_text('Usage: /analyze [coin]')
        return

    required_ids = set(db.get_required_coin_ids())
    coin_names = db.get_coin_names(required_ids)
    coin_id = None

    if context.args:
        value = context.args[0].strip().lower()
        for candidate_id, candidate_name in coin_names.items():
            if value in (candidate_id.lower(), candidate_name.lower()):
                coin_id = candidate_id
                break
        if coin_id is None:
            resolved_id, _, _ = await asyncio.to_thread(verify_coin, context.args[0])
            if resolved_id in required_ids:
                coin_id = resolved_id
    else:
        selected = context.user_data.get('coin')
        if selected in required_ids:
            coin_id = selected

    if coin_id is None:
        await update.message.reply_text('Select a coin with /start or use /analyze <coin>.')
        return

    for chunk in await _run_manual_analysis(
            str(update.message.chat_id), coin_id, coin_names.get(coin_id, coin_id.capitalize())):
        await update.message.reply_text(chunk)


async def check_price(bot):
    prices = _current_prices()
    if not prices:
        await asyncio.to_thread(refresh_fallback_prices)
        prices = _current_prices()
        if not prices:
            print('⚠️ Price cache empty, skipping alert check.')
            return

    position_keys = db.get_position_keys()
    coin_ids = {coin_id for _, coin_id in position_keys}
    analytics = await asyncio.to_thread(_build_trend_analytics, coin_ids)
    atr_by_coin = {}
    for coin_id, data in analytics.items():
        value = data['signals'].get('atr_pct')
        if value is not None:
            atr_by_coin[coin_id] = float(value)

    trailing_stops = db.update_position_trailing_stops(prices, atr_by_coin, multiplier=2.0)
    position_contexts = db.get_position_contexts()
    coin_names = db.get_coin_names(set(prices.keys()) | coin_ids)
    notifications = []

    for event in db.consume_price_alerts(prices):
        coin_name = coin_names.get(event['coin_id'], event['coin_id'].capitalize())
        if event['kind'] == 'above':
            text = (
                f'⚠️ {coin_name} price is above ${event["threshold"]:.2f} 📈: '
                f'Current price is ${event["price"]:.2f}'
            )
        else:
            text = (
                f'⚠️ {coin_name} price is below ${event["threshold"]:.2f} 📉: '
                f'Current price is ${event["price"]:.2f}'
            )
        notifications.append((event['chat_id'], text))

    for event in db.update_lot_pnl_zones(prices):
        coin_name = coin_names.get(event['coin_id'], event['coin_id'].capitalize())
        emoji = '📈' if event['zone'] == 'PROFIT' else '📉'
        notifications.append((
            event['chat_id'],
            (
                f'{emoji} {coin_name} Purchase Alert: {event["old_zone"]} → {event["zone"]}\n'
                f'• Bought on: {event["bought_on"]}\n'
                f'• Execution price: ${event["bought_at"]:.2f}\n'
                f'• Effective cost/coin: ${event["effective_cost_per_coin"]:.2f}\n'
                f'• Current price: ${event["current_price"]:.2f}\n'
                f'• Profit/Loss: {event["pnl_pct"]:.2f}%'
            ),
        ))

    for event in db.consume_trailing_stop_events(prices):
        coin_name = coin_names.get(event['coin_id'], event['coin_id'].capitalize())
        notifications.append((
            event['chat_id'],
            (
                f'🛑 {coin_name} ATR Trailing Stop Crossed\n'
                f'• Current price: ${event["current_price"]:.2f}\n'
                f'• Trailing stop: ${event["trailing_stop_price"]:.2f}\n'
                f'• Position high-water: ${event["high_water_price"]:.2f}\n'
                f'• Effective average cost: ${event["avg_cost_per_coin"]:.2f}\n'
                f'• Position P/L: {event["pnl_pct"]:+.2f}%'
            ),
        ))

    now = utc_now()
    now_iso = now.isoformat()
    cooldown_until = (now + datetime.timedelta(hours=3)).isoformat()

    for chat_id, coin_id in position_keys:
        coin_analytics = analytics.get(coin_id)
        if coin_analytics is None:
            db.update_signal_confirmation(chat_id, coin_id, 'NONE', now_iso, required=3)
            continue

        summary = coin_analytics['summary']
        signals = coin_analytics['signals']
        quality = coin_analytics['quality']

        if summary['percent_change'] >= 2.0:
            day_move = 'UP'
        elif summary['percent_change'] <= -2.0:
            day_move = 'DOWN'
        else:
            day_move = 'NONE'

        if quality['state'] == 'BAD':
            db.update_signal_confirmation(chat_id, coin_id, 'NONE', now_iso, required=3)
            continue

        if signals['mean_reversion'] != 'NONE':
            raw_signal = signals['assessment']
        elif day_move != 'NONE':
            raw_signal = f'DAY_MOVE_{day_move}'
        else:
            raw_signal = 'NONE'

        confirmation = db.update_signal_confirmation(
            chat_id, coin_id, raw_signal, now_iso, required=3
        )
        if not confirmation['confirmed']:
            continue

        if not db.claim_signal_notification(chat_id, coin_id, now_iso, cooldown_until):
            continue

        coin_name = coin_names.get(coin_id, coin_id.capitalize())
        stop = trailing_stops.get((chat_id, coin_id))
        position = position_contexts.get((chat_id, coin_id), {})

        message = _analysis_message(
            coin_name, coin_analytics, prices.get(coin_id),
            position=position, stop=stop,
            title='Confirmed Analysis Alert',
            confirmation=f'{confirmation["count"]}/{confirmation["required"]}',
        )
        notifications.append((chat_id, message))

    for chat_id, message in notifications:
        try:
            await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            print(f'Error sending notification to {chat_id}: {e}')


async def _execute_buy(update: Update, context: CallbackContext, args) -> bool:
    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id or not coin_name:
        await update.message.reply_text('❌ Please select a coin first with /start.')
        return False

    if not 1 <= len(args) <= 3:
        await update.message.reply_text(
            'Usage: /buy <amount_usd> [execution_price] [fee_usd]\n'
            'Example: /buy 100\n'
            'Example: /buy 100 62340.50 0.50'
        )
        return False

    try:
        amount_usd = float(args[0])
        if amount_usd <= 0:
            raise ValueError
        execution_price = float(args[1]) if len(args) >= 2 else None
        fee_usd = float(args[2]) if len(args) >= 3 else 0.0
        if execution_price is not None and execution_price <= 0:
            raise ValueError
        if fee_usd < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            'Invalid values. Amount and price must be positive; fee must be >= 0.\n'
            'Usage: /buy <amount_usd> [execution_price] [fee_usd]'
        )
        return False

    if execution_price is None:
        execution_price = await asyncio.to_thread(get_price, coin_id)
        if execution_price is None or execution_price <= 0:
            await update.message.reply_text(f'❌ Failed to fetch current {coin_name} price.')
            return False

    timestamp = utc_now().isoformat()
    result = db.record_buy(
        chat_id, coin_id, amount_usd, execution_price, timestamp, fee_usd=fee_usd
    )

    await update.message.reply_text(
        f'✅ Logged Buy #{result["transaction_id"]}:\n'
        f'Coin: {coin_name}\n'
        f'Trade value: ${amount_usd:.2f}\n'
        f'Execution price: ${execution_price:.2f}\n'
        f'Fee: ${fee_usd:.2f}\n'
        f'Total cost basis: ${result["cost_basis_usd"]:.2f}\n'
        f'Quantity Bought: {result["quantity"]:.8f}\n'
        f'Date: {timestamp}'
    )
    return True


async def buy(update: Update, context: CallbackContext) -> None:
    _clear_input_modes(context)
    await _execute_buy(update, context, context.args)


async def _execute_sell(update: Update, context: CallbackContext, args) -> bool:
    chat_id = str(update.message.chat_id)
    coin_id = context.user_data.get('coin')
    coin_name = context.user_data.get('coin_name')

    if not coin_id or not coin_name:
        await update.message.reply_text('❌ Please select a coin first with /start.')
        return False

    if not 1 <= len(args) <= 3:
        await update.message.reply_text(
            'Usage: /sell <quantity|max> [execution_price] [fee_usd]\n'
            'Example: /sell 0.5\n'
            'Example: /sell max 68000 1.25'
        )
        return False

    sell_max = args[0].lower() == 'max'
    try:
        requested_quantity = None if sell_max else float(args[0])
        if requested_quantity is not None and requested_quantity <= 0:
            raise ValueError
        execution_price = float(args[1]) if len(args) >= 2 else None
        fee_usd = float(args[2]) if len(args) >= 3 else 0.0
        if execution_price is not None and execution_price <= 0:
            raise ValueError
        if fee_usd < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            'Invalid values. Quantity and price must be positive; fee must be >= 0.\n'
            'Usage: /sell <quantity|max> [execution_price] [fee_usd]'
        )
        return False

    if execution_price is None:
        execution_price = await asyncio.to_thread(get_price, coin_id)
        if execution_price is None or execution_price <= 0:
            await update.message.reply_text('❌ Failed to retrieve the current price. Try again later.')
            return False

    timestamp = utc_now().isoformat()
    result = db.record_sell_lifo(
        chat_id, coin_id, requested_quantity, execution_price, timestamp, fee_usd=fee_usd
    )

    if result['status'] == 'empty':
        await update.message.reply_text(f"⚠️ You don't have any open {coin_name} position.")
        return False
    if result['status'] == 'insufficient':
        await update.message.reply_text(
            f'❌ You do not have enough {coin_name}. You have {result["available"]:.8f}, '
            f'but tried selling {result["requested"]:.8f}.'
        )
        return False

    emoji = '📈' if result['realized_pnl_usd'] >= 0 else '📉'
    await update.message.reply_text(
        f'🔴 Sold #{result["transaction_id"]} {result["quantity"]:.8f} {coin_name} (LIFO)\n'
        f'Execution price: ${execution_price:.2f} per coin\n'
        f'Gross proceeds: ${result["gross_usd"]:.2f}\n'
        f'Fee: ${result["fee_usd"]:.2f}\n'
        f'Net proceeds: ${result["net_usd"]:.2f}\n'
        f'Cost Basis: ${result["cost_basis_usd"]:.2f}\n'
        f'{emoji} Realized P/L: ${result["realized_pnl_usd"]:+.2f}\n'
        f'Date: {timestamp}'
    )
    return True


async def sell(update: Update, context: CallbackContext) -> None:
    _clear_input_modes(context)
    await _execute_sell(update, context, context.args)


def _format_transaction(tx):
    migrated = ' [imported]' if tx['migrated'] else ''
    fee = tx.get('fee_usd', 0.0) or 0.0
    if tx['side'] == 'BUY':
        total_cost = tx['gross_usd'] + fee
        return (
            f'#{tx["id"]} BUY {tx["coin_name"]}{migrated}\n'
            f'  {tx["quantity"]:.8f} @ ${tx["price_per_coin"]:.2f} = ${tx["gross_usd"]:.2f}\n'
            f'  Fee: ${fee:.2f}; total cost basis: ${total_cost:.2f}\n'
            f'  {tx["timestamp_utc"]}'
        )
    emoji = '📈' if tx['realized_pnl_usd'] >= 0 else '📉'
    net = tx['gross_usd'] - fee
    return (
        f'#{tx["id"]} SELL {tx["coin_name"]}{migrated}\n'
        f'  {tx["quantity"]:.8f} @ ${tx["price_per_coin"]:.2f} = ${tx["gross_usd"]:.2f} gross\n'
        f'  Fee: ${fee:.2f}; net proceeds: ${net:.2f}\n'
        f'  Cost basis: ${tx["cost_basis_usd"]:.2f}; {emoji} realized P/L: ${tx["realized_pnl_usd"]:+.2f}\n'
        f'  {tx["timestamp_utc"]}'
    )

def _split_message(text, limit=3900):
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ''
    for block in text.split('\n\n'):
        candidate = block if not current else current + '\n\n' + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


async def _send_history_text(update, context, text):
    chunks = _split_message(text)
    if update.message:
        for chunk in chunks:
            await update.message.reply_text(chunk)
    else:
        await update.callback_query.edit_message_text(chunks[0])
        chat_id = update.effective_chat.id
        for chunk in chunks[1:]:
            await context.bot.send_message(chat_id=chat_id, text=chunk)


async def history(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.effective_chat.id)
    positions = db.get_positions(chat_id)
    recent = db.get_transactions(chat_id, limit=10)

    if not positions and not recent:
        await _send_history_text(update, context, '🗒 Your wallet diary is empty.')
        return

    text = '📗 Wallet Diary\n\n'
    total_cost_basis = 0.0
    total_current_value = 0.0

    if positions:
        text += '📊 Open Positions:\n'
        for position in positions:
            current_price = await asyncio.to_thread(get_price, position['coin_id'])
            if current_price is None:
                text += f'\n🪙 {position["coin_name"]}: price unavailable\n'
                continue

            quantity = position['quantity']
            cost_basis = position['cost_basis_usd']
            current_value = quantity * current_price
            unrealized = current_value - cost_basis
            unrealized_pct = (unrealized / cost_basis * 100.0) if cost_basis > 0 else 0.0
            total_cost_basis += cost_basis
            total_current_value += current_value
            emoji = '📈' if unrealized >= 0 else '📉'
            text += (
                f'\n🪙 {position["coin_name"]}\n'
                f'  Quantity: {quantity:.8f}\n'
                f'  Open Cost Basis: ${cost_basis:.2f}\n'
                f'  Current Value: ${current_value:.2f}\n'
                f'  {emoji} Unrealized P/L: ${unrealized:+.2f} ({unrealized_pct:+.2f}%)\n'
            )
            if position.get('high_water_price') is not None:
                text += f'  High-water price: ${position["high_water_price"]:.2f}\n'
            if position.get('trailing_stop_price') is not None:
                text += f'  ATR trailing stop: ${position["trailing_stop_price"]:.2f}\n'
            if position.get('stop_state'):
                text += f'  Stop state: {position["stop_state"]}\n'

        unrealized_total = total_current_value - total_cost_basis
        unrealized_total_pct = (
            unrealized_total / total_cost_basis * 100.0 if total_cost_basis > 0 else 0.0
        )
        text += (
            f'\nOpen Cost Basis: ${total_cost_basis:.2f}\n'
            f'Current Portfolio Value: ${total_current_value:.2f}\n'
            f'Unrealized P/L: ${unrealized_total:+.2f} ({unrealized_total_pct:+.2f}%)\n'
        )
    else:
        text += '📊 Open Positions: none\n'

    realized = db.get_realized_pnl(chat_id)
    text += f'Realized P/L: ${realized:+.2f}\n'

    if recent:
        text += '\n🧾 Recent Transactions:\n\n'
        text += '\n\n'.join(_format_transaction(tx) for tx in recent)
        text += '\n\nUse /transactions [limit] for more history.'

    await _send_history_text(update, context, text)


async def transactions(update: Update, context: CallbackContext) -> None:
    chat_id = str(update.message.chat_id)
    limit = 30
    if context.args:
        try:
            limit = int(context.args[0])
            if limit < 1 or limit > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text('Usage: /transactions [1..100]')
            return

    rows = db.get_transactions(chat_id, limit=limit)
    if not rows:
        await update.message.reply_text('🧾 No transactions recorded.')
        return

    text = f'🧾 Transaction Log — latest {len(rows)}\n\n'
    text += '\n\n'.join(_format_transaction(tx) for tx in rows)
    for chunk in _split_message(text):
        await update.message.reply_text(chunk)


async def addcoin(update: Update, context: CallbackContext):
    if update is None or update.message is None:
        return

    if len(context.args) != 1:
        await update.message.reply_text('Usage: /addcoin <coin_symbol>\nExample: /addcoin BCH')
        return

    coin_input = context.args[0].strip()
    coin_id, coin_name, coin_symbol = await asyncio.to_thread(verify_coin, coin_input)

    if not coin_id:
        await update.message.reply_text(f"❌ Coin '{coin_input}' not found on CoinGecko.")
        return

    if not db.add_tracked_coin(coin_id, coin_name, coin_symbol):
        await update.message.reply_text(f'⚠️ {coin_name} is already available.')
        return

    db.ensure_default_market_symbols(coin_id, coin_symbol)
    await update.message.reply_text(f'✅ Added {coin_name} ({coin_id}) successfully!')


async def removecoin(update: Update, context: CallbackContext):
    if update is None or update.message is None:
        return

    if len(context.args) != 1:
        await update.message.reply_text('Usage: /removecoin <coin_symbol>\nExample: /removecoin BCH')
        return

    status, coin_name = db.remove_tracked_coin(context.args[0].strip())
    if status == 'not_found':
        await update.message.reply_text(f"❌ Coin '{context.args[0]}' isn't in your list.")
    elif status == 'held':
        await update.message.reply_text(f'❌ Cannot remove {coin_name}: there is still an open position.')
    elif status == 'alerted':
        await update.message.reply_text(f'❌ Cannot remove {coin_name}: there is still an active price alert.')
    else:
        await update.message.reply_text(f'🗑️ Removed {coin_name} successfully.')


async def _periodic_loop(application):
    last_fallback_refresh = time.monotonic()
    while True:
        try:
            await asyncio.sleep(60)
            if time.monotonic() - last_fallback_refresh >= 300:
                await asyncio.to_thread(refresh_fallback_prices)
                last_fallback_refresh = time.monotonic()
            await check_price(application.bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f'Periodic job failed: {e}')


async def _post_init(application):
    global MARKET_FEED
    await asyncio.to_thread(sync_market_symbols)
    MARKET_FEED = MarketFeed(db)
    await MARKET_FEED.start()

    # Seed fallback prices while WebSocket subscriptions establish.
    await asyncio.to_thread(refresh_fallback_prices)
    application.bot_data['periodic_task'] = asyncio.create_task(
        _periodic_loop(application), name='price-analysis-loop'
    )


async def _post_stop(application):
    global MARKET_FEED
    task = application.bot_data.pop('periodic_task', None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if MARKET_FEED is not None:
        await MARKET_FEED.stop()
        MARKET_FEED = None


def main():
    config = load_config()
    if 'botid' not in config:
        raise RuntimeError('config.json must contain botid')

    db.init_db()

    application = (
        ApplicationBuilder()
        .token(config['botid'])
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CallbackQueryHandler(button_cb))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.add_handler(CommandHandler('buy', buy))
    application.add_handler(CommandHandler('sell', sell))
    application.add_handler(CommandHandler('analyze', analyze))
    application.add_handler(CommandHandler('history', history))
    application.add_handler(CommandHandler('transactions', transactions))
    application.add_handler(CommandHandler('addcoin', addcoin))
    application.add_handler(CommandHandler('removecoin', removecoin))

    application.run_polling()


if __name__ == '__main__':
    main()
