#!/usr/bin/env python3
"""
Algorithm 3 - Multi-Source Social Sentiment & Screening Algorithm
(TradingAgents project, standalone runnable version)

Input : ticker T, evaluation date D, lookback window W (default 7 days), raw social streams S_raw
Output: grounded sentiment analysis report R_sent (JSON + Markdown + CSV + PNG chart)
        with composite crowd polarity score P_score in [-1.0, +1.0]

Dataset options
  A) Live Reddit + Yahoo Finance news streams                  (default, fallback to synthetic if offline)
  B) Your own CSV with columns: timestamp,source,text,upvotes,comments,author (--csv posts.csv)

Examples
  python alg3_social_sentiment.py --ticker AAPL --date 2025-06-30
  python alg3_social_sentiment.py --ticker NVDA --date 2025-06-30 --window 7
  python alg3_social_sentiment.py --ticker TCS --date 2025-06-30 --csv my_social_stream.csv
  python alg3_social_sentiment.py --ticker AAPL --date 2025-06-30 --llm   # needs ANTHROPIC_API_KEY
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = ["timestamp", "source", "text", "upvotes", "comments"]

# Financial & Retail Sentiment Lexicon (for standalone polarity scoring in [-1.0, +1.0])
BULL_TERMS = {
    "beat": 1.2, "beats": 1.2, "bull": 1.0, "bullish": 1.3, "buy": 0.9, "buying": 0.9,
    "breakout": 1.3, "calls": 0.8, "catalyst": 0.8, "growth": 0.9, "long": 0.7,
    "moon": 1.2, "outperform": 1.2, "profit": 0.8, "rally": 1.2, "record": 0.9,
    "rocket": 1.1, "soar": 1.3, "soars": 1.3, "strong": 0.8, "surge": 1.2,
    "surges": 1.2, "undervalued": 1.3, "upgrade": 1.3, "upgraded": 1.3, "upside": 1.0,
    "Accumulate": 1.0, "winner": 0.9, "boom": 1.1, "exceeds": 1.1, "optimistic": 1.0,
}

BEAR_TERMS = {
    "bagholder": -1.2, "bear": -1.0, "bearish": -1.3, "bubble": -1.2, "crash": -1.5,
    "cut": -0.8, "cuts": -0.8, "decline": -1.0, "declines": -1.0, "default": -1.4,
    "downgrade": -1.3, "downgraded": -1.3, "drop": -0.9, "drops": -0.9, "dump": -1.3,
    "Dump": -1.3, "fall": -0.8, "falls": -0.8, "fraud": -1.6, "investigation": -1.2,
    "lawsuit": -1.2, "layoff": -1.0, "layoffs": -1.0, "loss": -1.0, "losses": -1.0,
    "miss": -1.2, "misses": -1.2, "overvalued": -1.3, "plunge": -1.4, "plunges": -1.4,
    "puts": -0.8, "recession": -1.1, "risk": -0.6, "sell": -0.9, "selling": -0.9,
    "short": -0.8, "slump": -1.2, "tank": -1.3, "tanks": -1.3, "weak": -0.9,
}

SPAM_PATTERNS = [
    r"join my (telegram|discord|whatsapp)",
    r"100x guaranteed",
    r"free crypto",
    r"pump group",
    r"dm me for signals",
    r"airdrop",
    r"whatsapp\s*\+?\d+",
    r"click here to claim",
]


# --------------------------------------------------------------------------
# Step 1: Fetch social posts & retail discussion threads S_raw in [D-W, D]
# --------------------------------------------------------------------------
def _generate_fallback_stream(ticker, D, window):
    """Generates realistic point-in-time retail/news posts if live scraping is offline."""
    rng = np.random.default_rng(abs(hash(f"{ticker}_{D.strftime('%Y-%m-%d')}")) % (2**32))
    templates = [
        ("Reddit/r/stocks", f"${ticker} earnings beat expectations with strong margin expansion and bullish guidance.", 145, 42, "quant_trader99"),
        ("StockTwits", f"${ticker} breakout above resistance! Buying calls for next week rally.", 38, 9, "alpha_hunter"),
        ("YahooNews", f"{ticker} upgraded by institutional desk citing strong free cash flow and product growth.", 210, 55, "market_desk"),
        ("Reddit/r/wallstreetbets", f"Loaded up on ${ticker} shares today, undervalued relative to sector peers.", 320, 88, "value_ape"),
        ("StockTwits", f"${ticker} looking overvalued here, watching for potential pullback and profit taking.", 29, 11, "chart_watcher"),
        ("YahooNews", f"Sector headwinds and supply chain risk could pressure {ticker} operating margins next quarter.", 95, 24, "macro_wire"),
        ("Reddit/r/investing", f"Concerned about {ticker} valuation multiples if rate cuts are delayed; holding cash.", 64, 31, "prudent_inv"),
        ("StockTwits", f"FREE CRYPTO PUMP GROUP! Join my telegram for 100x guaranteed signals on ${ticker}!", 2, 0, "crypto_bot_8821"),
        ("Reddit/r/stocks", f"Unrelated discussion about random penny stocks and sports betting odds.", 12, 4, "random_user"),
        ("StockTwits", f"${ticker} institutional accumulation remains strong despite short-term volatility.", 54, 14, "flow_tracker"),
        ("YahooNews", f"{ticker} announces new strategic partnership expanding enterprise market share.", 180, 39, "biz_journal"),
        ("Reddit/r/stocks", f"Short sellers increasing puts on ${ticker} ahead of regulatory review.", 88, 47, "bear_fund"),
    ]
    rows = []
    for i, (src, txt, up, com, auth) in enumerate(templates):
        days_ago = float(rng.uniform(0.1, max(1.0, float(window))))
        ts = D - pd.Timedelta(days=days_ago)
        rows.append({
            "timestamp": ts,
            "source": src,
            "text": txt,
            "upvotes": int(max(0, up + rng.integers(-10, 20))),
            "comments": int(max(0, com + rng.integers(-5, 10))),
            "author": auth,
        })
    return pd.DataFrame(rows)


def load_social_stream(ticker, date, window=7, csv_path=None):
    D = pd.Timestamp(date).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    start_dt = D.normalize() - pd.Timedelta(days=window)
    t_up = ticker.upper()

    if csv_path:
        df = pd.read_csv(csv_path)
        col_map = {c.strip().lower(): c for c in df.columns}
        ts_col = next((col_map[k] for k in ("timestamp", "date", "datetime", "created_utc") if k in col_map), df.columns[0])
        txt_col = next((col_map[k] for k in ("text", "title", "body", "headline", "content") if k in col_map), None)
        if txt_col is None:
            sys.exit(f"CSV must contain a text/headline column. Found: {list(df.columns)}")

        out = pd.DataFrame()
        out["timestamp"] = pd.to_datetime(df[ts_col], utc=True, errors="coerce").dt.tz_localize(None)
        out["source"] = df[col_map["source"]] if "source" in col_map else "CSV_Stream"
        out["text"] = df[txt_col].astype(str)
        out["upvotes"] = pd.to_numeric(df[col_map["upvotes"]], errors="coerce").fillna(1) if "upvotes" in col_map else 1
        out["comments"] = pd.to_numeric(df[col_map["comments"]], errors="coerce").fillna(0) if "comments" in col_map else 0
        out["author"] = df[col_map["author"]].astype(str) if "author" in col_map else "user"
        df = out.dropna(subset=["timestamp", "text"])
    else:
        records = []
        # Attempt 1: Live Reddit JSON search
        try:
            url = f"https://www.reddit.com/search.json?q={t_up}&sort=new&limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "TradingAgents-Algo3/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for child in data.get("data", {}).get("children", []):
                    p = child.get("data", {})
                    ts = pd.to_datetime(p.get("created_utc", 0), unit="s", utc=True).tz_localize(None)
                    title = p.get("title", "")
                    body = p.get("selftext", "")
                    records.append({
                        "timestamp": ts,
                        "source": f"Reddit/r/{p.get('subreddit', 'stocks')}",
                        "text": f"{title}. {body}".strip()[:500],
                        "upvotes": int(p.get("ups", 1)),
                        "comments": int(p.get("num_comments", 0)),
                        "author": str(p.get("author", "reddit_user")),
                    })
        except Exception:
            pass

        # Attempt 2: Yahoo Finance news via yfinance
        try:
            import yfinance as yf
            news_list = getattr(yf.Ticker(t_up), "news", []) or []
            for item in news_list:
                pub = item.get("providerPublishTime")
                if pub:
                    ts = pd.to_datetime(pub, unit="s", utc=True).tz_localize(None)
                    records.append({
                        "timestamp": ts,
                        "source": f"News/{item.get('publisher', 'Finance')}",
                        "text": str(item.get("title", "")),
                        "upvotes": 25,
                        "comments": 5,
                        "author": str(item.get("publisher", "news_wire")),
                    })
        except Exception:
            pass

        df = pd.DataFrame(records)
        if not df.empty:
            df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= D)]
        if df.empty:
            print(f"[info] No live stream archived inside [{start_dt.date()}, {D.date()}] - using point-in-time synthetic stream.")
            df = _generate_fallback_stream(t_up, D, window)

    # Strict PIT firewall: no post after D or before D - window
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= D)].copy()
    if df.empty:
        sys.exit(f"No social posts found in window [{start_dt.date()}, {D.date()}].")
    assert df["timestamp"].max() <= D, "Look-ahead leakage detected in social stream!"
    return df, D


# --------------------------------------------------------------------------
# Step 3: Bot, Spam & Entity Relevance Screening Filter
# --------------------------------------------------------------------------
def social_screening_filter(post_text, author=""):
    text_low = str(post_text).lower()
    auth_low = str(author).lower()
    if len(text_low.strip()) < 12:
        return "SPAM_OR_BOT"
    if any(re.search(pat, text_low) for pat in SPAM_PATTERNS):
        return "SPAM_OR_BOT"
    if "bot" in auth_low and ("crypto" in auth_low or "pump" in text_low):
        return "SPAM_OR_BOT"
    return "VALID"


def entity_relevance(post_text, ticker):
    text_low = str(post_text).lower()
    t_low = ticker.lower()
    score = 0.0
    if f"${t_low}" in text_low:
        score += 0.8
    if re.search(rf"{re.escape(t_low)}", text_low):
        score += 0.6
    fin_context = ["stock", "shares", "earnings", "revenue", "margin", "market", "price",
                   "valuation", "calls", "puts", "guidance", "quarter", "growth", "upgrade", "downgrade"]
    if any(w in text_low for w in fin_context):
        score += 0.3
    return min(1.0, score)


# --------------------------------------------------------------------------
# Step 4 & 5: Polarity Classification, Engagement x Recency Weighting, P_score
# --------------------------------------------------------------------------
def classify_sentiment_polarity(text):
    """Returns numeric polarity score in [-1.0, +1.0]."""
    tokens = re.findall(r"[a-zA-Z]+", str(text).lower())
    if not tokens:
        return 0.0
    raw = 0.0
    negate = False
    negators = {"not", "no", "never", "neither", "barely", "hardly", "without", "dont", "cant", "wont"}
    for i, tok in enumerate(tokens):
        if tok in negators:
            negate = True
            continue
        val = BULL_TERMS.get(tok, 0.0) + BEAR_TERMS.get(tok, 0.0)
        if val != 0.0:
            if negate:
                val = -val * 0.8
                negate = False
            raw += val
        elif i > 0 and tokens[i - 1] in negators:
            negate = False
    # Smooth normalization into [-1.0, +1.0]
    return float(np.tanh(raw / 2.2))


def evaluate_sentiment(df, ticker, D, theta_rel=0.4, decay_lambda=0.35):
    # Step 2: Initialize ValidatedPosts = [], WeightedSum = 0.0, TotalWeight = 0.0
    validated_rows = []
    discarded_count = 0
    weighted_sum = 0.0
    total_weight = 0.0

    # Step 3 & Step 4
    for _, row in df.iterrows():
        text = row["text"]
        author = row.get("author", "")
        screening = social_screening_filter(text, author)
        rel = entity_relevance(text, ticker)

        if screening == "SPAM_OR_BOT" or rel < theta_rel:
            discarded_count += 1
            continue

        polarity = classify_sentiment_polarity(text)
        upvotes = max(0.0, float(row["upvotes"]))
        comments = max(0.0, float(row["comments"]))
        engagement = math.log(1.0 + upvotes + comments)

        age_days = max(0.0, (D - row["timestamp"]).total_seconds() / 86400.0)
        decay = math.exp(-decay_lambda * age_days)
        weight = engagement * decay

        weighted_sum += polarity * weight
        total_weight += weight

        validated_rows.append({
            "timestamp": str(row["timestamp"]),
            "source": row["source"],
            "author": author,
            "text": text,
            "upvotes": int(upvotes),
            "comments": int(comments),
            "relevance": round(rel, 3),
            "polarity": round(polarity, 4),
            "engagement": round(engagement, 4),
            "decay": round(decay, 4),
            "weight": round(weight, 4),
            "weighted_polarity": round(polarity * weight, 4),
        })

    # Step 5: Compute normalized composite crowd polarity score P_score
    p_score = (weighted_sum / total_weight) if total_weight > 0 else 0.0
    p_score = float(np.clip(p_score, -1.0, 1.0))

    if p_score >= 0.20:
        bias = "Bullish"
    elif p_score <= -0.20:
        bias = "Bearish"
    else:
        bias = "Neutral / Mixed"

    val_df = pd.DataFrame(validated_rows)
    stats = {
        "raw_posts_count": int(len(df)),
        "validated_posts_count": int(len(validated_rows)),
        "discarded_spam_or_irrelevant": int(discarded_count),
        "bullish_posts": int((val_df["polarity"] > 0.1).sum()) if not val_df.empty else 0,
        "bearish_posts": int((val_df["polarity"] < -0.1).sum()) if not val_df.empty else 0,
        "neutral_posts": int((val_df["polarity"].abs() <= 0.1).sum()) if not val_df.empty else 0,
        "weighted_sum": round(weighted_sum, 4),
        "total_weight": round(total_weight, 4),
        "p_score": round(p_score, 4),
        "overall_bias": bias,
    }
    return val_df, stats


# --------------------------------------------------------------------------
# Step 6: Sentiment Analyst LLM Narrative Synthesis (Optional)
# --------------------------------------------------------------------------
def optional_llm(report):
    """Sentiment Analyst LLM step. Runs only with --llm and ANTHROPIC_API_KEY."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[llm] ANTHROPIC_API_KEY not set - skipping LLM synthesis")
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
            max_tokens=900,
            messages=[{"role": "user", "content":
                       "You are a Sentiment Analyst in the TradingAgents framework. Using ONLY these "
                       "validated social posts and composite polarity metrics, write a concise sentiment "
                       "report R_sent (crowd mood, dominant bullish/bearish themes, spam filtering summary, "
                       "and contrarian or momentum implications). Do not invent numbers.\n\n"
                       + json.dumps(report, indent=2)}])
        return msg.content[0].text
    except Exception as e:  # noqa
        print(f"[llm] failed: {e}")
        return None


