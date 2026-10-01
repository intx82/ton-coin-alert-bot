__version__ = 'rev14'

import datetime
import sqlite3
import statistics


DB_FILE = 'wallet.db'


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso_utc(value=None):
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def init_db():
    with _connect() as conn:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.executescript(
            '''

            CREATE TABLE IF NOT EXISTS coin_catalog (
                coin_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                symbol TEXT
            );

            CREATE TABLE IF NOT EXISTS tracked_coins (
                coin_id TEXT PRIMARY KEY,
                FOREIGN KEY (coin_id) REFERENCES coin_catalog(coin_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts (
                chat_id TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                above REAL,
                below REAL,
                signal_next_at_utc TEXT,
                PRIMARY KEY (chat_id, coin_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                quantity REAL NOT NULL CHECK (quantity > 0),
                price_per_coin REAL NOT NULL CHECK (price_per_coin > 0),
                gross_usd REAL NOT NULL CHECK (gross_usd >= 0),
                cost_basis_usd REAL NOT NULL DEFAULT 0 CHECK (cost_basis_usd >= 0),
                realized_pnl_usd REAL NOT NULL DEFAULT 0,
                fee_usd REAL NOT NULL DEFAULT 0 CHECK (fee_usd >= 0),
                timestamp_utc TEXT NOT NULL,
                migrated INTEGER NOT NULL DEFAULT 0 CHECK (migrated IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS open_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buy_transaction_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                quantity_remaining REAL NOT NULL CHECK (quantity_remaining > 0),
                price_per_coin REAL NOT NULL CHECK (price_per_coin > 0),
                cost_basis_remaining_usd REAL NOT NULL CHECK (cost_basis_remaining_usd >= 0),
                acquired_at_utc TEXT NOT NULL,
                pnl_zone TEXT NOT NULL DEFAULT 'NEUTRAL',
                FOREIGN KEY (buy_transaction_id) REFERENCES transactions(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS position_state (
                chat_id TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                high_water_price REAL NOT NULL CHECK (high_water_price > 0),
                trailing_stop_price REAL,
                stop_state TEXT NOT NULL DEFAULT 'ABOVE_STOP',
                PRIMARY KEY (chat_id, coin_id)
            );

            CREATE TABLE IF NOT EXISTS signal_state (
                chat_id TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                candidate TEXT NOT NULL DEFAULT 'NONE',
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                last_seen_at_utc TEXT,
                PRIMARY KEY (chat_id, coin_id)
            );


            CREATE TABLE IF NOT EXISTS market_symbols (
                coin_id TEXT NOT NULL,
                exchange TEXT NOT NULL CHECK (exchange IN ('binance', 'kraken', 'coinbase')),
                symbol TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                PRIMARY KEY (coin_id, exchange),
                FOREIGN KEY (coin_id) REFERENCES coin_catalog(coin_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS market_candles (
                ts_utc TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                exchange TEXT NOT NULL,
                open REAL NOT NULL CHECK (open > 0),
                high REAL NOT NULL CHECK (high > 0),
                low REAL NOT NULL CHECK (low > 0),
                close REAL NOT NULL CHECK (close > 0),
                volume REAL NOT NULL DEFAULT 0 CHECK (volume >= 0),
                vwap REAL NOT NULL CHECK (vwap > 0),
                trades INTEGER NOT NULL DEFAULT 0 CHECK (trades >= 0),
                source_count INTEGER NOT NULL DEFAULT 1 CHECK (source_count >= 1),
                PRIMARY KEY (ts_utc, coin_id, exchange)
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_chat_time
                ON transactions(chat_id, timestamp_utc DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_transactions_chat_coin
                ON transactions(chat_id, coin_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_open_lots_chat_coin
                ON open_lots(chat_id, coin_id, acquired_at_utc DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_market_candles_coin_time
                ON market_candles(coin_id, exchange, ts_utc);
            CREATE INDEX IF NOT EXISTS idx_market_symbols_exchange
                ON market_symbols(exchange, symbol);
            '''
        )
        coin_columns = {row['name'] for row in conn.execute('PRAGMA table_info(coin_catalog)').fetchall()}
        if 'symbol' not in coin_columns:
            conn.execute('ALTER TABLE coin_catalog ADD COLUMN symbol TEXT')

        # Forward-compatible migration for wallet.db files created by rev4.
        position_columns = {row['name'] for row in conn.execute('PRAGMA table_info(position_state)').fetchall()}
        if 'stop_state' not in position_columns:
            conn.execute("ALTER TABLE position_state ADD COLUMN stop_state TEXT NOT NULL DEFAULT 'ABOVE_STOP'")

        # These tables belonged to the pre-OHLCV/JSON migration path and are no longer used.
        conn.execute('DROP TABLE IF EXISTS price_history')
        conn.execute('DROP TABLE IF EXISTS meta')


