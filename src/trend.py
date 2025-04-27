#!/usr/bin/env python3
"""
btc_trend.py ― extended daily analytics + Theil-Sen slope + signals
-------------------------------------------------------------------
Key points:
1. We keep everything (price, slope, VWAP, signals) at the raw 5-min resolution.
2. For ATR specifically, we first roll up 5-min data into 1-hour bars, compute
   real high/low-based ATR(14), then forward-fill back onto the 5-min timestamps.
3. This ensures ATR actually reflects intrabar range rather than being 0.

Usage:
  ./btc_trend.py -f data_5min.json -o out.png
  ./btc_trend.py --summary-json < data_5min.json
"""
import sys, json, argparse, pathlib, textwrap
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

###############################################################################
# Helper functions
###############################################################################

def load_json(handle) -> dict:
    """
    Reads raw JSON → normalizes columns to at least:
        df["ts"] = Timestamps
        df["price"] = 'close' or 'Coin' or 'price' or something
        df["volume"] (optional, if provided)
    Sorts by ts ascending.
    """
    raw = json.load(handle)
    if not isinstance(raw, list) or len(raw) == 0:
        raise ValueError("Expected a non-empty JSON array.")
    return raw

def convert_json(raw) -> pd.DataFrame:
    df = pd.json_normalize(raw)
    # Try columns in fallback order
    for candidate in ["close","price"]:
        if candidate in df.columns:
            df["price"] = df[candidate]
            break
    if "price" not in df.columns:
        raise ValueError("No 'price', 'close' column found in JSON.")

    # volume optional
    if "volume" not in df.columns:
        df["volume"] = np.nan

    if "ts" not in df.columns:
        raise ValueError("No 'ts' column found in JSON.")
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def calc_theil_sen(df: pd.DataFrame) -> tuple[float, float]:
    """
    Returns (slope, intercept) using Theil-Sen (median of pairwise slopes).
      slope (float): price units / second
      intercept (float): median of (y_i - slope * t_i)
    O(N^2) naive approach, fine for a few hundred points.
    """
    t = (df["ts"] - df["ts"].iloc[0]).dt.total_seconds().values
    y = df["price"].values
    n = len(t)
    if n < 2:
        return (0.0, float(y[0]) if n==1 else 0.0)

    slopes = []
    for i in range(n-1):
        for j in range(i+1,n):
            dt = t[j] - t[i]
            if dt != 0:
                slopes.append( (y[j] - y[i]) / dt )

    slope = np.median(slopes)
    intercepts = y - slope * t
    intercept = np.median(intercepts)
    return slope, intercept


