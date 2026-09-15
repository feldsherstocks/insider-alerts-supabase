#!/usr/bin/env python3
"""
Copy Significant Trades

Copies rows from insider_alerts into significant_trades where:
  reported = false AND market_cap > 50,000,000 AND (
    (transaction_code = 'S' AND value > 1,000,000) OR
    (transaction_code = 'P' AND value > 100,000)
  )

After a row is successfully copied, it's marked reported = true on the
source table so it's never picked up again. This flag-based approach
avoids a timing gap that a pure timestamp cursor would have: since
market_cap is filled in by a separate step sometime after a row is
inserted, a timestamp cursor could move past a row before its market_cap
was populated, permanently missing it. Filtering on reported = false has
no such gap — a row just waits until it qualifies, however long that takes.
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SOURCE_TABLE = os.environ.get("SOURCE_TABLE", "insider_alerts")
DEST_TABLE = os.environ.get("DEST_TABLE", "significant_trades")

SELECT_COLUMNS = (
    "accession_number,transaction_index,symbol,issuer_name,owner_name,"
    "officer_title,transaction_code,transaction_category,shares,price,"
    "value,market_cap,filing_url,created_at"
)


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_matching_rows():
    # PostgREST filter: top-level params are AND'd together; the "or" param
    # expresses the nested (S-sale-big OR P-buy-big) condition.
    or_filter = "or=(and(transaction_code.eq.S,value.gt.1000000),and(transaction_code.eq.P,value.gt.100000))"
    url = (
        f"{SUPABASE_URL}/rest/v1/{SOURCE_TABLE}"
        f"?select={SELECT_COLUMNS}"
        f"&reported=is.false"
        f"&market_cap=gt.50000000"
        f"&{or_filter}"
    )
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def insert_rows(rows):
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{DEST_TABLE}?on_conflict=accession_number,transaction_index"
    payload = json.dumps(rows).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={**_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def mark_reported(accession_number, transaction_index):
    url = (
        f"{SUPABASE_URL}/rest/v1/{SOURCE_TABLE}"
        f"?accession_number=eq.{urllib.parse.quote(accession_number)}"
        f"&transaction_index=eq.{transaction_index}"
    )
    payload = json.dumps({"reported": True}).encode()
    req = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={**_headers(), "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase credentials not set — aborting.")
        return

    try:
        rows = fetch_matching_rows()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Failed to query source table: HTTP {e.code} — {body}")
        return

    print(f"Found {len(rows)} unreported matching rows.")

    if not rows:
        return

    try:
        insert_rows(rows)
        print(f"Inserted {len(rows)} rows into {DEST_TABLE}.")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Failed to insert into {DEST_TABLE}: HTTP {e.code} — {body}")
        return  # don't mark anything reported if the copy failed

    marked = 0
    for row in rows:
        try:
            mark_reported(row["accession_number"], row["transaction_index"])
            marked += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            print(f"Failed to mark {row['accession_number']}#{row['transaction_index']} "
                  f"as reported: HTTP {e.code} — {body}")

    print(f"Marked {marked}/{len(rows)} rows as reported.")


if __name__ == "__main__":
    main()
