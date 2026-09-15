#!/usr/bin/env python3
"""
Send Significant Trades to Telegram

Finds rows in significant_trades where sent = false, sends each as a
Telegram message (same format as the original insider alert bot), and
marks the row sent = true only after a successful delivery — so a failed
send gets retried on the next run instead of being silently skipped.
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SOURCE_TABLE = os.environ.get("SOURCE_TABLE", "significant_trades")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [
    c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()
]

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


def fetch_unsent_rows():
    url = f"{SUPABASE_URL}/rest/v1/{SOURCE_TABLE}?select={SELECT_COLUMNS}&sent=is.false"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def mark_sent(accession_number, transaction_index):
    url = (
        f"{SUPABASE_URL}/rest/v1/{SOURCE_TABLE}"
        f"?accession_number=eq.{urllib.parse.quote(accession_number)}"
        f"&transaction_index=eq.{transaction_index}"
    )
    payload = json.dumps({"sent": True}).encode()
    req = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={**_headers(), "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def esc(text):
    """Escape text for Telegram HTML parse mode (only &, <, > are reserved)."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_market_cap(value):
    if value is None:
        return "N/A"
    value = float(value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def format_message(row):
    symbol = row.get("symbol") or "N/A"
    issuer_name = row.get("issuer_name") or ""
    owner_name = row.get("owner_name") or "Unknown"
    officer_title = row.get("officer_title") or ""
    category = (row.get("transaction_category") or "").title()
    price = row.get("price")
    value = row.get("value") or 0
    market_cap = row.get("market_cap")
    filing_url = row.get("filing_url") or ""

    role_str = esc(officer_title) if officer_title else "Insider"

    lines = [
        "🔔 <b>Insider Alert</b>",
        f"<b>{esc(symbol)}</b> ({esc(issuer_name)})",
        f"Market Cap: {esc(format_market_cap(market_cap))}",
        f"<b>Insider:</b> {esc(owner_name)} — {role_str}",
        f"{esc(category)} - @ ${esc(price)} (~${value:,.0f})",
        f'<a href="{esc(filing_url)}">View filing</a>',
    ]
    return "\n".join(lines)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram credentials not set — printing instead:\n", message)
        return False

    all_ok = True
    for chat_id in TELEGRAM_CHAT_IDS:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True}),
        }).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            print(f"Failed to send Telegram message to {chat_id}: HTTP {e.code} — {body}")
            all_ok = False
        except Exception as e:
            print(f"Failed to send Telegram message to {chat_id}: {e}")
            all_ok = False
    return all_ok


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase credentials not set — aborting.")
        return

    try:
        rows = fetch_unsent_rows()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Failed to query {SOURCE_TABLE}: HTTP {e.code} — {body}")
        return

    print(f"Found {len(rows)} unsent rows.")

    sent_count = 0
    for row in rows:
        message = format_message(row)
        ok = send_telegram(message)
        if ok:
            try:
                mark_sent(row["accession_number"], row["transaction_index"])
                sent_count += 1
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                print(f"Sent but failed to mark {row['accession_number']}#{row['transaction_index']} "
                      f"as sent: HTTP {e.code} — {body}")
        time.sleep(0.5)  # be gentle on Telegram's rate limit

    print(f"Sent and marked {sent_count}/{len(rows)} rows.")


if __name__ == "__main__":
    main()
