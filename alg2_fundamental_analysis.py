#!/usr/bin/env python3
"""
Algorithm 2 - Fundamental Valuation & SEC EDGAR Parsing
(TradingAgents project, standalone runnable version)

Input : ticker T, evaluation date D, SEC EDGAR filings (10-K / 10-Q), Form 4 insider records
Output: fundamental valuation report R_fund (JSON + Markdown + CSV)

Dataset: SEC EDGAR (free, official). Uses
  - https://www.sec.gov/files/company_tickers.json         (ticker -> CIK)
  - https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   (parsed 10-K/10-Q statements)
  - https://data.sec.gov/submissions/CIK##########.json    (filing list incl. Form 4)
  - Form 4 XML documents under sec.gov/Archives
Price for market cap: Yahoo Finance via yfinance (or pass --price).

SEC REQUIRES a descriptive User-Agent. Set it once:
  Windows PowerShell : $env:SEC_USER_AGENT = "Pratham yourname@college.edu"
  Linux / macOS      : export SEC_USER_AGENT="Pratham yourname@college.edu"

Examples
  python alg2_fundamental_analysis.py --ticker AAPL --date 2025-06-30
  python alg2_fundamental_analysis.py --ticker MSFT --date 2025-06-30 --skip-insiders
  # fully offline (saved companyfacts JSON):
  python alg2_fundamental_analysis.py --ticker TEST --date 2025-06-30 --facts-json facts.json --price 100 --skip-insiders
"""
import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

import pandas as pd

from dotenv import load_dotenv
load_dotenv()

SEC_UA = os.environ.get("SEC_USER_AGENT") or "Pratham prathamahegde9@gmail.com"


# --------------------------------------------------------------------------
# EDGAR access
# --------------------------------------------------------------------------
def sec_get(url):
    import requests
    if not SEC_UA:
        sys.exit("Set SEC_USER_AGENT first, e.g.  export SEC_USER_AGENT=\"Your Name you@email.com\"")
    time.sleep(0.15)  # SEC fair-use limit: <= 10 requests/second
    r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=30)
    r.raise_for_status()
    return r


def ticker_to_cik(ticker):
    data = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    for v in data.values():
        if v["ticker"].upper() == ticker.upper():
            return int(v["cik_str"]), v["title"]
    sys.exit(f"Ticker {ticker} not found in SEC company list (US-listed companies only).")


def load_facts(cik):
    return sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json").json()


# --------------------------------------------------------------------------
# Step 1 + 2: point-in-time selection (filed <= D) and statement parsing
# --------------------------------------------------------------------------
def _entries(facts, tag, unit="USD", taxonomy="us-gaap"):
    try:
        return facts["facts"][taxonomy][tag]["units"][unit]
    except KeyError:
        return []


