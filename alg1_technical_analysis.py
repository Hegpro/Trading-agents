#!/usr/bin/env python3
"""
Algorithm 1 - Technical Indicator & Price Pattern Extraction
(TradingAgents project, standalone runnable version)

Input : ticker T, evaluation date D, lookback window W (trading days)
Output: technical analysis report R_tech (JSON + Markdown + CSV + PNG chart)

Dataset options
  A) Live Yahoo Finance OHLCV via `yfinance`        (default)
  B) Your own CSV with columns Date,Open,High,Low,Close,Volume   (--csv file.csv)

Examples
  python alg1_technical_analysis.py --ticker AAPL --date 2025-06-30
  python alg1_technical_analysis.py --ticker AAPL --date 2025-06-30 --window 120
  python alg1_technical_analysis.py --ticker TCS --date 2025-06-30 --csv my_prices.csv
  python alg1_technical_analysis.py --ticker NVDA --date 2025-06-30 --llm   # needs ANTHROPIC_API_KEY
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = ["Open", "High", "Low", "Close", "Volume"]


# --------------------------------------------------------------------------
# Step 1: fetch point-in-time OHLCV  P[D-W : D]  and enforce  t <= D
# --------------------------------------------------------------------------
def load_prices(ticker, date, window, csv_path=None):
    D = pd.Timestamp(date).normalize()

    if csv_path:
        df = pd.read_csv(csv_path)
        date_col = next((c for c in df.columns if c.strip().lower() in ("date", "datetime", "timestamp")),
                        df.columns[0])
        df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce").dt.tz_localize(None)
        df = df.dropna(subset=[date_col]).set_index(date_col)
        df.columns = [c.strip().title() for c in df.columns]
    else:
        try:
            import yfinance as yf
        except ImportError:
            sys.exit("Install yfinance:  pip install yfinance")
        # extra history so that SMA200 / EMA warm-up is valid at the start of the window
        start = D - pd.Timedelta(days=int(window * 1.5) + 400)
        end = D + pd.Timedelta(days=1)  # yfinance 'end' is exclusive
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                         auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"Data is missing columns: {missing}. Found: {list(df.columns)}")

    df = df.sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")]
    df = df[df.index <= D]  # PIT firewall: no bar after D may survive
    df = df[REQUIRED].apply(pd.to_numeric, errors="coerce").dropna()
    if df.empty:
        sys.exit("No price data on or before the evaluation date.")
    assert df.index.max() <= D, "Look-ahead leakage detected!"
    return df


# --------------------------------------------------------------------------
# Steps 2-4: trend, momentum, volatility indicators
# --------------------------------------------------------------------------
def compute_indicators(df):
    c, h, l = df["Close"], df["High"], df["Low"]
    out = df.copy()

    # Step 2 - trend
    out["SMA20"] = c.rolling(20).mean()
    out["SMA50"] = c.rolling(50).mean()
    out["SMA200"] = c.rolling(200).mean()
    out["EMA12"] = c.ewm(span=12, adjust=False).mean()
    out["EMA26"] = c.ewm(span=26, adjust=False).mean()

    # Step 3 - momentum: MACD (12,26,9) and RSI14 (Wilder smoothing)
    out["MACD"] = out["EMA12"] - out["EMA26"]
    out["MACD_Signal"] = out["MACD"].ewm(span=9, adjust=False).mean()
    out["MACD_Hist"] = out["MACD"] - out["MACD_Signal"]

    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["RSI14"] = (100 - 100 / (1 + rs)).fillna(100.0).where(avg_gain.notna())

    # Step 4 - volatility: Bollinger Bands (20, 2 sigma) and ATR14
    std20 = c.rolling(20).std(ddof=0)
    out["Upper_BB"] = out["SMA20"] + 2 * std20
    out["Lower_BB"] = out["SMA20"] - 2 * std20
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["ATR14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return out


# --------------------------------------------------------------------------
# Step 5 (rule-based stand-in for the Technical Analyst LLM)
# --------------------------------------------------------------------------
def _num(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) or pd.isna(x) else float(x)


def interpret(ind, window):
    last, prev = ind.iloc[-1], ind.iloc[-2] if len(ind) > 1 else ind.iloc[-1]
    votes, notes = 0, {}

    # trend
    if pd.notna(last["SMA200"]):
        if last["SMA50"] > last["SMA200"]:
            notes["trend"] = "Bullish: SMA50 above SMA200"
            votes += 1
        else:
            notes["trend"] = "Bearish: SMA50 below SMA200"
            votes -= 1
        spread = np.sign((ind["SMA50"] - ind["SMA200"]).dropna().tail(11))
        if len(spread) > 1 and spread.iloc[0] != spread.iloc[-1]:
            notes["cross"] = "Golden cross in last 10 bars" if spread.iloc[-1] > 0 else "Death cross in last 10 bars"
    else:
        notes["trend"] = "Insufficient history for SMA200 (need 200 bars)"
    if pd.notna(last["SMA50"]):
        notes["price_vs_sma50"] = "Above SMA50" if last["Close"] > last["SMA50"] else "Below SMA50"

    # momentum
    if pd.notna(last["MACD_Hist"]):
        if last["MACD_Hist"] > 0:
            notes["macd"] = "Bullish momentum (histogram > 0)"
            votes += 1
        else:
            notes["macd"] = "Bearish momentum (histogram < 0)"
            votes -= 1
        if pd.notna(prev["MACD_Hist"]) and prev["MACD_Hist"] <= 0 < last["MACD_Hist"]:
            notes["macd_cross"] = "Fresh bullish MACD crossover"
        if pd.notna(prev["MACD_Hist"]) and prev["MACD_Hist"] >= 0 > last["MACD_Hist"]:
            notes["macd_cross"] = "Fresh bearish MACD crossover"
    if pd.notna(last["RSI14"]):
        r = last["RSI14"]
        if r > 70:
            notes["rsi"] = f"Overbought (RSI {r:.1f} > 70)"
            votes -= 1
        elif r < 30:
            notes["rsi"] = f"Oversold (RSI {r:.1f} < 30)"
            votes += 1
        else:
            notes["rsi"] = f"Neutral (RSI {r:.1f})"

    # volatility
    if pd.notna(last["Upper_BB"]):
        pb = (last["Close"] - last["Lower_BB"]) / (last["Upper_BB"] - last["Lower_BB"])
        notes["bollinger_pct_b"] = round(float(pb), 3)
        notes["bollinger"] = ("Close above upper band (stretched / breakout)" if pb > 1 else
                              "Close below lower band (oversold / breakdown)" if pb < 0 else
                              "Close inside bands")
    if pd.notna(last["ATR14"]):
        notes["atr_pct"] = round(float(last["ATR14"] / last["Close"] * 100), 2)

    # support / resistance and breakouts (20-bar channel, excluding today)
    w = ind.tail(window)
    if len(w) > 21:
        res20 = w["High"].iloc[-21:-1].max()
        sup20 = w["Low"].iloc[-21:-1].min()
        notes["resistance_20d"], notes["support_20d"] = float(res20), float(sup20)
        if last["Close"] > res20:
            notes["breakout"] = "Breakout above 20-day resistance"
            votes += 1
        elif last["Close"] < sup20:
            notes["breakout"] = "Breakdown below 20-day support"
            votes -= 1
    notes["window_high"], notes["window_low"] = float(w["High"].max()), float(w["Low"].min())

    bias = "Bullish" if votes >= 2 else "Bearish" if votes <= -2 else "Neutral / Mixed"
    return votes, bias, notes


def optional_llm(report):
    """Technical Analyst LLM step. Runs only with --llm and ANTHROPIC_API_KEY."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[llm] ANTHROPIC_API_KEY not set - skipping LLM synthesis")
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            max_tokens=900,
            messages=[{"role": "user", "content":
                       "You are a technical analyst. Using ONLY these computed indicators, write a concise "
                       "report (trend, momentum, volatility, support/resistance, overall view). "
                       "Do not invent numbers.\n\n" + json.dumps(report, indent=2)}])
        return msg.content[0].text
    except Exception as e:  # noqa
        print(f"[llm] failed: {e}")
        return None