def get_tracked_coins():
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT c.coin_id, c.name
            FROM tracked_coins t
            JOIN coin_catalog c ON c.coin_id = t.coin_id
            ORDER BY c.name COLLATE NOCASE
            '''
        ).fetchall()
    return {row['coin_id']: row['name'] for row in rows}


def add_tracked_coin(coin_id, name, symbol=None):
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT INTO coin_catalog(coin_id, name, symbol) VALUES(?, ?, ?) '
            'ON CONFLICT(coin_id) DO UPDATE SET name=excluded.name, symbol=COALESCE(excluded.symbol, coin_catalog.symbol)',
            (coin_id, name, symbol),
        )
        cur = conn.execute('INSERT OR IGNORE INTO tracked_coins(coin_id) VALUES(?)', (coin_id,))
        conn.commit()
        return cur.rowcount > 0


def remove_tracked_coin(coin_input):
    coin_input = coin_input.lower()
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            '''
            SELECT c.coin_id, c.name
            FROM tracked_coins t
            JOIN coin_catalog c ON c.coin_id = t.coin_id
            WHERE lower(c.coin_id) = ? OR lower(c.name) = ?
            LIMIT 1
            ''',
            (coin_input, coin_input),
        ).fetchone()
        if not row:
            conn.commit()
            return ('not_found', None)

        open_count = conn.execute(
            'SELECT COUNT(*) FROM open_lots WHERE coin_id = ?',
            (row['coin_id'],),
        ).fetchone()[0]
        alert_count = conn.execute(
            'SELECT COUNT(*) FROM alerts WHERE coin_id = ? AND (above IS NOT NULL OR below IS NOT NULL)',
            (row['coin_id'],),
        ).fetchone()[0]

        if open_count:
            conn.commit()
            return ('held', row['name'])
        if alert_count:
            conn.commit()
            return ('alerted', row['name'])

        conn.execute('DELETE FROM tracked_coins WHERE coin_id = ?', (row['coin_id'],))
        conn.commit()
        return ('ok', row['name'])


def get_required_coin_ids():
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT coin_id FROM tracked_coins
            UNION
            SELECT DISTINCT coin_id FROM open_lots
            UNION
            SELECT DISTINCT coin_id FROM alerts WHERE above IS NOT NULL OR below IS NOT NULL
            '''
        ).fetchall()
    return [row['coin_id'] for row in rows]


def get_coin_names(coin_ids=None):
    with _connect() as conn:
        if coin_ids:
            placeholders = ','.join('?' for _ in coin_ids)
            rows = conn.execute(
                f'SELECT coin_id, name FROM coin_catalog WHERE coin_id IN ({placeholders})',
                tuple(coin_ids),
            ).fetchall()
        else:
            rows = conn.execute('SELECT coin_id, name FROM coin_catalog').fetchall()
    return {row['coin_id']: row['name'] for row in rows}


def set_price_alert(chat_id, coin_id, kind, value):
    if kind not in ('above', 'below'):
        raise ValueError('invalid alert kind')
    column = kind
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT INTO alerts(chat_id, coin_id) VALUES(?, ?) '
            'ON CONFLICT(chat_id, coin_id) DO NOTHING',
            (chat_id, coin_id),
        )
        conn.execute(
            f'UPDATE alerts SET {column} = ? WHERE chat_id = ? AND coin_id = ?',
            (value, chat_id, coin_id),
        )
        conn.commit()