def calc_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Volume-weighted average price over entire dataset.
    If volume not provided, fallback to equal weighting => simple mean price.
    Returns a single scalar repeated for each row.
    """
    valid_mask = ~df["volume"].isna() & (df["volume"]>0)
    if valid_mask.sum() < 2:
        # fallback
        avgp = df["price"].mean()
        return pd.Series([avgp]*len(df), index=df.index)

    v_sum = df.loc[valid_mask, "volume"].sum()
    pv_sum = (df["price"]*df["volume"]).loc[valid_mask].sum()
    vwap_val = pv_sum / v_sum
    return pd.Series([vwap_val]*len(df), index=df.index)


def calc_atr_pct_with_resample(df: pd.DataFrame, period: int = 14, freq: str = "2H") -> pd.Series:
    """
    Computes ATR(%) using OHLC bars resampled from raw price data.
    Returns a Series of ATR% values (normalized by last close).
    Each 5-min timestamp is forward-filled with the latest ATR%.
    """
    df = df.set_index("ts")

    # Create OHLC bars
    ohlc = df["price"].resample(freq).agg(["first", "max", "min", "last"]).dropna()
    ohlc.columns = ["open", "high", "low", "close"]

    # True Range
    prev_close = ohlc["close"].shift(1).bfill() #fillna(method="bfill")
    tr1 = ohlc["high"] - ohlc["low"]
    tr2 = (ohlc["high"] - prev_close).abs()
    tr3 = (ohlc["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR in absolute units
    atr = tr.ewm(span=period, adjust=False, min_periods=period).mean()

    # Normalize to ATR%
    atr_pct = (atr / ohlc["close"]) * 100

    # Forward-fill to raw 5-min timestamps
    atr_pct_filled = atr_pct.reindex(df.index, method="ffill")

    # Reset index for consistency
    atr_pct_filled.name = "atr"
    return atr_pct_filled.reset_index(drop=True)



###############################################################################
# Example signals
###############################################################################
def detect_signals(df: pd.DataFrame, slope: float, intercept: float) -> dict:
    out = {}

    last_price = df["price"].iloc[-1]
    last_vwap = df["vwap"].iloc[-1]
    last_atr_pct = df["atr"].iloc[-1]

    # Mean reversion trigger
    pct_diff = 100.0 * (last_price - last_vwap) / last_vwap
    out["mean_reversion"] = abs(pct_diff) >= 1.0

    # Momentum slope (Theil-Sen in %/h)
    slope_hour = slope * 3600.0
    open_price = df["price"].iloc[0]
    slope_perc_hour = slope_hour / open_price * 100.0
    out["momentum_filter"] = abs(slope_perc_hour) > 0.1

    # Stop-loss (back to absolute ATR; here, just show placeholder or omit)
    multiplier = 2.0  # how many ATR% to use
    out["stop_loss"] = float(last_price * (1 - (last_atr_pct * multiplier) / 100.0)) #if pd.notna(last_atr) else None

    # Volatility-aware position size from ATR%
    # 0%   → max size (10)
    # 2%+  → min size (1)
    raw = 10.0 - last_atr_pct * 4.5  # shrink fast after 1%
    out["position_size_multiplier"] = round(max(1.0, min(10.0, raw)), 2)

    return out


###############################################################################
# Plot
###############################################################################

def plot_price(df: pd.DataFrame, slope: float, intercept: float, outpath: pathlib.Path):
    tsec = (df["ts"] - df["ts"].iloc[0]).dt.total_seconds().values
    trend = intercept + slope * tsec

    plt.figure(figsize=(10,4))
    plt.plot(df["ts"], df["price"], label="Close price")
    plt.plot(df["ts"], trend, "--", label="Theil-Sen trend")
    if "vwap" in df.columns:
        plt.plot(df["ts"], df["vwap"], label="VWAP")

    plt.title("Coin Price + Theil-Sen Trend (5-min data)")
    plt.xlabel("Time")
    plt.ylabel("Price (USD)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=110)
    plt.close()


###############################################################################
# CLI
###############################################################################

def main():
    ap = argparse.ArgumentParser(
        description="Extended BTC script with Theil-Sen, VWAP, ATR (via resample), signals"
    )
    ap.add_argument("-f", "--file", type=pathlib.Path,
                    help="JSON file; if omitted, read from stdin")
    ap.add_argument("-o", "--output", default="btc_trend.png", type=pathlib.Path,
                    help="Output PNG file name")
    ap.add_argument("--summary-json", action="store_true",
                    help="Emit machine-readable JSON summary")
    ap.add_argument("--atr-freq", default="1h",
                    help="What timeframe to resample for ATR. e.g. '1H','30Min','4H'")
    args = ap.parse_args()

    # 1) Load data
    with (args.file.open() if args.file else sys.stdin) as f:
        df = load_json(f)

    df = convert_json(df)
    if len(df) == 0:
        print("No data found after parsing JSON.")
        sys.exit(1)

    # 2) Theil-Sen slope
    slope_sec, intercept = calc_theil_sen(df)

    # 3) VWAP on the entire 5-min data
    df["vwap"] = calc_vwap(df)

    # 4) ATR from resampled data
    df["atr"] = calc_atr_pct_with_resample(df, period=14, freq=args.atr_freq)

    # 5) Summaries
    open_price = df["price"].iloc[0]
    close_price = df["price"].iloc[-1]
    pct_change = (close_price - open_price)/open_price * 100.0
    min_idx = df["price"].idxmin()
    max_idx = df["price"].idxmax()

    slope_hour = slope_sec * 3600.0
    slope_perc_hour = slope_hour / open_price * 100.0

    summary = {
        "open_price": float(open_price),
        "close_price": float(close_price),
        "min_price": float(df["price"].iloc[min_idx]),
        "min_time": df["ts"].iloc[min_idx].isoformat(),
        "max_price": float(df["price"].iloc[max_idx]),
        "max_time": df["ts"].iloc[max_idx].isoformat(),
        "percent_change": float(pct_change),
        "theil_sen_slope_sec": float(slope_sec),
        "theil_sen_slope_hour": float(slope_hour),
        "theil_sen_slope_perc_hour": float(slope_perc_hour),
    }

    # 6) Signals
    signals = detect_signals(df, slope_sec, intercept)

    # 7) Plot
    plot_price(df, slope_sec, intercept, args.output)

    # 8) Output
    if args.summary_json:
        out_dict = {
            "summary": summary,
            "signals": signals
        }
        print(json.dumps(out_dict, indent=2))
    else:
        txt = textwrap.dedent(f"""
        Open price: ${summary['open_price']:.2f}
        Close price: ${summary['close_price']:.2f}
        Change: {summary['percent_change']:+.2f}%

        Day high: ${summary['max_price']:.2f}   ({summary['max_time']})
        Day low:  ${summary['min_price']:.2f}   ({summary['min_time']})

        Theil-Sen slope/hour: {summary['theil_sen_slope_hour']:+.2f} USD/h
                              ({summary['theil_sen_slope_perc_hour']:+.4f}%/h)

        VWAP (full period):   ${df['vwap'].iloc[-1]:.2f}
        Last ATR({args.atr_freq},14):  {df['atr'].iloc[-1]:.2f}%

        Signals:
          mean_reversion = {signals['mean_reversion']}
          momentum_filter = {signals['momentum_filter']}
          stop_loss = {signals['stop_loss']}
          position_size_multiplier = {signals['position_size_multiplier']}

        Plot saved to: {args.output}
        """).strip()
        print(txt)


if __name__ == "__main__":
    main()
