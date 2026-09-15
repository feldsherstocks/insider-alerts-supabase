#!/usr/bin/env python3
"""
Insider Trading Alert Bot — Supabase version

Polls SEC EDGAR's live Form 4 (insider trading) feed, fetches the detailed
XML for each new filing, applies your filters from config.json, and inserts
every new filing into a Supabase table (instead of sending Telegram messages).

Free data source: SEC EDGAR "getcurrent" Atom feed + per-filing Form 4 XML.
Storage: Supabase (Postgres) via its REST API.
"""

import os
import json
import time
import re
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import urllib.error

# ---- Configuration (env vars / GitHub secrets) ----
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "insider_alerts")

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "InsiderAlertBot your-email@example.com")

FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count=100&output=atom"
)

BASE_DIR = os.path.dirname(__file__)
STATE_FILE = os.path.join(BASE_DIR, "seen.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Form 4 transaction codes -> friendly categories
TRANSACTION_CODE_MAP = {
    "P": "buy",       # Open market or private purchase
    "S": "sale",      # Open market or private sale
    "A": "grant",     # Grant, award, or other acquisition
    "M": "exercise",  # Exercise or conversion of derivative
    "G": "gift",      # Gift
    "F": "other",     # Payment of exercise price/tax via withholding
    "C": "exercise",  # Conversion of derivative
    "D": "other",     # Disposition to the issuer
    "X": "exercise",  # Exercise of in-the-money option
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_seen(seen):
    trimmed = list(seen)[-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_feed():
    return http_get(FEED_URL)


def parse_feed_entries(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = root.findall("atom:entry", ATOM_NS)

    filings = {}
    for entry in entries:
        entry_id = entry.find("atom:id", ATOM_NS).text
        acc_no = entry_id.split("=")[-1]
        link = entry.find("atom:link", ATOM_NS).get("href")
        updated = entry.find("atom:updated", ATOM_NS).text
        if acc_no not in filings:
            filings[acc_no] = {"acc_no": acc_no, "index_url": link, "updated": updated}
    return list(filings.values())


def find_form4_xml_url(index_url):
    html = http_get(index_url).decode("utf-8", errors="ignore")
    candidates = re.findall(r'href="([^"]+\.xml)"', html)
    plain = [c for c in candidates if "/xslF345" not in c]
    if not plain:
        return None
    path = plain[0]
    if path.startswith("http"):
        return path
    return "https://www.sec.gov" + path


def parse_form4_xml(xml_bytes):
    root = ET.fromstring(xml_bytes)

    def text(elem, path, default=""):
        if elem is None:
            return default
        node = elem.find(path)
        return node.text.strip() if node is not None and node.text else default

    issuer_symbol = text(root, "issuer/issuerTradingSymbol")
    issuer_name = text(root, "issuer/issuerName")
    issuer_cik = text(root, "issuer/issuerCik")
    owner_name = text(root, "reportingOwner/reportingOwnerId/rptOwnerName")

    rel = root.find("reportingOwner/reportingOwnerRelationship")
    roles = []
    if rel is not None:
        if text(rel, "isDirector") == "1":
            roles.append("director")
        if text(rel, "isOfficer") == "1":
            roles.append("officer")
        if text(rel, "isTenPercentOwner") == "1":
            roles.append("ten_percent_owner")
        if text(rel, "isOther") == "1":
            roles.append("other")
    officer_title = text(rel, "officerTitle")

    transactions = []
    for table_tag in ("nonDerivativeTable", "derivativeTable"):
        table = root.find(table_tag)
        if table is None:
            continue
        for txn in table.findall("*[transactionCoding]"):
            code = text(txn, "transactionCoding/transactionCode")
            shares = text(txn, "transactionAmounts/transactionShares/value", "0")
            price = text(txn, "transactionAmounts/transactionPricePerShare/value", "0")
            ad_code = text(txn, "transactionAmounts/transactionAcquiredDisposedCode/value")
            try:
                value = float(shares) * float(price)
            except ValueError:
                value = 0.0
            transactions.append({
                "code": code,
                "category": TRANSACTION_CODE_MAP.get(code, "other"),
                "shares": shares,
                "price": price,
                "value": value,
                "acquired_or_disposed": ad_code,
            })

    return {
        "symbol": issuer_symbol,
        "issuer_name": issuer_name,
        "issuer_cik": issuer_cik,
        "owner_name": owner_name,
        "roles": roles,
        "officer_title": officer_title,
        "transactions": transactions,
    }


def _rule_matches(detail, rule):
    role_filter = set(rule.get("roles", []))
    txn_type_filter = set(rule.get("transaction_types", []))
    min_value = rule.get("min_transaction_value", 0)

    if role_filter and not (role_filter & set(detail["roles"])):
        return False

    if not detail["transactions"]:
        return not txn_type_filter and min_value == 0

    matching_txns = detail["transactions"]
    if txn_type_filter:
        matching_txns = [t for t in matching_txns if t["category"] in txn_type_filter]
        if not matching_txns:
            return False
    if min_value:
        matching_txns = [t for t in matching_txns if t["value"] >= min_value]
        if not matching_txns:
            return False

    return True


def passes_filters(detail, config):
    symbol = (detail["symbol"] or "").upper()

    exclude_symbols = {s.upper() for s in config.get("exclude_symbols", [])}
    include_symbols = {s.upper() for s in config.get("include_symbols", [])}

    if symbol in exclude_symbols:
        return False
    if include_symbols and symbol not in include_symbols:
        return False

    rules = config.get("rules")
    if rules:
        return any(_rule_matches(detail, rule) for rule in rules)

    flat_rule = {
        "roles": config.get("roles", []),
        "transaction_types": config.get("transaction_types", []),
        "min_transaction_value": config.get("min_transaction_value", 0),
    }
    return _rule_matches(detail, flat_rule)


def build_rows(detail, filing, passed):
    """
    One filing can have multiple transaction lines; we insert one row per
    transaction line so amounts/prices/shares are queryable individually.
    transaction_index (0, 1, 2...) plus accession_number forms the composite
    primary key, since a single filing can have several transactions.
    """
    roles_str = ",".join(detail["roles"])
    base = {
        "accession_number": filing["acc_no"],
        "symbol": detail["symbol"] or None,
        "issuer_name": detail["issuer_name"] or None,
        "issuer_cik": detail["issuer_cik"] or None,
        "owner_name": detail["owner_name"] or None,
        "roles": roles_str or None,
        "officer_title": detail["officer_title"] or None,
        "filing_url": filing["index_url"],
        "passed_filters": passed,
    }

    if not detail["transactions"]:
        return [dict(base, transaction_index=0, transaction_code=None,
                     transaction_category=None, shares=None, price=None, value=None)]

    rows = []
    for i, t in enumerate(detail["transactions"]):
        rows.append(dict(
            base,
            transaction_index=i,
            transaction_code=t["code"],
            transaction_category=t["category"],
            shares=float(t["shares"]) if t["shares"] else None,
            price=float(t["price"]) if t["price"] else None,
            value=t["value"],
        ))
    return rows


def insert_to_supabase(rows):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase credentials not set — printing rows instead:")
        for row in rows:
            print(json.dumps(row))
        return

    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=accession_number,transaction_index"
    payload = json.dumps(rows).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            # Ignore rows that violate the unique accession_number constraint
            # (i.e. we've already inserted this filing before) instead of erroring.
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Failed to insert into Supabase: HTTP {e.code} — {body}")
    except Exception as e:
        print(f"Failed to insert into Supabase: {e}")


def main():
    config = load_json(CONFIG_FILE, {})
    seen = set(load_json(STATE_FILE, []))

    filings = parse_feed_entries(fetch_feed())
    new_filings = [f for f in filings if f["acc_no"] not in seen]

    inserted_count = 0
    for filing in reversed(new_filings):  # oldest first
        seen.add(filing["acc_no"])

        try:
            xml_url = find_form4_xml_url(filing["index_url"])
            if not xml_url:
                continue
            detail = parse_form4_xml(http_get(xml_url))
        except (urllib.error.URLError, ET.ParseError) as e:
            print(f"Skipping {filing['acc_no']} — failed to fetch/parse detail: {e}")
            continue

        passed = passes_filters(detail, config)
        rows = build_rows(detail, filing, passed)
        insert_to_supabase(rows)
        inserted_count += len(rows)

        time.sleep(0.3)  # be polite to SEC's servers

    save_seen(seen)
    print(f"Checked feed: {len(filings)} filings, {len(new_filings)} new, "
          f"{inserted_count} rows inserted into Supabase.")


if __name__ == "__main__":
    main()