def consume_price_alerts(prices):
    events = []
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            'SELECT chat_id, coin_id, above, below FROM alerts WHERE above IS NOT NULL OR below IS NOT NULL'
        ).fetchall()
        for row in rows:
            price = prices.get(row['coin_id'])
            if price is None:
                continue
            above = row['above']
            below = row['below']
            if above is not None and price > above:
                events.append({
                    'chat_id': row['chat_id'], 'coin_id': row['coin_id'],
                    'kind': 'above', 'threshold': above, 'price': price,
                })
                above = None
            if below is not None and price < below:
                events.append({
                    'chat_id': row['chat_id'], 'coin_id': row['coin_id'],
                    'kind': 'below', 'threshold': below, 'price': price,
                })
                below = None
            conn.execute(
                'UPDATE alerts SET above = ?, below = ? WHERE chat_id = ? AND coin_id = ?',
                (above, below, row['chat_id'], row['coin_id']),
            )
        conn.commit()
    return events


def claim_signal_notification(chat_id, coin_id, now_utc, cooldown_until_utc):
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT signal_next_at_utc FROM alerts WHERE chat_id = ? AND coin_id = ?',
            (chat_id, coin_id),
        ).fetchone()
        if row and row['signal_next_at_utc'] and row['signal_next_at_utc'] > now_utc:
            conn.commit()
            return False
        conn.execute(
            '''
            INSERT INTO alerts(chat_id, coin_id, signal_next_at_utc)
            VALUES(?, ?, ?)
            ON CONFLICT(chat_id, coin_id) DO UPDATE SET signal_next_at_utc=excluded.signal_next_at_utc
            ''',
            (chat_id, coin_id, cooldown_until_utc),
        )
        conn.commit()
        return True


def record_buy(chat_id, coin_id, amount_usd, price_per_coin, timestamp_utc, fee_usd=0.0):
    if amount_usd <= 0 or price_per_coin <= 0 or fee_usd < 0:
        raise ValueError('invalid buy values')

    quantity = amount_usd / price_per_coin
    cost_basis = amount_usd + fee_usd
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.execute(
            '''
            INSERT INTO transactions(
                chat_id, coin_id, side, quantity, price_per_coin,
                gross_usd, cost_basis_usd, realized_pnl_usd,
                fee_usd, timestamp_utc, migrated
            ) VALUES(?, ?, 'BUY', ?, ?, ?, ?, 0, ?, ?, 0)
            ''',
            (chat_id, coin_id, quantity, price_per_coin, amount_usd, cost_basis, fee_usd, timestamp_utc),
        )
        conn.execute(
            '''
            INSERT INTO open_lots(
                buy_transaction_id, chat_id, coin_id, quantity_remaining,
                price_per_coin, cost_basis_remaining_usd, acquired_at_utc, pnl_zone
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'NEUTRAL')
            ''',
            (cur.lastrowid, chat_id, coin_id, quantity, price_per_coin, cost_basis, timestamp_utc),
        )
        conn.execute(
            '''
            INSERT INTO position_state(chat_id, coin_id, high_water_price, trailing_stop_price)
            VALUES(?, ?, ?, NULL)
            ON CONFLICT(chat_id, coin_id) DO UPDATE SET
                high_water_price = MAX(position_state.high_water_price, excluded.high_water_price)
            ''',
            (chat_id, coin_id, price_per_coin),
        )
        conn.commit()
        return {
            'transaction_id': cur.lastrowid,
            'quantity': quantity,
            'cost_basis_usd': cost_basis,
            'fee_usd': fee_usd,
        }