def save_chart(ind, path, title, window):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    w = ind.tail(window)
    fig, ax = plt.subplots(3, 1, figsize=(12, 10), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
    ax[0].plot(w.index, w["Close"], label="Close", lw=1.4)
    for col in ("SMA50", "SMA200"):
        ax[0].plot(w.index, w[col], label=col, lw=1)
    ax[0].fill_between(w.index, w["Lower_BB"], w["Upper_BB"], alpha=0.15, label="Bollinger (20,2)")
    ax[0].set_title(title)
    ax[0].legend(loc="upper left", ncol=4)
    ax[1].bar(w.index, w["MACD_Hist"], color=np.where(w["MACD_Hist"] >= 0, "g", "r"), alpha=0.6)
    ax[1].plot(w.index, w["MACD"], lw=1, label="MACD")
    ax[1].plot(w.index, w["MACD_Signal"], lw=1, label="Signal")
    ax[1].legend(loc="upper left")
    ax[2].plot(w.index, w["RSI14"], color="purple")
    ax[2].axhline(70, color="r", ls="--", lw=0.8)
    ax[2].axhline(30, color="g", ls="--", lw=0.8)
    ax[2].set_ylim(0, 100)
    ax[2].set_ylabel("RSI14")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="Algorithm 1: Technical Indicator & Price Pattern Extraction")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", required=True, help="Evaluation date D, YYYY-MM-DD")
    ap.add_argument("--window", type=int, default=120, help="Lookback window W in trading days (default 120)")
    ap.add_argument("--csv", help="Use your own OHLCV CSV instead of Yahoo Finance")
    ap.add_argument("--llm", action="store_true", help="Add LLM-written narrative (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--outdir", default="outputs")
    a = ap.parse_args()

    df = load_prices(a.ticker, a.date, a.window, a.csv)
    ind = compute_indicators(df)
    if len(ind) < 30:
        sys.exit("Need at least ~30 bars to compute indicators.")
    votes, bias, notes = interpret(ind, a.window)
    last = ind.iloc[-1]

    keys = ["Close", "SMA50", "SMA200", "EMA12", "EMA26", "MACD", "MACD_Signal", "MACD_Hist",
            "RSI14", "SMA20", "Upper_BB", "Lower_BB", "ATR14"]
    report = {
        "ticker": a.ticker.upper(),
        "requested_date": a.date,
        "last_bar_used": str(ind.index[-1].date()),
        "bars_available": int(len(ind)),
        "window": a.window,
        "indicators": {k: _num(last[k]) for k in keys},
        "signals": notes,
        "vote_score": votes,
        "overall_bias": bias,
    }
    if a.llm:
        report["llm_narrative"] = optional_llm(report)

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.ticker.upper()}_{a.date}_technical"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2))
    ind.tail(a.window).to_csv(out / f"{stem}_indicators.csv")
    save_chart(ind, out / f"{stem}.png", f"{a.ticker.upper()} technical view @ {report['last_bar_used']}", a.window)

    md = [f"# Technical Analyst Report - {report['ticker']} ({report['last_bar_used']})", "",
          f"**Overall bias: {bias}** (vote score {votes:+d})", "", "## Indicators"]
    md += [f"- {k}: {v:.4f}" if v is not None else f"- {k}: n/a" for k, v in report["indicators"].items()]
    md += ["", "## Signals"] + [f"- {k}: {v}" for k, v in notes.items()]
    if report.get("llm_narrative"):
        md += ["", "## LLM narrative", report["llm_narrative"]]
    (out / f"{stem}.md").write_text("\n".join(md))

    print("\n".join(md))
    print(f"\nSaved to {out.resolve()}/{stem}.(json|md|png) and _indicators.csv")


if __name__ == "__main__":
    main()
