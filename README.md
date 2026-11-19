# Trading Analysis Algorithms

This repository contains three standalone market-analysis algorithms for evaluating a stock from different perspectives:

1. Technical Analysis Algorithm
2. Fundamental Analysis Algorithm
3. Social Sentiment Algorithm

All three are designed to work on a specified evaluation date and produce structured outputs (JSON, Markdown, CSV, and where applicable, charts).

## Repository contents

- `alg1_technical_analysis.py` — Technical indicator and price-pattern extraction
- `alg2_fundamental_analysis.py` — Fundamental valuation and SEC EDGAR analysis
- `alg3_social_sentiment.py` — Multi-source social sentiment and retail-discussion analysis
- `outputs/` — Generated reports and analysis artifacts

---

# Algorithm 1: Technical Indicator & Price Pattern Extraction

File: `alg1_technical_analysis.py`

## Purpose

This algorithm evaluates a stock using price action and technical indicators only. It is designed to answer:

- Is the price trend bullish or bearish?
- Is momentum positive or negative?
- Are prices stretched relative to volatility bands?
- Is the stock breaking above resistance or below support?
- What is the overall bias for a selected evaluation date?

## Input

The script accepts:

- `--ticker` : stock ticker, e.g. `AAPL`
- `--date` : evaluation date in `YYYY-MM-DD`
- `--window` : lookback window in trading days (default `120`)
- `--csv` : optional local OHLCV CSV input instead of Yahoo Finance data
- `--llm` : optional LLM narrative generation using Anthropic if `ANTHROPIC_API_KEY` is set
- `--outdir` : output folder (default `outputs`)

## Data source

By default, the algorithm downloads OHLCV data using Yahoo Finance (`yfinance`).

It also supports custom input via CSV with columns such as:

- `Date`
- `Open`
- `High`
- `Low`
- `Close`
- `Volume`

Important feature: it enforces a point-in-time rule by filtering all data to bars with `date <= evaluation_date`. This prevents look-ahead bias.

## Workflow

### Step 1: Load price data safely

The function `load_prices()` does the following:

- Converts the evaluation date to a normalized timestamp
- Downloads historical price data or reads a local CSV
- Sorts the data chronologically
- Removes duplicates
- Keeps only data that is on or before the evaluation date
- Validates required columns: `Open`, `High`, `Low`, `Close`, `Volume`
- Converts values to numeric and drops invalid rows

This ensures the analysis is based only on information available as of the requested date.

### Step 2: Trend indicators

The algorithm computes:

- SMA20
- SMA50
- SMA200
- EMA12
- EMA26

These help identify trend direction and the relationship between short-, medium- and long-term trend states.

### Step 3: Momentum indicators

The script computes:

- MACD = EMA12 - EMA26
- MACD Signal = EMA of MACD over 9 periods
- MACD Histogram = MACD - Signal
- RSI14 using Wilder smoothing

These indicators help identify whether momentum is bullish or bearish.

### Step 4: Volatility indicators

The script computes:

- Bollinger Bands (20-period, 2 standard deviations)
- ATR14 (Average True Range)

This helps identify volatility, squeeze conditions, band compression, and whether price is stretched relative to its recent range.

### Step 5: Rule-based interpretation

The function `interpret()` is a stand-in for a technical analyst and evaluates signals using a vote-based system:

- SMA50 above SMA200 = bullish vote
- MACD histogram positive = bullish vote
- RSI below 30 = bullish vote
- RSI above 70 = bearish vote
- Breakout above 20-day resistance = bullish vote
- Breakdown below 20-day support = bearish vote

It uses a vote score:

- `>= 2` → Bullish
- `<= -2` → Bearish
- Else → Neutral / Mixed

The script also records supporting notes such as:

- trend state
- crossovers
- support/resistance
- breakout status
- ATR percentage
- Bollinger %B
- price relative to moving averages

### Step 6: Optional LLM synthesis

If `--llm` is set and `ANTHROPIC_API_KEY` exists, the script calls an Anthropic model to generate a concise narrative. The LLM is instructed to use only the computed metrics and not invent numbers.

## Output files

The script writes output to the selected out directory (default `outputs/`):

- `<TICKER>_YYYY-MM-DD_technical.json`
- `<TICKER>_YYYY-MM-DD_technical.md`
- `<TICKER>_YYYY-MM-DD_technical.png`
- `<TICKER>_YYYY-MM-DD_technical_indicators.csv`

