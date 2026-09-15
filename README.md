# Insider Trading Alert → Supabase (Free)

Same SEC EDGAR polling logic as the Telegram version, but instead of sending
messages, every filing gets inserted as a row (or rows — one per transaction
line) into a Supabase table.

## 1. Create the Supabase table

In your Supabase project → **Table Editor** → **+ New Table**:
- Name: `insider_alerts`
- Uncheck "Enable Row Level Security" (we write server-side with the service key)
- Columns (click **+ Add column** for each):

| Column | Type | Notes |
|---|---|---|
| `accession_number` | `text` | **Unique** ✅ — prevents duplicate rows |
| `symbol` | `text` | |
| `issuer_name` | `text` | |
| `issuer_cik` | `text` | |
| `owner_name` | `text` | |
| `roles` | `text` | comma-separated, e.g. `officer,director` |
| `officer_title` | `text` | |
| `transaction_code` | `text` | raw Form 4 code, e.g. `S`, `P`, `A` |
| `transaction_category` | `text` | `buy`, `sale`, `grant`, `exercise`, `gift`, `other` |
| `shares` | `numeric` | |
| `price` | `numeric` | |
| `value` | `numeric` | shares × price |
| `passed_filters` | `bool` | whether it matched your `config.json` rules |
| `filing_url` | `text` | link to the SEC filing index |
| `created_at` | `timestamptz` | default `now()` |

## 2. Get your Supabase credentials

Project → **Settings → API**:
- **Project URL** (`https://xxxxx.supabase.co`)
- **service_role key** (NOT the `anon` key — this one bypasses Row Level Security so the script can write from the server side)

## 3. Set up the repo

1. Create a new (private) GitHub repo, upload all files here (`alert.py`, `config.json`, `seen.json`, `.github/workflows/poll.yml`, this README)
2. **Settings → Secrets and variables → Actions**, add:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `SEC_USER_AGENT` — any descriptive string, e.g. `YourName your-email@example.com`

## 4. Run it

**Actions** tab → "Insider Trading Alert Poll (Supabase)" → **Run workflow**. It also runs automatically every 15 minutes via the schedule.

Check your Supabase Table Editor — new rows should appear for every filing processed.

## How deduplication works

- `seen.json` (committed back to the repo automatically) prevents re-processing
  the same SEC filing across runs — same mechanism as the Telegram version.
- The `accession_number` **unique constraint** in Supabase is a second safety
  net: even if a filing were somehow reprocessed, `Prefer: resolution=ignore-duplicates`
  in the insert request means Supabase silently skips the duplicate rather
  than erroring or creating a second row.

## Customizing filters

Same `config.json` format as the Telegram version — `exclude_symbols`,
`include_symbols`, and `rules` (OR logic across rule objects, each combining
`roles` / `transaction_types` / `min_transaction_value`). Filings that don't
pass your filters still get inserted (with `passed_filters: false`) so you
have a complete record — you can query only the ones that passed with:

```sql
select * from insider_alerts where passed_filters = true order by created_at desc;
```

## One row per transaction line

A single Form 4 filing can report multiple transactions (e.g. an option
exercise followed by a sale). Each transaction line becomes its own row,
all sharing the same `accession_number`, so you can filter/aggregate at the
transaction level in SQL.