def latest_annual_flow(facts, tags, D):
    """Income / cash-flow items: latest ~12-month value from a 10-K filed on or before D."""
    for tag in tags:
        cand = []
        for e in _entries(facts, tag):
            if e.get("form") not in ("10-K", "10-K/A") or e["filed"] > D or "start" not in e:
                continue
            days = (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days
            if 330 <= days <= 400:
                cand.append(e)
        if cand:
            b = max(cand, key=lambda e: (e["end"], e["filed"]))
            return b["val"], tag, b
    return None, None, None


def latest_instant(facts, tags, D, end=None):
    """Balance-sheet items: latest value from a 10-K/10-Q filed on or before D."""
    for tag in tags:
        cand = [e for e in _entries(facts, tag)
                if e.get("form") in ("10-K", "10-K/A", "10-Q") and e["filed"] <= D
                and "start" not in e and (end is None or e["end"] == end)]
        if cand:
            b = max(cand, key=lambda e: (e["end"], e["filed"]))
            return b["val"], tag, b
    return None, None, None


def shares_outstanding(facts, D):
    cand = [e for e in _entries(facts, "EntityCommonStockSharesOutstanding", "shares", "dei") if e["filed"] <= D]
    return max(cand, key=lambda e: (e["filed"], e["end"]))["val"] if cand else None


def parse_statements(facts, D):
    """Return dict of raw statement items + provenance (which tag / filing was used)."""
    src = {}

    def flow(name, tags):
        v, t, e = latest_annual_flow(facts, tags, D)
        src[name] = {"tag": t, "period_end": e["end"] if e else None, "filed": e["filed"] if e else None}
        return v

    # Income statement + cash-flow statement (annual, latest 10-K)
    inc = {
        "TotalRevenue": flow("TotalRevenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                              "SalesRevenueNet"]),
        "OperatingIncome": flow("OperatingIncome", ["OperatingIncomeLoss"]),
        "NetIncome": flow("NetIncome", ["NetIncomeLoss", "ProfitLoss"]),
    }
    cf = {
        "OperatingCashFlow": flow("OperatingCashFlow", ["NetCashProvidedByUsedInOperatingActivities"]),
        "CapitalExpenditure": flow("CapitalExpenditure", ["PaymentsToAcquirePropertyPlantAndEquipment",
                                                          "PaymentsToAcquireProductiveAssets"]),
    }

    # Balance sheet (latest 10-K or 10-Q); anchor everything to the equity balance date
    eq, t, e = latest_instant(facts, ["StockholdersEquity",
                                      "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], D)
    ref_end = e["end"] if e else None
    src["ShareholdersEquity"] = {"tag": t, "period_end": ref_end, "filed": e["filed"] if e else None}

    def inst(name, tags):
        v, t2, e2 = latest_instant(facts, tags, D, ref_end)
        src[name] = {"tag": t2, "period_end": e2["end"] if e2 else None, "filed": e2["filed"] if e2 else None}
        return v

    ca = inst("CurrentAssets", ["AssetsCurrent"])
    cl = inst("CurrentLiabilities", ["LiabilitiesCurrent"])

    lt_nc = inst("LongTermDebtNoncurrent", ["LongTermDebtNoncurrent"])
    lt_all = inst("LongTermDebt", ["LongTermDebt"])
    cur_debt = inst("DebtCurrent", ["DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper"])
    if lt_nc is not None:
        total_debt = lt_nc + (cur_debt or 0)
    elif lt_all is not None:  # LongTermDebt already includes the current portion
        total_debt = lt_all
    else:
        total_debt = cur_debt
    bs = {"ShareholdersEquity": eq, "CurrentAssets": ca, "CurrentLiabilities": cl, "TotalDebt": total_debt}
    return inc, bs, cf, src


# --------------------------------------------------------------------------
# Steps 3-4: ratios
# --------------------------------------------------------------------------
def div(a, b):
    return None if a is None or b in (None, 0) else a / b


def compute_ratios(inc, bs, cf, market_cap):
    fcf = None if cf["OperatingCashFlow"] is None else cf["OperatingCashFlow"] - (cf["CapitalExpenditure"] or 0)
    return {
        "MarketCap": market_cap,
        "PE_Ratio": div(market_cap, inc["NetIncome"]),
        "ROE": div(inc["NetIncome"], bs["ShareholdersEquity"]),
        "OperatingMargin": div(inc["OperatingIncome"], inc["TotalRevenue"]),
        "FreeCashFlow": fcf,
        "DebtToEquity": div(bs["TotalDebt"], bs["ShareholdersEquity"]),
        "CurrentRatio": div(bs["CurrentAssets"], bs["CurrentLiabilities"]),
    }


# --------------------------------------------------------------------------
# Step 5: Form 4 insider flow  (sum bought - sum sold, TxDate <= D)
# --------------------------------------------------------------------------
def net_insider_flow(cik, D, lookback_days=180, max_filings=40):
    sub = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
    lo = (pd.Timestamp(D) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    rows = [(f, a, p) for form, f, a, p in zip(sub["form"], sub["filingDate"], sub["accessionNumber"],
                                                sub["primaryDocument"]) if form == "4" and lo <= f <= D]
    rows = rows[:max_filings]
    bought = sold = 0.0
    n_tx = 0
    for filing_date, acc, doc in rows:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc.split('/')[-1]}"
        try:
            root = ET.fromstring(sec_get(url).content)
        except Exception:
            continue
        for t in root.findall(".//nonDerivativeTransaction"):
            code = t.findtext(".//transactionCode")
            tx_date = t.findtext(".//transactionDate/value", default="")[:10]
            if code not in ("P", "S") or (tx_date and tx_date > D):
                continue
            shares = float(t.findtext(".//transactionShares/value", default="0") or 0)
            n_tx += 1
            if code == "P":
                bought += shares
            else:
                sold += shares
    return {"shares_bought": bought, "shares_sold": sold, "net": bought - sold,
            "filings_scanned": len(rows), "open_market_transactions": n_tx, "lookback_days": lookback_days}


# --------------------------------------------------------------------------
# Step 6: rule-based stand-in for the Fundamentals Analyst LLM
# --------------------------------------------------------------------------
def interpret(r, insider):
    flags, score = [], 0

    def add(ok, msg):
        nonlocal score
        score += 1 if ok else -1
        flags.append(("[+] " if ok else "[-] ") + msg)

    if r["PE_Ratio"] is not None:
        if r["PE_Ratio"] < 0:
            add(False, "Negative earnings - P/E not meaningful")
        elif r["PE_Ratio"] > 35:
            add(False, f"Rich valuation (P/E {r['PE_Ratio']:.1f} > 35)")
        elif r["PE_Ratio"] < 20:
            add(True, f"Modest valuation (P/E {r['PE_Ratio']:.1f} < 20)")
        else:
            flags.append(f"[ ] Fair valuation (P/E {r['PE_Ratio']:.1f})")
    if r["ROE"] is not None:
        add(r["ROE"] >= 0.15, f"ROE {r['ROE']*100:.1f}% ({'strong' if r['ROE'] >= 0.15 else 'weak'}, threshold 15%)")
    if r["OperatingMargin"] is not None:
        add(r["OperatingMargin"] >= 0.15, f"Operating margin {r['OperatingMargin']*100:.1f}% (threshold 15%)")
    if r["FreeCashFlow"] is not None:
        add(r["FreeCashFlow"] > 0, f"Free cash flow {'positive' if r['FreeCashFlow'] > 0 else 'negative'}")
    if r["DebtToEquity"] is not None:
        add(r["DebtToEquity"] <= 1.5, f"Debt/Equity {r['DebtToEquity']:.2f} (threshold 1.5)")
    if r["CurrentRatio"] is not None:
        add(r["CurrentRatio"] >= 1.0, f"Current ratio {r['CurrentRatio']:.2f} (threshold 1.0)")
    if insider and insider.get("open_market_transactions"):
        add(insider["net"] > 0, f"Net insider open-market flow {insider['net']:+,.0f} shares")
    view = "Fundamentally strong" if score >= 3 else "Fundamentally weak / risky" if score <= -2 else "Mixed fundamentals"
    return score, view, flags


def optional_llm(report):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[llm] ANTHROPIC_API_KEY not set - skipping LLM synthesis")
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=900,
            messages=[{"role": "user", "content":
                       "You are a fundamentals analyst. Using ONLY these computed metrics, write a concise report "
                       "(valuation, profitability, cash generation, solvency, insider signal, risks). "
                       "Do not invent numbers.\n\n" + json.dumps(report, indent=2, default=str)}])
        return msg.content[0].text
    except Exception as e:  # noqa
        print(f"[llm] failed: {e}")
        return None


def get_price(ticker, D):
    try:
        import yfinance as yf
        df = yf.download(ticker, start=(pd.Timestamp(D) - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                         end=(pd.Timestamp(D) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                         auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[df.index <= pd.Timestamp(D)]
        return float(df["Close"].iloc[-1])
    except Exception as e:  # noqa
        print(f"[price] could not fetch price: {e}")
        return None


def fmt(v, kind="num"):
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v*100:.2f}%"
    if kind == "ratio":
        return f"{v:.2f}"
    return f"{v:,.0f}"


def main():
    ap = argparse.ArgumentParser(description="Algorithm 2: Fundamental Valuation & SEC EDGAR Parsing")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", required=True, help="Evaluation date D, YYYY-MM-DD")
    ap.add_argument("--price", type=float, help="Spot price override (skips Yahoo)")
    ap.add_argument("--shares", type=float, help="Shares outstanding override")
    ap.add_argument("--facts-json", help="Offline: path to a saved companyfacts JSON")
    ap.add_argument("--skip-insiders", action="store_true", help="Skip Form 4 download (faster)")
    ap.add_argument("--insider-days", type=int, default=180)
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--outdir", default="outputs")
    a = ap.parse_args()
    D = a.date

    # Step 1: filings database (companyfacts = parsed 10-K/10-Q XBRL), filtered by filing date <= D
    cik, name = None, a.ticker.upper()
    if a.facts_json:
        facts = json.loads(Path(a.facts_json).read_text())
        cik = facts.get("cik")
        name = facts.get("entityName", name)
    else:
        cik, name = ticker_to_cik(a.ticker)
        facts = load_facts(cik)
    print(f"Company: {name} (CIK {cik})")

    # Step 2: parse statements
    inc, bs, cf, src = parse_statements(facts, D)
    if all(v is None for v in list(inc.values()) + list(bs.values())):
        sys.exit("No usable filings found on/before D (company may file as foreign issuer, or D is too early).")

    # Market cap = spot price x shares outstanding
    price = a.price or get_price(a.ticker, D)
    shares = a.shares or shares_outstanding(facts, D)
    market_cap = price * shares if price and shares else None

    # Steps 3-4: ratios
    ratios = compute_ratios(inc, bs, cf, market_cap)

    # Step 5: insiders
    insider = None
    if not a.skip_insiders and cik:
        try:
            insider = net_insider_flow(cik, D, a.insider_days)
        except Exception as e:  # noqa
            print(f"[insiders] skipped: {e}")

    # Step 6: report
    score, view, flags = interpret(ratios, insider)
    report = {"ticker": a.ticker.upper(), "company": name, "cik": cik, "evaluation_date": D,
              "price_used": price, "shares_outstanding": shares,
              "statements": {"income": inc, "balance_sheet": bs, "cash_flow": cf},
              "ratios": ratios, "net_insider_flow": insider, "score": score, "view": view,
              "flags": flags, "data_provenance": src}
    if a.llm:
        report["llm_narrative"] = optional_llm(report)

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.ticker.upper()}_{D}_fundamental"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str))
    pd.DataFrame([{"metric": k, "value": v} for k, v in ratios.items()]).to_csv(out / f"{stem}_ratios.csv", index=False)

    md = [f"# Fundamentals Analyst Report - {name} ({a.ticker.upper()}), as of {D}", "",
          f"**View: {view}** (score {score:+d})", "", "## Ratios",
          f"- Market cap: {fmt(ratios['MarketCap'])}",
          f"- P/E: {fmt(ratios['PE_Ratio'], 'ratio')}",
          f"- ROE: {fmt(ratios['ROE'], 'pct')}",
          f"- Operating margin: {fmt(ratios['OperatingMargin'], 'pct')}",
          f"- Free cash flow: {fmt(ratios['FreeCashFlow'])}",
          f"- Debt/Equity: {fmt(ratios['DebtToEquity'], 'ratio')}",
          f"- Current ratio: {fmt(ratios['CurrentRatio'], 'ratio')}"]
    if insider:
        md.append(f"- Net insider flow ({insider['lookback_days']}d): {insider['net']:+,.0f} shares "
                  f"({insider['open_market_transactions']} open-market tx in {insider['filings_scanned']} Form 4s)")
    md += ["", "## Flags"] + [f"- {f}" for f in flags]
    md += ["", "## Source filings used"] + [
        f"- {k}: {v['tag']} (period end {v['period_end']}, filed {v['filed']})" for k, v in src.items() if v["tag"]]
    if report.get("llm_narrative"):
        md += ["", "## LLM narrative", report["llm_narrative"]]
    (out / f"{stem}.md").write_text("\n".join(md))
    print("\n".join(md))
    print(f"\nSaved to {out.resolve()}/{stem}.(json|md) and _ratios.csv")


if __name__ == "__main__":
    main()