def record_sell_lifo(chat_id, coin_id, requested_quantity, price_per_coin, timestamp_utc, fee_usd=0.0):
    if price_per_coin <= 0 or fee_usd < 0:
        raise ValueError('invalid sell values')

    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        lots = conn.execute(
            '''
            SELECT id, quantity_remaining, price_per_coin, cost_basis_remaining_usd
            FROM open_lots
            WHERE chat_id = ? AND coin_id = ?
            ORDER BY acquired_at_utc DESC, id DESC
            ''',
            (chat_id, coin_id),
        ).fetchall()
        if not lots:
            conn.commit()
            return {'status': 'empty', 'available': 0.0}

        total_available = sum(row['quantity_remaining'] for row in lots)
        sell_quantity = total_available if requested_quantity is None else requested_quantity
        if sell_quantity <= 0:
            conn.commit()
            return {'status': 'empty', 'available': total_available}
        if sell_quantity > total_available + 1e-12:
            conn.commit()
            return {'status': 'insufficient', 'requested': sell_quantity, 'available': total_available}

        remaining = sell_quantity
        cost_basis = 0.0
        for lot in lots:
            if remaining <= 1e-12:
                break
            lot_qty = lot['quantity_remaining']
            take = min(lot_qty, remaining)
            unit_cost = lot['cost_basis_remaining_usd'] / lot_qty if lot_qty > 0 else lot['price_per_coin']
            taken_cost = take * unit_cost
            cost_basis += taken_cost
            new_qty = lot_qty - take
            new_cost = lot['cost_basis_remaining_usd'] - taken_cost
            if new_qty <= 1e-12:
                conn.execute('DELETE FROM open_lots WHERE id = ?', (lot['id'],))
            else:
                conn.execute(
                    'UPDATE open_lots SET quantity_remaining = ?, cost_basis_remaining_usd = ? WHERE id = ?',
                    (new_qty, max(0.0, new_cost), lot['id']),
                )
            remaining -= take

        gross = sell_quantity * price_per_coin
        net = gross - fee_usd
        realized_pnl = net - cost_basis
        cur = conn.execute(
            '''
            INSERT INTO transactions(
                chat_id, coin_id, side, quantity, price_per_coin,
                gross_usd, cost_basis_usd, realized_pnl_usd,
                fee_usd, timestamp_utc, migrated
            ) VALUES(?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, 0)
            ''',
            (
                chat_id, coin_id, sell_quantity, price_per_coin, gross,
                cost_basis, realized_pnl, fee_usd, timestamp_utc,
            ),
        )

        still_open = conn.execute(
            'SELECT 1 FROM open_lots WHERE chat_id = ? AND coin_id = ? LIMIT 1',
            (chat_id, coin_id),
        ).fetchone()
        if not still_open:
            conn.execute(
                'DELETE FROM position_state WHERE chat_id = ? AND coin_id = ?',
                (chat_id, coin_id),
            )
            conn.execute(
                'DELETE FROM signal_state WHERE chat_id = ? AND coin_id = ?',
                (chat_id, coin_id),
            )

        conn.commit()
        return {
            'status': 'ok',
            'transaction_id': cur.lastrowid,
            'quantity': sell_quantity,
            'available_before': total_available,
            'gross_usd': gross,
            'fee_usd': fee_usd,
            'net_usd': net,
            'cost_basis_usd': cost_basis,
            'realized_pnl_usd': realized_pnl,
        }

def get_position_keys():
    with _connect() as conn:
        rows = conn.execute(
            'SELECT DISTINCT chat_id, coin_id FROM open_lots ORDER BY chat_id, coin_id'
        ).fetchall()
    return [(row['chat_id'], row['coin_id']) for row in rows]


