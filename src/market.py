__version__ = 'rev14'

import asyncio
import datetime
import json
import re
import statistics
import threading
import time

import websockets


BINANCE_WS = 'wss://stream.binance.com:9443/stream'
KRAKEN_WS = 'wss://ws.kraken.com/v2'
COINBASE_WS = 'wss://advanced-trade-ws.coinbase.com'
HISTORY_DAYS = 30


class MarketFeed:
    """Public multi-exchange market-data collector.

    Binance: 1-minute kline stream.
    Kraken: 1-minute OHLC stream.
    Coinbase: public market_trades stream, aggregated locally to 1-minute OHLCV.

    Only finalized 1-minute candles are persisted. Latest venue prices stay in memory
    and are combined with a median, so one stale/outlier venue does not dominate.
    """

    def __init__(self, db, stale_seconds=30.0):
        self.db = db
        self.stale_seconds = float(stale_seconds)
        self._latest = {}
        self._latest_lock = threading.RLock()
        self._pending = {}
        self._tasks = []
        self._stop = asyncio.Event()
        self._status = {}

    async def start(self):
        self._stop.clear()
        await asyncio.to_thread(self.db.prune_market_history, HISTORY_DAYS)
        self._tasks = [
            asyncio.create_task(self._binance_loop(), name='market-binance'),
            asyncio.create_task(self._kraken_loop(), name='market-kraken'),
            asyncio.create_task(self._coinbase_loop(), name='market-coinbase'),
            asyncio.create_task(self._pending_flush_loop(), name='market-candle-flush'),
            asyncio.create_task(self._prune_loop(), name='market-prune'),
        ]

    async def stop(self):
        self._stop.set()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._flush_expired_pending(force=True)

    def get_prices(self, max_age_seconds=None):
        max_age = self.stale_seconds if max_age_seconds is None else float(max_age_seconds)
        now = time.time()
        result = {}
        with self._latest_lock:
            for coin_id, venues in self._latest.items():
                values = [
                    float(price)
                    for price, ts in venues.values()
                    if price is not None and price > 0 and now - ts <= max_age
                ]
                if values:
                    result[coin_id] = float(statistics.median(values))
        return result

    def get_price(self, coin_id, max_age_seconds=None):
        return self.get_prices(max_age_seconds=max_age_seconds).get(coin_id)

    def _set_latest(self, coin_id, exchange, price):
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        with self._latest_lock:
            self._latest.setdefault(coin_id, {})[exchange] = (price, time.time())

    def _symbols(self, exchange):
        rows = self.db.get_market_symbols(exchange=exchange, required_only=True)
        return {row['symbol']: row['coin_id'] for row in rows if row.get('enabled', 1)}

    async def _store_candle(self, candle):
        await asyncio.to_thread(self.db.upsert_market_candle, candle)
        await asyncio.to_thread(
            self.db.rebuild_aggregate_candle, candle['coin_id'], candle['ts_utc']
        )

    @staticmethod
    def _minute_iso(value):
        if isinstance(value, (int, float)):
            dt = datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
        elif isinstance(value, datetime.datetime):
            dt = value
        else:
            dt = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0)
        return dt.isoformat()

    async def _binance_loop(self):
        exchange = 'binance'
        while not self._stop.is_set():
            symbols = self._symbols(exchange)
            if not symbols:
                self._status[exchange] = 'idle: no symbols'
                await asyncio.sleep(10)
                continue

            fingerprint = tuple(sorted(symbols.items()))
            streams = '/'.join(f'{symbol.lower()}@kline_1m' for symbol in symbols)
            url = f'{BINANCE_WS}?streams={streams}'
            try:
                self._status[exchange] = f'connecting: {len(symbols)} symbols'
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self._status[exchange] = f'connected: {len(symbols)} symbols'
                    last_mapping_check = time.monotonic()
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        msg = json.loads(raw)
                        data = msg.get('data', msg)
                        if data.get('e') != 'kline':
                            continue
                        symbol = str(data.get('s', ''))
                        coin_id = symbols.get(symbol)
                        if coin_id is None:
                            # Binance events are uppercase while mapping is normally uppercase.
                            coin_id = symbols.get(symbol.upper()) or symbols.get(symbol.lower())
                        if coin_id is None:
                            continue
                        k = data.get('k', {})
                        close = float(k.get('c', 0) or 0)
                        self._set_latest(coin_id, exchange, close)
                        if k.get('x'):
                            volume = float(k.get('v', 0) or 0)
                            quote_volume = float(k.get('q', 0) or 0)
                            vwap = quote_volume / volume if volume > 0 else close
                            candle = {
                                'ts_utc': self._minute_iso(float(k['t']) / 1000.0),
                                'coin_id': coin_id,
                                'exchange': exchange,
                                'open': float(k['o']),
                                'high': float(k['h']),
                                'low': float(k['l']),
                                'close': close,
                                'volume': volume,
                                'vwap': vwap,
                                'trades': int(k.get('n', 0) or 0),
                                'source_count': 1,
                            }
                            await self._store_candle(candle)

                        if time.monotonic() - last_mapping_check >= 15:
                            if tuple(sorted(self._symbols(exchange).items())) != fingerprint:
                                break
                            last_mapping_check = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status[exchange] = f'error: {exc}'
                print(f'Binance market feed error: {exc}')
                await asyncio.sleep(5)

    async def _kraken_loop(self):
        exchange = 'kraken'
        while not self._stop.is_set():
            symbols = self._symbols(exchange)
            if not symbols:
                self._status[exchange] = 'idle: no symbols'
                await asyncio.sleep(10)
                continue
            fingerprint = tuple(sorted(symbols.items()))
            try:
                self._status[exchange] = f'connecting: {len(symbols)} symbols'
                async with websockets.connect(KRAKEN_WS, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(json.dumps({
                        'method': 'subscribe',
                        'params': {
                            'channel': 'ohlc',
                            'symbol': list(symbols.keys()),
                            'interval': 1,
                            'snapshot': True,
                        },
                    }))
                    self._status[exchange] = f'connected: {len(symbols)} symbols'
                    last_mapping_check = time.monotonic()
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        msg = json.loads(raw)
                        if msg.get('success') is False:
                            error = str(msg.get('error', msg))
                            print(f'Kraken subscription warning: {error}')
                            match = re.search(r'Currency pair not supported\s+([^\s]+)', error, re.IGNORECASE)
                            if match:
                                rejected = match.group(1).upper()
                                coin_id = await asyncio.to_thread(
                                    self.db.disable_market_symbol, exchange, rejected
                                )
                                if coin_id:
                                    print(f'Disabled unsupported Kraken market: {coin_id} -> {rejected}')
                                    break
                            continue
                        if msg.get('channel') != 'ohlc':
                            continue
                        data = msg.get('data') or []
                        # A snapshot may contain multiple candles; keep the newest as pending
                        # and safely persist older ones as completed.
                        grouped = {}
                        for item in data:
                            symbol = item.get('symbol')
                            coin_id = symbols.get(symbol)
                            if coin_id is None:
                                continue
                            grouped.setdefault(coin_id, []).append(item)
                        for coin_id, items in grouped.items():
                            items.sort(key=lambda x: x.get('interval_begin', ''))
                            for item in items[:-1]:
                                await self._store_candle(self._kraken_candle(coin_id, item))
                            if items:
                                item = items[-1]
                                close = float(item.get('close', 0) or 0)
                                self._set_latest(coin_id, exchange, close)
                                await self._set_pending_candle(self._kraken_candle(coin_id, item))

                        if time.monotonic() - last_mapping_check >= 15:
                            if tuple(sorted(self._symbols(exchange).items())) != fingerprint:
                                break
                            last_mapping_check = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status[exchange] = f'error: {exc}'
                print(f'Kraken market feed error: {exc}')
                await asyncio.sleep(5)

    def _kraken_candle(self, coin_id, item):
        close = float(item.get('close', 0) or 0)
        return {
            'ts_utc': self._minute_iso(item['interval_begin']),
            'coin_id': coin_id,
            'exchange': 'kraken',
            'open': float(item.get('open', close)),
            'high': float(item.get('high', close)),
            'low': float(item.get('low', close)),
            'close': close,
            'volume': float(item.get('volume', 0) or 0),
            'vwap': float(item.get('vwap', close) or close),
            'trades': int(item.get('trades', 0) or 0),
            'source_count': 1,
        }

    async def _coinbase_loop(self):
        exchange = 'coinbase'
        while not self._stop.is_set():
            symbols = self._symbols(exchange)
            if not symbols:
                self._status[exchange] = 'idle: no symbols'
                await asyncio.sleep(10)
                continue
            fingerprint = tuple(sorted(symbols.items()))
            try:
                self._status[exchange] = f'connecting: {len(symbols)} symbols'
                async with websockets.connect(COINBASE_WS, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(json.dumps({
                        'type': 'subscribe',
                        'product_ids': list(symbols.keys()),
                        'channel': 'market_trades',
                    }))
                    # Heartbeats prevent quiet subscriptions being closed by intermediaries.
                    await ws.send(json.dumps({
                        'type': 'subscribe',
                        'channel': 'heartbeats',
                    }))
                    self._status[exchange] = f'connected: {len(symbols)} symbols'
                    last_mapping_check = time.monotonic()
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        msg = json.loads(raw)
                        if msg.get('channel') != 'market_trades':
                            continue
                        for event in msg.get('events', []):
                            for trade in event.get('trades', []):
                                product_id = trade.get('product_id')
                                coin_id = symbols.get(product_id)
                                if coin_id is None:
                                    continue
                                try:
                                    price = float(trade['price'])
                                    size = float(trade.get('size', 0) or 0)
                                    timestamp = trade.get('time') or msg.get('timestamp')
                                except (KeyError, TypeError, ValueError):
                                    continue
                                self._set_latest(coin_id, exchange, price)
                                await self._accumulate_trade(
                                    coin_id, exchange, timestamp, price, size
                                )

                        if time.monotonic() - last_mapping_check >= 15:
                            if tuple(sorted(self._symbols(exchange).items())) != fingerprint:
                                break
                            last_mapping_check = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status[exchange] = f'error: {exc}'
                print(f'Coinbase market feed error: {exc}')
                await asyncio.sleep(5)

    async def _accumulate_trade(self, coin_id, exchange, timestamp, price, size):
        minute = self._minute_iso(timestamp)
        key = (coin_id, exchange)
        current = self._pending.get(key)
        if current is not None and current['ts_utc'] != minute:
            await self._store_candle(current)
            current = None
        if current is None:
            current = {
                'ts_utc': minute,
                'coin_id': coin_id,
                'exchange': exchange,
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 0.0,
                'vwap': price,
                'trades': 0,
                'source_count': 1,
                '_notional': 0.0,
            }
            self._pending[key] = current
        current['high'] = max(current['high'], price)
        current['low'] = min(current['low'], price)
        current['close'] = price
        current['volume'] += size
        current['_notional'] = current.get('_notional', 0.0) + price * size
        current['trades'] += 1
        if current['volume'] > 0:
            current['vwap'] = current['_notional'] / current['volume']

    async def _set_pending_candle(self, candle):
        key = (candle['coin_id'], candle['exchange'])
        previous = self._pending.get(key)
        if previous is not None and previous['ts_utc'] != candle['ts_utc']:
            await self._store_candle(previous)
        self._pending[key] = candle

    async def _pending_flush_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(5)
                await self._flush_expired_pending(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'Market candle flush error: {exc}')

    async def _flush_expired_pending(self, force=False):
        current_minute = self._minute_iso(datetime.datetime.now(datetime.timezone.utc))
        for key, candle in list(self._pending.items()):
            if force or candle['ts_utc'] < current_minute:
                self._pending.pop(key, None)
                clean = {k: v for k, v in candle.items() if not k.startswith('_')}
                await self._store_candle(clean)

    async def _prune_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(3600)
                await asyncio.to_thread(self.db.prune_market_history, HISTORY_DAYS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'Market history prune error: {exc}')
