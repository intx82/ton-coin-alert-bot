"""OHLCV market analysis used by the Telegram bot."""

__version__ = 'rev14'

import numpy as np
import pandas as pd


def convert_candles(raw) -> pd.DataFrame:
    """Normalize 1-minute OHLCV candle rows from SQLite."""
    df = pd.json_normalize(raw)
    required = ['ts', 'open', 'high', 'low', 'close']
    missing = [name for name in required if name not in df.columns]
    if missing:
        raise ValueError(f'Missing OHLC fields: {missing}')

    df['ts'] = pd.to_datetime(df['ts'], errors='coerce', utc=True)
    for name in ('open', 'high', 'low', 'close', 'volume', 'vwap'):
        if name not in df.columns:
            df[name] = 0.0 if name == 'volume' else np.nan
        df[name] = pd.to_numeric(df[name], errors='coerce')

    df['price'] = df['close']
    df.sort_values('ts', inplace=True)
    df.dropna(subset=['ts', 'open', 'high', 'low', 'close'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def calc_candle_vwap(df: pd.DataFrame) -> pd.Series:
    """Return volume-weighted price across real OHLCV candles."""
    if df.empty:
        return pd.Series(dtype=float, index=df.index)

    volume = pd.to_numeric(df['volume'], errors='coerce').fillna(0.0)
    price = pd.to_numeric(df['vwap'], errors='coerce')
    valid = (volume > 0) & price.notna()

    if valid.any() and float(volume[valid].sum()) > 0:
        value = float((price[valid] * volume[valid]).sum() / volume[valid].sum())
    else:
        value = float(df['close'].mean())
    return pd.Series([value] * len(df), index=df.index, name='vwap_reference')


def calc_atr_pct_from_ohlcv(df: pd.DataFrame, period: int = 14, freq: str = '1h') -> pd.Series:
    """Compute ATR% from real candle high/low/close data."""
    if df.empty:
        return pd.Series(dtype=float, index=df.index, name='atr')

    indexed = df.set_index('ts')
    ohlc = indexed.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
    }).dropna()
    if ohlc.empty:
        return pd.Series([np.nan] * len(df), index=df.index, name='atr')

    prev_close = ohlc['close'].shift(1)
    tr = pd.concat([
        ohlc['high'] - ohlc['low'],
        (ohlc['high'] - prev_close).abs(),
        (ohlc['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False, min_periods=period).mean()
    atr_pct = (atr / ohlc['close']) * 100.0
    filled = atr_pct.reindex(indexed.index, method='ffill')
    filled.name = 'atr'
    return filled.reset_index(drop=True)


def calc_theil_sen_slope(df: pd.DataFrame) -> float:
    """Return robust Theil-Sen price slope in price units per second."""
    t = (df['ts'] - df['ts'].iloc[0]).dt.total_seconds().to_numpy(dtype=float)
    y = df['price'].to_numpy(dtype=float)
    n = len(t)
    if n < 2:
        return 0.0

    slope_chunks = []
    for i in range(n - 1):
        dt = t[i + 1:] - t[i]
        valid = dt != 0
        if np.any(valid):
            slope_chunks.append((y[i + 1:][valid] - y[i]) / dt[valid])

    if not slope_chunks:
        return 0.0
    return float(np.median(np.concatenate(slope_chunks)))


def _window(df: pd.DataFrame, hours: float) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    cutoff = df['ts'].iloc[-1] - pd.Timedelta(hours=hours)
    return df[df['ts'] >= cutoff].copy().reset_index(drop=True)


def calc_efficiency_ratio(df: pd.DataFrame) -> float:
    """Kaufman-style efficiency ratio in [0, 1]."""
    if len(df) < 2:
        return 0.0
    prices = df['price'].to_numpy(dtype=float)
    path = float(np.abs(np.diff(prices)).sum())
    if path <= 0.0:
        return 0.0
    return float(min(1.0, max(0.0, abs(prices[-1] - prices[0]) / path)))


def calc_horizon_trends(df: pd.DataFrame, horizons=(1, 4, 24)) -> dict:
    """Return robust trend information for several trailing windows."""
    out = {}
    for hours in horizons:
        window = _window(df, hours)
        key = f'{hours}h'
        if len(window) < 3:
            out[key] = {
                'samples': len(window),
                'direction': 'UNKNOWN',
                'slope_pct_hour': 0.0,
                'slope_usd_hour': 0.0,
            }
            continue

        slope_sec = calc_theil_sen_slope(window)
        open_price = float(window['price'].iloc[0])
        slope_hour = slope_sec * 3600.0
        slope_pct_hour = slope_hour / open_price * 100.0 if open_price else 0.0
        if slope_pct_hour > 0.05:
            direction = 'UP'
        elif slope_pct_hour < -0.05:
            direction = 'DOWN'
        else:
            direction = 'FLAT'

        out[key] = {
            'samples': len(window),
            'direction': direction,
            'slope_pct_hour': float(slope_pct_hour),
            'slope_usd_hour': float(slope_hour),
        }
    return out


def analyze_data_quality(df: pd.DataFrame, now=None, expected_interval_seconds=60.0,
                         horizon_hours=24.0) -> dict:
    """Grade whether trailing history is complete and fresh enough for signals."""
    if df.empty:
        return {
            'state': 'BAD', 'samples': 0, 'coverage': 0.0,
            'max_gap_seconds': None, 'latest_age_seconds': None,
        }

    ts = pd.to_datetime(df['ts'], utc=True, errors='coerce').dropna().sort_values()
    if ts.empty:
        return {
            'state': 'BAD', 'samples': 0, 'coverage': 0.0,
            'max_gap_seconds': None, 'latest_age_seconds': None,
        }

    expected = max(1.0, horizon_hours * 3600.0 / expected_interval_seconds)
    coverage = min(1.0, len(ts) / expected)
    if len(ts) >= 2:
        gaps = ts.diff().dt.total_seconds().dropna()
        max_gap = float(gaps.max()) if not gaps.empty else 0.0
    else:
        max_gap = None

    now_ts = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize('UTC')
    else:
        now_ts = now_ts.tz_convert('UTC')
    latest_age = max(0.0, float((now_ts - ts.iloc[-1]).total_seconds()))

    if coverage >= 0.80 and (max_gap is None or max_gap <= 300.0) and latest_age <= 180.0:
        state = 'GOOD'
    elif coverage >= 0.50 and (max_gap is None or max_gap <= 1800.0) and latest_age <= 900.0:
        state = 'DEGRADED'
    else:
        state = 'BAD'

    return {
        'state': state,
        'samples': int(len(ts)),
        'coverage': float(coverage),
        'max_gap_seconds': max_gap,
        'latest_age_seconds': latest_age,
    }


def classify_market_regime(efficiency_ratio: float, slope_pct_hour: float,
                           atr_pct, data_quality_state='GOOD') -> dict:
    if data_quality_state == 'BAD':
        return {'structure': 'UNKNOWN', 'volatility': 'UNKNOWN', 'label': 'UNKNOWN'}

    if efficiency_ratio >= 0.55 and slope_pct_hour > 0.03:
        structure = 'TRENDING_UP'
    elif efficiency_ratio >= 0.55 and slope_pct_hour < -0.03:
        structure = 'TRENDING_DOWN'
    elif efficiency_ratio <= 0.35:
        structure = 'RANGING'
    else:
        structure = 'MIXED'

    if atr_pct is None or not np.isfinite(atr_pct):
        volatility = 'UNKNOWN'
    elif atr_pct >= 1.5:
        volatility = 'VOLATILE'
    elif atr_pct <= 0.25:
        volatility = 'QUIET'
    else:
        volatility = 'NORMAL'

    return {
        'structure': structure,
        'volatility': volatility,
        'label': f'{structure}/{volatility}',
    }


def detect_signals(df: pd.DataFrame, horizons=None, data_quality=None) -> dict:
    """Build directional, volatility-normalized market signals from OHLCV data."""
    last_price = float(df['price'].iloc[-1])
    reference_price = float(df['vwap_reference'].iloc[-1])
    atr_value = float(df['atr'].iloc[-1])
    atr_pct = atr_value if np.isfinite(atr_value) else None

    horizons = horizons or calc_horizon_trends(df)
    quality = data_quality or {'state': 'GOOD'}
    slope_24_pct = float(horizons.get('24h', {}).get('slope_pct_hour', 0.0))
    efficiency_ratio = calc_efficiency_ratio(df)
    regime = classify_market_regime(
        efficiency_ratio, slope_24_pct, atr_pct, quality.get('state', 'GOOD')
    )

    deviation_pct = 100.0 * (last_price - reference_price) / reference_price
    if atr_pct is None or atr_pct <= 1e-12:
        deviation_atr = None
        candidate = 'NONE'
    else:
        deviation_atr = deviation_pct / atr_pct
        if deviation_atr <= -1.5:
            candidate = 'BUY'
        elif deviation_atr >= 1.5:
            candidate = 'SELL'
        else:
            candidate = 'NONE'

    slope_1_pct = float(horizons.get('1h', {}).get('slope_pct_hour', 0.0))
    if slope_1_pct > 0.1:
        momentum = 'UP'
    elif slope_1_pct < -0.1:
        momentum = 'DOWN'
    else:
        momentum = 'FLAT'

    momentum_blocked = (
        (candidate == 'BUY' and momentum == 'DOWN') or
        (candidate == 'SELL' and momentum == 'UP')
    )
    structure = regime['structure']
    regime_blocked = (
        (candidate == 'BUY' and structure == 'TRENDING_DOWN') or
        (candidate == 'SELL' and structure == 'TRENDING_UP')
    )

    if atr_pct is None:
        signal_filter = 'ATR_UNAVAILABLE'
        mean_reversion = 'NONE'
    elif candidate == 'NONE':
        signal_filter = 'N/A'
        mean_reversion = 'NONE'
    elif quality.get('state') == 'BAD':
        signal_filter = 'DATA_QUALITY_BLOCKED'
        mean_reversion = 'NONE'
    elif momentum_blocked:
        signal_filter = 'MOMENTUM_BLOCKED'
        mean_reversion = 'NONE'
    elif regime_blocked:
        signal_filter = 'REGIME_BLOCKED'
        mean_reversion = 'NONE'
    else:
        signal_filter = 'PASS'
        mean_reversion = candidate

    slope_4_pct = float(horizons.get('4h', {}).get('slope_pct_hour', 0.0))
    acceleration = slope_1_pct - slope_4_pct
    if acceleration > 0.05:
        acceleration_state = 'ACCELERATING_UP'
    elif acceleration < -0.05:
        acceleration_state = 'ACCELERATING_DOWN'
    else:
        acceleration_state = 'STABLE'

    directions = [horizons.get(k, {}).get('direction', 'UNKNOWN') for k in ('1h', '4h', '24h')]
    if quality.get('state') == 'BAD':
        assessment = 'INSUFFICIENT_DATA'
    elif mean_reversion == 'BUY' and horizons.get('4h', {}).get('direction') == 'UP' and horizons.get('24h', {}).get('direction') == 'UP':
        assessment = 'BUY_PULLBACK'
    elif mean_reversion == 'SELL' and horizons.get('4h', {}).get('direction') == 'DOWN' and horizons.get('24h', {}).get('direction') == 'DOWN':
        assessment = 'SELL_RALLY'
    elif mean_reversion == 'BUY':
        assessment = 'MEAN_REVERSION_BUY'
    elif mean_reversion == 'SELL':
        assessment = 'MEAN_REVERSION_SELL'
    elif directions == ['UP', 'UP', 'UP']:
        assessment = 'TREND_UP'
    elif directions == ['DOWN', 'DOWN', 'DOWN']:
        assessment = 'TREND_DOWN'
    else:
        assessment = 'NEUTRAL'

    if atr_pct is not None:
        raw_size = 10.0 - atr_pct * 4.5
        position_size_multiplier = round(max(1.0, min(10.0, raw_size)), 2)
    else:
        position_size_multiplier = None

    return {
        'reference_name': 'VWAP',
        'reference_price': reference_price,
        'reference_deviation_pct': float(deviation_pct),
        'reference_deviation_atr': float(deviation_atr) if deviation_atr is not None else None,
        'mean_reversion_candidate': candidate,
        'momentum': momentum,
        'momentum_pct_hour': slope_1_pct,
        'signal_filter': signal_filter,
        'mean_reversion': mean_reversion,
        'efficiency_ratio': float(efficiency_ratio),
        'market_regime': regime,
        'horizons': horizons,
        'slope_acceleration_pct_hour': float(acceleration),
        'slope_acceleration': acceleration_state,
        'assessment': assessment,
        'data_quality': quality,
        'atr_pct': atr_pct,
        'position_size_multiplier': position_size_multiplier,
    }