def update_lot_pnl_zones(prices):
    """Update per-lot PROFIT/NEUTRAL/LOSS state and return alert-worthy transitions.

    Direct PROFIT -> LOSS and LOSS -> PROFIT transitions are intentionally allowed;
    they do not require the price to pass through a sampled NEUTRAL state first.
    P/L uses the remaining cost basis, so BUY fees are included.
    """
    events = []
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            '''
            SELECT id, chat_id, coin_id, quantity_remaining, price_per_coin,
                   cost_basis_remaining_usd, acquired_at_utc, pnl_zone
            FROM open_lots
            ORDER BY id
            '''
        ).fetchall()
        for row in rows:
            current_price = prices.get(row['coin_id'])
            quantity = row['quantity_remaining']
            if current_price is None or quantity <= 0:
                continue

            effective_cost = row['cost_basis_remaining_usd'] / quantity
            if effective_cost <= 0:
                continue
            pnl_pct = (current_price / effective_cost - 1.0) * 100.0
            old_zone = row['pnl_zone']

            if pnl_pct >= 5.0:
                new_zone = 'PROFIT'
            elif pnl_pct <= -5.0:
                new_zone = 'LOSS'
            elif -4.0 < pnl_pct < 4.0:
                new_zone = 'NEUTRAL'
            else:
                new_zone = old_zone

            if old_zone == 'MIGRATED_TRIGGERED':
                conn.execute('UPDATE open_lots SET pnl_zone = ? WHERE id = ?', (new_zone, row['id']))
                continue

            if new_zone != old_zone:
                conn.execute('UPDATE open_lots SET pnl_zone = ? WHERE id = ?', (new_zone, row['id']))
                if new_zone in ('PROFIT', 'LOSS'):
                    events.append({
                        'chat_id': row['chat_id'],
                        'coin_id': row['coin_id'],
                        'bought_at': row['price_per_coin'],
                        'effective_cost_per_coin': effective_cost,
                        'bought_on': row['acquired_at_utc'],
                        'current_price': current_price,
                        'pnl_pct': pnl_pct,
                        'old_zone': old_zone,
                        'zone': new_zone,
                    })
        conn.commit()
    return events


def update_position_trailing_stops(prices, atr_pct_by_coin, multiplier=2.0):
    """Update long-position ATR trailing stops without ever moving them downward."""
    result = {}
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            '''
            SELECT chat_id, coin_id,
                   SUM(quantity_remaining) AS quantity,
                   SUM(cost_basis_remaining_usd) AS cost_basis
            FROM open_lots
            GROUP BY chat_id, coin_id
            '''
        ).fetchall()

        active = {(row['chat_id'], row['coin_id']) for row in rows}
        for row in rows:
            key = (row['chat_id'], row['coin_id'])
            current_price = prices.get(row['coin_id'])
            atr_pct = atr_pct_by_coin.get(row['coin_id'])
            if current_price is None or current_price <= 0 or atr_pct is None:
                continue
            try:
                atr_pct = float(atr_pct)
            except (TypeError, ValueError):
                continue
            if not (atr_pct >= 0.0 and atr_pct < float('inf')):
                continue

            quantity = row['quantity'] or 0.0
            cost_basis = row['cost_basis'] or 0.0
            avg_cost = cost_basis / quantity if quantity > 0 else current_price
            state = conn.execute(
                'SELECT high_water_price, trailing_stop_price FROM position_state '
                'WHERE chat_id = ? AND coin_id = ?',
                key,
            ).fetchone()

            previous_high = state['high_water_price'] if state else avg_cost
            high_water = max(previous_high, avg_cost, current_price)
            candidate_stop = max(0.0, high_water * (1.0 - multiplier * atr_pct / 100.0))
            previous_stop = state['trailing_stop_price'] if state else None
            trailing_stop = max(previous_stop, candidate_stop) if previous_stop is not None else candidate_stop

            conn.execute(
                '''
                INSERT INTO position_state(chat_id, coin_id, high_water_price, trailing_stop_price)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id, coin_id) DO UPDATE SET
                    high_water_price=excluded.high_water_price,
                    trailing_stop_price=excluded.trailing_stop_price
                ''',
                (row['chat_id'], row['coin_id'], high_water, trailing_stop),
            )
            result[key] = {
                'high_water_price': high_water,
                'trailing_stop_price': trailing_stop,
                'atr_pct': atr_pct,
                'avg_cost_per_coin': avg_cost,
            }

        state_rows = conn.execute('SELECT chat_id, coin_id FROM position_state').fetchall()
        for state in state_rows:
            if (state['chat_id'], state['coin_id']) not in active:
                conn.execute(
                    'DELETE FROM position_state WHERE chat_id = ? AND coin_id = ?',
                    (state['chat_id'], state['coin_id']),
                )

        conn.commit()
    return result