The JSON report includes:

- ticker
- evaluation date
- last bar used
- bars available
- window
- technical indicator values
- signal notes
- vote score
- overall bias
- optional LLM narrative

## Example usage

### Default Yahoo Finance data

```bash
python alg1_technical_analysis.py --ticker AAPL --date 2025-06-30
```

### Custom lookback window

```bash
python alg1_technical_analysis.py --ticker AAPL --date 2025-06-30 --window 120
```

### Use a CSV file

```bash
python alg1_technical_analysis.py --ticker TCS --date 2025-06-30 --csv my_prices.csv
```

### With LLM narrative

```bash
export ANTHROPIC_API_KEY="your_key_here"
python alg1_technical_analysis.py --ticker NVDA --date 2025-06-30 --llm
```

## Technical summary

This algorithm is best suited for:

- trend-following analysis
- price pattern recognition
- momentum assessment
- breakout support/resistance screening

It is not a fundamental valuation model and does not analyze company earnings, balance sheets, or SEC filings.

---

# Algorithm 2: Fundamental Valuation & SEC EDGAR Parsing

File: `alg2_fundamental_analysis.py`

## Purpose

This algorithm evaluates a stock using company fundamentals, SEC filings, and insider trading activity. It answers questions such as:

- Is the stock reasonably valued?
- Is the company profitable and efficient?
- Is it producing positive free cash flow?
- Is leverage manageable?
- Are insiders buying or selling?
- What is the overall fundamental view for the selected date?

## Input

The script accepts:

- `--ticker` : stock ticker, e.g. `AAPL`
- `--date` : evaluation date in `YYYY-MM-DD`
- `--price` : optional price override instead of fetching live price
- `--shares` : optional shares outstanding override
- `--facts-json` : optional offline companyfacts JSON file
- `--skip-insiders` : skip Form 4 insider analysis
- `--insider-days` : lookback period for insider trades (default `180`)
- `--llm` : optional LLM narrative generation
- `--outdir` : output folder (default `outputs`)

## Required environment variable

SEC EDGAR access requires a descriptive user-agent header.

Example:

```bash
export SEC_USER_AGENT="Pratham yourname@college.edu"
```

This is required because SEC requests include a proper `User-Agent` for compliance and rate limiting.

## Data sources

The algorithm pulls data from official SEC sources:

- Company tickers map: `https://www.sec.gov/files/company_tickers.json`
- XBRL facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- Filing submissions: `https://data.sec.gov/submissions/CIK##########.json`
- Form 4 XML filings in the SEC archive

It also fetches market price via Yahoo Finance (`yfinance`) if no price override is supplied.

## Workflow

### Step 1: Resolve ticker to CIK

The function `ticker_to_cik()`:

- fetches the SEC ticker registry
- matches the ticker to the `cik_str`
- returns both the CIK and company title

This is needed to access the correct SEC company facts and filing history.

### Step 2: Load company facts

The function `load_facts(cik)` downloads the company facts JSON, which contains parsed XBRL data for reported financial statements.

### Step 3: Parse point-in-time financial statements

The script filters filings based on the evaluation date `D` and keeps only information that was filed on or before that date.

It distinguishes between:

- annual flows (income statement and cash flow) using latest 10-K values
- instant balance sheet items using latest 10-K or 10-Q values

The function `parse_statements()` extracts key items such as:

- `TotalRevenue`
- `OperatingIncome`
- `NetIncome`
- `OperatingCashFlow`
- `CapitalExpenditure`
- `ShareholdersEquity`
- `CurrentAssets`
- `CurrentLiabilities`
- `TotalDebt`

These fields are selected using the latest valid filing while respecting the evaluation date.

### Step 4: Compute valuation and health ratios

The script computes:

- Market Cap = price × shares outstanding
- P/E Ratio = Market Cap / Net Income
- ROE = Net Income / Shareholders Equity
- Operating Margin = Operating Income / Total Revenue
- Free Cash Flow = Operating Cash Flow - Capital Expenditure
- Debt-to-Equity = Total Debt / Shareholders Equity
- Current Ratio = Current Assets / Current Liabilities

These ratios provide both valuation and balance-sheet health signals.

### Step 5: Insider trading review

The function `net_insider_flow()` scans Form 4 filings for open-market transactions over a selected lookback window (default 180 days):

- code `P` = purchase
- code `S` = sale

It sums:

- shares bought
- shares sold
- net flow = bought - sold

