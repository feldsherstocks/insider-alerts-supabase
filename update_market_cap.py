#!/usr/bin/env python3
"""
Market Cap Updater

Scans the insider_alerts Supabase table for rows where market_cap is still
NULL, computes the market cap once per unique company (symbol + CIK), and
updates all matching rows in a single request per company.

Market cap = live stock price (Yahoo Finance) x shares outstanding (SEC's
free XBRL company facts API, using the issuer's CIK already stored in the
table).
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "insider_alerts")
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "InsiderAlertBot your-email@example.com")


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_rows_missing_market_cap():
    """Get distinct (symbol, issuer_cik) pairs where market_cap is NULL."""
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?select=symbol,issuer_cik&market_cap=is.null&symbol=not.is.null&issuer_cik=not.is.null"
    )
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = json.loads(resp.read())

    seen_pairs = set()
    unique_pairs = []
    for row in rows:
        pair = (row["symbol"], row["issuer_cik"])
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_pairs.append(pair)
    return unique_pairs


def _fetch_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["chart"]["result"][0]["meta"]["regularMarketPrice"]


def _fetch_shares_outstanding(cik):
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    facts = data.get("facts", {})
    candidates = [
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesIssued"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ]
    for taxonomy, tag in candidates:
        try:
            shares_facts = facts[taxonomy][tag]["units"]["shares"]
            most_recent = max(shares_facts, key=lambda x: x["end"])
            return most_recent["val"]
        except (KeyError, ValueError):
            continue
    return None


def compute_market_cap(symbol, cik):
    try:
        price = _fetch_price(symbol)
        shares = _fetch_shares_outstanding(cik)
        if shares is None:
            return None
        return price * shares
    except Exception as e:
        print(f"Could not compute market cap for {symbol}: {e}")
        return None


def update_market_cap(symbol, cik, market_cap):
    """Update every row for this company that still has a NULL market_cap."""
    url = (
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
        f"?symbol=eq.{urllib.parse.quote(symbol)}"
        f"&issuer_cik=eq.{urllib.parse.quote(str(cik))}"
        f"&market_cap=is.null"
    )
    payload = json.dumps({"market_cap": market_cap}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={**_headers(), "Prefer": "return=minimal"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Failed to update {symbol}: HTTP {e.code} — {body}")
        return False
    except Exception as e:
        print(f"Failed to update {symbol}: {e}")
        return False


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase credentials not set — aborting.")
        return

    pairs = fetch_rows_missing_market_cap()
    print(f"Found {len(pairs)} companies with missing market cap.")

    updated = 0
    for symbol, cik in pairs:
        market_cap = compute_market_cap(symbol, cik)
        if market_cap is None:
            print(f"Skipping {symbol} (CIK {cik}) — could not determine market cap.")
            continue
        if update_market_cap(symbol, cik, market_cap):
            print(f"Updated {symbol} (CIK {cik}) -> market_cap = {market_cap:,.0f}")
            updated += 1
        time.sleep(0.3)  # be polite to Yahoo/SEC

    print(f"Done. Updated {updated}/{len(pairs)} companies.")


if __name__ == "__main__":
    main()