def update_signal_confirmation(chat_id, coin_id, candidate, timestamp_utc, required=3):
    """Persist consecutive signal observations and return the confirmation state."""
    required = max(1, int(required))
    candidate = candidate or 'NONE'
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT candidate, confirmation_count FROM signal_state WHERE chat_id = ? AND coin_id = ?',
            (chat_id, coin_id),
        ).fetchone()
        if candidate == 'NONE':
            count = 0
        elif row and row['candidate'] == candidate:
            count = min(required, int(row['confirmation_count']) + 1)
        else:
            count = 1
        conn.execute(
            '''
            INSERT INTO signal_state(chat_id, coin_id, candidate, confirmation_count, last_seen_at_utc)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, coin_id) DO UPDATE SET
                candidate=excluded.candidate,
                confirmation_count=excluded.confirmation_count,
                last_seen_at_utc=excluded.last_seen_at_utc
            ''',
            (chat_id, coin_id, candidate, count, timestamp_utc),
        )
        conn.commit()
    return {
        'candidate': candidate,
        'count': count,
        'required': required,
        'confirmed': candidate != 'NONE' and count >= required,
    }


def consume_trailing_stop_events(prices):
    """Return a one-shot event when a long position first crosses its trailing stop."""
    events = []
    with _connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute(
            '''
            SELECT s.chat_id, s.coin_id, s.high_water_price, s.trailing_stop_price,
                   s.stop_state,
                   SUM(l.quantity_remaining) AS quantity,
                   SUM(l.cost_basis_remaining_usd) AS cost_basis
            FROM position_state s
            JOIN open_lots l ON l.chat_id = s.chat_id AND l.coin_id = s.coin_id
            WHERE s.trailing_stop_price IS NOT NULL
            GROUP BY s.chat_id, s.coin_id, s.high_water_price, s.trailing_stop_price, s.stop_state
            '''
        ).fetchall()
        for row in rows:
            price = prices.get(row['coin_id'])
            if price is None or price <= 0:
                continue
            state = row['stop_state'] or 'ABOVE_STOP'
            if price <= row['trailing_stop_price'] and state != 'STOP_TRIGGERED':
                conn.execute(
                    'UPDATE position_state SET stop_state = ? WHERE chat_id = ? AND coin_id = ?',
                    ('STOP_TRIGGERED', row['chat_id'], row['coin_id']),
                )
                qty = row['quantity'] or 0.0
                cost = row['cost_basis'] or 0.0
                avg_cost = cost / qty if qty > 0 else 0.0
                pnl_pct = ((price * qty - cost) / cost * 100.0) if cost > 0 else 0.0
                events.append({
                    'chat_id': row['chat_id'],
                    'coin_id': row['coin_id'],
                    'current_price': float(price),
                    'trailing_stop_price': float(row['trailing_stop_price']),
                    'high_water_price': float(row['high_water_price']),
                    'quantity': float(qty),
                    'avg_cost_per_coin': float(avg_cost),
                    'pnl_pct': float(pnl_pct),
                })
        conn.commit()
    return events