def save_chart(val_df, stats, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(2, 1, figsize=(11, 7.5), gridspec_kw={"height_ratios": [2.2, 1.3]})
    if not val_df.empty:
        ts = pd.to_datetime(val_df["timestamp"])
        colors = np.where(val_df["polarity"] > 0.1, "g", np.where(val_df["polarity"] < -0.1, "r", "gray"))
        sizes = np.clip(val_df["weight"] * 45 + 30, 30, 450)
        ax[0].scatter(ts, val_df["polarity"], c=colors, s=sizes, alpha=0.75, edgecolors="k", linewidth=0.5)
        ax[0].axhline(stats["p_score"], color="blue", ls="--", lw=1.5, label=f"Composite P_score = {stats['p_score']:+.3f}")
        ax[0].axhline(0.0, color="black", lw=0.8, alpha=0.5)
        ax[0].set_ylim(-1.1, 1.1)
        ax[0].set_ylabel("Post Polarity [-1, +1]")
        ax[0].set_title(title)
        ax[0].legend(loc="upper left")

        src_grp = val_df.groupby("source")["weighted_polarity"].sum()
        bar_colors = ["g" if v >= 0 else "r" for v in src_grp.values]
        ax[1].barh(src_grp.index, src_grp.values, color=bar_colors, alpha=0.75)
        ax[1].axvline(0, color="black", lw=0.8)
        ax[1].set_xlabel("Net Weighted Polarity Contribution by Source")
    else:
        ax[0].text(0.5, 0.5, "No Validated Posts", ha="center", va="center")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="Algorithm 3: Multi-Source Social Sentiment & Screening")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", required=True, help="Evaluation date D, YYYY-MM-DD")
    ap.add_argument("--window", type=int, default=7, help="Lookback window in calendar days (default 7)")
    ap.add_argument("--theta", type=float, default=0.4, help="Minimum entity relevance threshold (default 0.4)")
    ap.add_argument("--csv", help="Use your own social posts CSV instead of live feeds")
    ap.add_argument("--llm", action="store_true", help="Add LLM-written narrative (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--outdir", default="outputs")
    a = ap.parse_args()

    raw_df, D = load_social_stream(a.ticker, a.date, a.window, a.csv)
    val_df, stats = evaluate_sentiment(raw_df, a.ticker, D, theta_rel=a.theta)

    top_posts = []
    if not val_df.empty:
        top_posts = val_df.sort_values("weight", ascending=False).head(8).to_dict(orient="records")

    report = {
        "ticker": a.ticker.upper(),
        "requested_date": a.date,
        "window_days": a.window,
        "relevance_threshold": a.theta,
        "composite_polarity_score": stats["p_score"],
        "overall_bias": stats["overall_bias"],
        "screening_and_metrics": stats,
        "top_weighted_posts": top_posts,
    }
    if a.llm:
        report["llm_narrative"] = optional_llm(report)

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.ticker.upper()}_{a.date}_sentiment"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2))
    val_df.to_csv(out / f"{stem}_posts.csv", index=False)
    save_chart(val_df, stats, out / f"{stem}.png", f"{a.ticker.upper()} Social Sentiment @ {a.date}")

    md = [
        f"# Sentiment Analyst Report (R_sent) - {report['ticker']} ({a.date})",
        "",
        f"**Overall crowd bias: {stats['overall_bias']}** (Composite $P_{{score}}$ = {stats['p_score']:+.4f})",
        "",
        "## Screening & Aggregation Metrics",
        f"- Raw posts fetched: {stats['raw_posts_count']}",
        f"- Validated posts retained: {stats['validated_posts_count']}",
        f"- Discarded (spam / bot / low relevance): {stats['discarded_spam_or_irrelevant']}",
        f"- Post breakdown: {stats['bullish_posts']} Bullish | {stats['bearish_posts']} Bearish | {stats['neutral_posts']} Neutral",
        f"- Total engagement-recency weight: {stats['total_weight']:.4f}",
        "",
        "## Top Weighted Validated Posts",
    ]
    for p in top_posts[:5]:
        md.append(f"- **[{p['source']}]** (polarity {p['polarity']:+.2f}, weight {p['weight']:.2f}): {p['text']}")
    if report.get("llm_narrative"):
        md += ["", "## LLM Narrative", report["llm_narrative"]]
    (out / f"{stem}.md").write_text("\n".join(md))

    print("\n".join(md))
    print(f"\nSaved to {out.resolve()}/{stem}.(json|md|png) and _posts.csv")


if __name__ == "__main__":
    main()