This adds an insider sentiment signal to the fundamental view.

### Step 6: Rule-based interpretation

The function `interpret()` assigns a score based on thresholds such as:

- P/E below 20 → favorable
- P/E above 35 → unfavorable
- ROE >= 15% → favorable
- Operating margin >= 15% → favorable
- Positive free cash flow → favorable
- Debt/Equity <= 1.5 → favorable
- Current ratio >= 1.0 → favorable
- Positive insider net flow → favorable

Then it produces one of three views:

- Fundamentally strong
- Fundamentally weak / risky
- Mixed fundamentals

## Output files

The script writes information to:

- `<TICKER>_YYYY-MM-DD_fundamental.json`
- `<TICKER>_YYYY-MM-DD_fundamental_ratios.csv`
- corresponding Markdown summary generated during execution

The JSON includes:

- ticker
- company name
- CIK
- evaluation date
- price used
- shares outstanding
- statement values
- derived ratios
- insider trading flow
- score
- view
- flags
- filing provenance (data source tags)

## Example usage

### Standard live run

```bash
export SEC_USER_AGENT="Pratham yourname@college.edu"
python alg2_fundamental_analysis.py --ticker AAPL --date 2025-06-30
```

### Skip insider trading scan

```bash
python alg2_fundamental_analysis.py --ticker MSFT --date 2025-06-30 --skip-insiders
```

### Use a price override and offline facts JSON

```bash
python alg2_fundamental_analysis.py --ticker TEST --date 2025-06-30 --facts-json facts.json --price 100 --skip-insiders
```

## Technical summary

This algorithm is best suited for:

- valuation comparison
- quality and profitability analysis
- balance-sheet health checks
- free cash flow and leverage review
- insider sentiment context

It is complementary to the technical algorithm and provides a company-level, fundamentals-first perspective.

---

## Algorithm 3: Multi-Source Social Sentiment & Screening

File: `alg3_social_sentiment.py`

## Purpose

This algorithm evaluates market sentiment from retail and financial social streams as of a selected evaluation date. It answers questions such as:

- What is the dominant bullish or bearish crowd bias?
- Are posts about the target ticker or irrelevant noise?
- Are the strongest opinions supported by engagement?
- How much weight should be given to recent posts versus older posts?
- What is the overall sentiment view for the selected date?

## Input

The script accepts:

- `--ticker` : stock ticker, e.g. `AAPL`
- `--date` : evaluation date in `YYYY-MM-DD`
- `--window` : lookback window in calendar days (default `7`)
- `--theta` : minimum entity-relevance threshold (default `0.4`)
- `--csv` : optional local social-post CSV instead of live feeds
- `--llm` : optional LLM narrative generation using Anthropic if `ANTHROPIC_API_KEY` is set
- `--outdir` : output folder (default `outputs`)

## Data source

By default, the algorithm attempts to gather:

- Reddit search results
- Yahoo Finance news headlines

If live data is unavailable or no posts are found in the point-in-time window, it uses a deterministic synthetic fallback stream for offline demonstrations. A custom CSV can be supplied instead with columns such as:

- `timestamp` (or `date`, `datetime`, `created_utc`)
- `source`
- `text` (or `title`, `body`, `headline`, `content`)
- `upvotes`
- `comments`
- `author` (optional)

The script applies a strict point-in-time filter and keeps only posts in `[evaluation_date - window, evaluation_date]`.

## Workflow

### Step 1: Collect and normalize posts

The algorithm fetches or reads posts and converts them into a consistent schema containing:

- timestamp
- source
- text
- upvote count
- comment count
- author

Timestamps are normalized and sorted before screening.

### Step 2: Screen spam, bots, and irrelevant posts

The script rejects posts that:

- are too short
- match known spam or promotion patterns
- appear to come from bot-like accounts promoting pump schemes
- do not meet the target-ticker relevance threshold

Entity relevance begins with ticker mentions and is boosted when the text also contains financial context such as earnings, valuation, calls, puts, growth, or guidance.

### Step 3: Classify polarity

Each retained post receives a polarity score in `[-1.0, +1.0]` using a financial and retail sentiment lexicon:

- bullish terms increase polarity
- bearish terms decrease polarity
- negation terms reverse the sentiment weight
- `tanh(raw_score / 2.2)` smooths the result into the final range

Post polarity is classified as:

- Bullish: polarity greater than `+0.1`
- Bearish: polarity less than `-0.1`
- Neutral: polarity between `-0.1` and `+0.1`