def get_position_contexts():
    """Return current open-position accounting context keyed by (chat_id, coin_id)."""
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT chat_id, coin_id,
                   SUM(quantity_remaining) AS quantity,
                   SUM(cost_basis_remaining_usd) AS cost_basis_usd
            FROM open_lots
            GROUP BY chat_id, coin_id
            '''
        ).fetchall()
    return {
        (row['chat_id'], row['coin_id']): {
            'quantity': float(row['quantity'] or 0.0),
            'cost_basis_usd': float(row['cost_basis_usd'] or 0.0),
        }
        for row in rows
    }

def set_coin_symbol(coin_id, symbol):
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return
    with _connect() as conn:
        conn.execute('UPDATE coin_catalog SET symbol = ? WHERE coin_id = ?', (symbol, coin_id))


def set_market_symbol(coin_id, exchange, symbol, enabled=True):
    exchange = exchange.strip().lower()
    symbol = (symbol or '').strip().upper()
    if exchange not in ('binance', 'kraken', 'coinbase'):
        raise ValueError('unsupported exchange')
    if not symbol:
        raise ValueError('empty market symbol')
    with _connect() as conn:
        conn.execute(
            '''
            INSERT INTO market_symbols(coin_id, exchange, symbol, enabled)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(coin_id, exchange) DO UPDATE SET
                symbol=excluded.symbol, enabled=excluded.enabled
            ''',
            (coin_id, exchange, symbol, 1 if enabled else 0),
        )


def ensure_default_market_symbols(coin_id, symbol):
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return
    defaults = {
        'binance': f'{symbol}USDT',
        'kraken': f'{symbol}/USD',
        'coinbase': f'{symbol}-USD',
    }
    for exchange, market_symbol in defaults.items():
        set_market_symbol(coin_id, exchange, market_symbol, enabled=True)


def disable_market_symbol(exchange, symbol):
    exchange = exchange.strip().lower()
    symbol = (symbol or '').strip().upper()
    if not symbol:
        return None
    with _connect() as conn:
        row = conn.execute(
            'SELECT coin_id FROM market_symbols WHERE exchange = ? AND upper(symbol) = ?',
            (exchange, symbol),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            'UPDATE market_symbols SET enabled = 0 WHERE exchange = ? AND upper(symbol) = ?',
            (exchange, symbol),
        )
        return row['coin_id']


def get_coin_catalog():
    with _connect() as conn:
        rows = conn.execute(
            'SELECT coin_id, name, symbol FROM coin_catalog ORDER BY name COLLATE NOCASE'
        ).fetchall()
    return [dict(row) for row in rows]


def get_market_symbols(exchange=None, required_only=False):
    with _connect() as conn:
        sql = '''
            SELECT m.coin_id, m.exchange, m.symbol, m.enabled
            FROM market_symbols m
        '''
        params = []
        where = []
        if exchange is not None:
            where.append('m.exchange = ?')
            params.append(exchange)
        if required_only:
            where.append(
                "m.coin_id IN ("
                "SELECT coin_id FROM tracked_coins "
                "UNION SELECT DISTINCT coin_id FROM open_lots "
                "UNION SELECT DISTINCT coin_id FROM alerts WHERE above IS NOT NULL OR below IS NOT NULL)"
            )
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY m.exchange, m.coin_id'
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def upsert_market_candle(candle):
    with _connect() as conn:
        conn.execute(
            '''
            INSERT INTO market_candles(
                ts_utc, coin_id, exchange, open, high, low, close,
                volume, vwap, trades, source_count
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_utc, coin_id, exchange) DO UPDATE SET
                open=excluded.open,
                high=MAX(market_candles.high, excluded.high),
                low=MIN(market_candles.low, excluded.low),
                close=excluded.close,
                volume=excluded.volume,
                vwap=excluded.vwap,
                trades=excluded.trades,
                source_count=excluded.source_count
            ''',
            (
                candle['ts_utc'], candle['coin_id'], candle['exchange'],
                float(candle['open']), float(candle['high']), float(candle['low']),
                float(candle['close']), float(candle.get('volume', 0.0)),
                float(candle.get('vwap', candle['close'])), int(candle.get('trades', 0)),
                int(candle.get('source_count', 1)),
            ),
        )


def rebuild_aggregate_candle(coin_id, ts_utc):
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT open, high, low, close, volume, vwap, trades
            FROM market_candles
            WHERE coin_id = ? AND ts_utc = ? AND exchange != 'aggregate'
            ''',
            (coin_id, ts_utc),
        ).fetchall()
        if not rows:
            return None
        opens = [float(row['open']) for row in rows]
        highs = [float(row['high']) for row in rows]
        lows = [float(row['low']) for row in rows]
        closes = [float(row['close']) for row in rows]
        volumes = [float(row['volume'] or 0.0) for row in rows]
        total_volume = sum(volumes)
        if total_volume > 0:
            vwap = sum(float(row['vwap']) * volume for row, volume in zip(rows, volumes)) / total_volume
        else:
            vwap = statistics.median(float(row['vwap']) for row in rows)
        candle = {
            'ts_utc': ts_utc,
            'coin_id': coin_id,
            'exchange': 'aggregate',
            'open': statistics.median(opens),
            'high': statistics.median(highs),
            'low': statistics.median(lows),
            'close': statistics.median(closes),
            'volume': total_volume,
            'vwap': float(vwap),
            'trades': sum(int(row['trades'] or 0) for row in rows),
            'source_count': len(rows),
        }
        conn.execute(
            '''
            INSERT INTO market_candles(
                ts_utc, coin_id, exchange, open, high, low, close,
                volume, vwap, trades, source_count
            ) VALUES(?, ?, 'aggregate', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_utc, coin_id, exchange) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, vwap=excluded.vwap,
                trades=excluded.trades, source_count=excluded.source_count
            ''',
            (
                ts_utc, coin_id, candle['open'], candle['high'], candle['low'], candle['close'],
                candle['volume'], candle['vwap'], candle['trades'], candle['source_count'],
            ),
        )
        return candle


def get_market_candles(coin_ids, since_utc, exchange='aggregate'):
    if not coin_ids:
        return {}
    placeholders = ','.join('?' for _ in coin_ids)
    with _connect() as conn:
        rows = conn.execute(
            f'''
            SELECT ts_utc, coin_id, open, high, low, close, volume, vwap, trades, source_count
            FROM market_candles
            WHERE coin_id IN ({placeholders}) AND exchange = ? AND ts_utc >= ?
            ORDER BY ts_utc ASC
            ''',
            tuple(coin_ids) + (exchange, since_utc),
        ).fetchall()
    result = {coin_id: [] for coin_id in coin_ids}
    for row in rows:
        result.setdefault(row['coin_id'], []).append({
            'ts': row['ts_utc'],
            'open': row['open'],
            'high': row['high'],
            'low': row['low'],
            'close': row['close'],
            'volume': row['volume'],
            'vwap': row['vwap'],
            'trades': row['trades'],
            'source_count': row['source_count'],
        })
    return result


def prune_market_history(days=30):
    cutoff = _iso_utc(_utc_now() - datetime.timedelta(days=int(days)))
    with _connect() as conn:
        conn.execute('DELETE FROM market_candles WHERE ts_utc < ?', (cutoff,))


def get_positions(chat_id):
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT l.coin_id,
                   COALESCE(c.name, l.coin_id) AS coin_name,
                   SUM(l.quantity_remaining) AS quantity,
                   SUM(l.cost_basis_remaining_usd) AS cost_basis_usd,
                   s.high_water_price,
                   s.trailing_stop_price,
                   s.stop_state
            FROM open_lots l
            LEFT JOIN coin_catalog c ON c.coin_id = l.coin_id
            LEFT JOIN position_state s ON s.chat_id = l.chat_id AND s.coin_id = l.coin_id
            WHERE l.chat_id = ?
            GROUP BY l.coin_id, c.name, s.high_water_price, s.trailing_stop_price, s.stop_state
            ORDER BY coin_name COLLATE NOCASE
            ''',
            (chat_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_realized_pnl(chat_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS pnl FROM transactions WHERE chat_id = ? AND side = 'SELL'",
            (chat_id,),
        ).fetchone()
    return float(row['pnl'])


def get_transactions(chat_id, limit=30):
    limit = max(1, min(int(limit), 100))
    with _connect() as conn:
        rows = conn.execute(
            '''
            SELECT t.id, t.coin_id, COALESCE(c.name, t.coin_id) AS coin_name,
                   t.side, t.quantity, t.price_per_coin, t.gross_usd,
                   t.cost_basis_usd, t.realized_pnl_usd, t.fee_usd,
                   t.timestamp_utc, t.migrated
            FROM transactions t
            LEFT JOIN coin_catalog c ON c.coin_id = t.coin_id
            WHERE t.chat_id = ?
            ORDER BY t.timestamp_utc DESC, t.id DESC
            LIMIT ?
            ''',
            (chat_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]