### Step 4: Apply engagement and recency weighting

The weight of a validated post is:

`weight = log(1 + upvotes + comments) * exp(-lambda * age_days)`

where `lambda` is `0.35`. This gives more influence to:

- highly engaged posts
- recent posts

The composite crowd polarity score is:

`P_score = sum(polarity * weight) / sum(weight)`

It is also constrained to `[-1.0, +1.0]`:

- `P_score >= 0.20` → Bullish
- `P_score <= -0.20` → Bearish
- otherwise → Neutral / Mixed

### Step 5: Generate the report

The script ranks validated posts by weight and optionally calls an Anthropic model for narrative synthesis. The LLM receives only the computed report metrics and posts; it does not replace or recompute the score.

## Output files

The script writes output to the selected out directory (default `outputs/`):

- `<TICKER>_YYYY-MM-DD_sentiment.json`
- `<TICKER>_YYYY-MM-DD_sentiment.md`
- `<TICKER>_YYYY-MM-DD_sentiment.png`
- `<TICKER>_YYYY-MM-DD_sentiment_posts.csv`

The JSON report includes:

- ticker and evaluation date
- lookback window and relevance threshold
- raw, validated, and discarded post counts
- bullish, bearish, and neutral post counts
- composite polarity score and overall bias
- total engagement-recency weight
- top weighted posts
- optional LLM narrative

## Example usage

### Default live feeds and fallback stream

```bash
python alg3_social_sentiment.py --ticker AAPL --date 2025-06-30
```

### Custom lookback and relevance threshold

```bash
python alg3_social_sentiment.py --ticker NVDA --date 2025-06-30 --window 7 --theta 0.4
```

### Use a CSV file

```bash
python alg3_social_sentiment.py --ticker TCS --date 2025-06-30 --csv my_social_stream.csv
```

### With LLM narrative

```bash
export ANTHROPIC_API_KEY="your_key_here"
python alg3_social_sentiment.py --ticker AAPL --date 2025-06-30 --llm
```

## Technical summary

This algorithm is best suited for:

- social and retail crowd-sentiment screening
- spam and bot filtering
- engagement-weighted sentiment aggregation
- comparing how different social sources contribute to crowd bias

Lexicon-based polarity is not a substitute for source verification or financial analysis. It should be treated as a sentiment signal rather than a standalone trading recommendation.

---

# How the three algorithms work together

The three algorithms are designed to be complementary:

- Technical analysis focuses on price behavior, momentum, and short-term trading signals.
- Fundamental analysis focuses on company quality, valuation, and accounting-based health.
- Social sentiment analysis focuses on the direction and concentration of public discussion.

A practical workflow is:

1. Run the technical algorithm to assess trend and momentum.
2. Run the fundamental algorithm to assess valuation and business quality.
3. Run the sentiment algorithm to assess crowd bias and discussion quality.
4. Compare outputs to form a broader investment view.

In a full research workflow, the technical and fundamental views can be combined to decide whether a setup is:

- high-quality and trending up
- fundamentally weak but technically strong
- fundamentally strong but technically weak
- mixed and uncertain

---

# Quick start

## Technical analysis

```bash
python alg1_technical_analysis.py --ticker AAPL --date 2025-06-30 --window 120
```

## Fundamental analysis

```bash
export SEC_USER_AGENT="Pratham yourname@college.edu"
python alg2_fundamental_analysis.py --ticker AAPL --date 2025-06-30
```

## Social sentiment analysis

```bash
python alg3_social_sentiment.py --ticker AAPL --date 2025-06-30 --window 7
```

---

# Notes and assumptions

- All scripts are built to be date-aware and prevent look-ahead leakage.
- The technical model uses data available up to the selected date only.
- The fundamental model relies on SEC filings and XBRL structured values available as of the selected date.
- The social sentiment model uses posts from only the selected lookback window ending on the evaluation date.
- LLM output is optional and only runs when the Anthropic API key is available.
- The SEC model requires a valid `SEC_USER_AGENT` value to comply with SEC access rules.
- Social sentiment uses live Reddit and Yahoo Finance news when available, with a synthetic fallback stream when live data cannot be retrieved.
- If internet access or official data sources are unavailable, the fundamental analysis may fail or require offline JSON inputs.

This README covers the key logic, data pipeline, mathematical/financial rules, and operational usage for all algorithms in this project.
