"""
garmin_backfill.py — Historical Garmin wellness backfill → Google Sheets
Patrick's AI Performance Coach

Fetches all daily wellness data from START_DATE to yesterday and upserts
into the Daily_Stats tab. Safe to re-run — existing rows are skipped.

Data per day:
  Sleep_Score | Sleep_Duration_hrs | Resting_HR |
  Body_Battery_High | Body_Battery_Low | Stress_Score | Weight_kg

Design decisions:
  - Oldest-first so the sheet fills chronologically
  - Each metric is fetched independently — a failure on one (e.g. body battery
    unavailable for an old date) doesn't block the others
  - Rows where ALL metrics are blank are still written with just the date,
    so the sheet has a complete date spine for joining with Strava data
  - Batches Sheets writes in groups of BATCH_SIZE to avoid timeouts
  - Sleeps between Garmin API calls to avoid soft rate-limit blocks
  - Safe to retrigger daily — already-written rows are skipped on the date check

Run manually via workflow_dispatch — NOT on a cron schedule.
Same environment variables as garmin_to_sheets.py.
"""

import os
import json
import datetime
import time
import gspread
import garminconnect
from google.oauth2.service_account import Credentials

# ─── Configuration ────────────────────────────────────────────────────────────

START_DATE  = datetime.date(2021, 1, 1)   # First date to backfill
BATCH_SIZE  = 25                           # Rows per Sheets API write call
API_SLEEP   = 1.2                          # Seconds between Garmin API calls
BATCH_SLEEP = 2.0                          # Seconds between Sheets batch writes

# ─── Constants ────────────────────────────────────────────────────────────────

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DAILY_STATS_TAB = "Daily_Stats"

HEADERS = [
    "Date",
    "Sleep_Score",
    "Sleep_Duration_hrs",
    "Resting_HR",
    "Body_Battery_High",
    "Body_Battery_Low",
    "Stress_Score",
    "Weight_kg",
]

# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_garmin_client() -> garminconnect.Garmin:
    email    = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    client   = garminconnect.Garmin(email=email, password=password)
    client.login()
    print("[Garmin] Authenticated successfully.")
    return client


def get_gspread_client() -> gspread.Client:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    return gspread.authorize(creds)


def get_daily_stats_sheet(gc: gspread.Client) -> gspread.Worksheet:
    """Open or create the Daily_Stats tab. Expands rows if needed."""
    wb = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    try:
        ws = wb.worksheet(DAILY_STATS_TAB)
        print(f"[Sheets] Opened existing tab: {DAILY_STATS_TAB}")
        # Ensure enough rows for the full backfill (~1,900 days from 2021)
        if ws.row_count < 2500:
            ws.add_rows(2500 - ws.row_count)
            print(f"[Sheets] Expanded to 2500 rows")
        return ws
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=DAILY_STATS_TAB, rows=2500, cols=len(HEADERS))
        ws.append_row(HEADERS)
        print(f"[Sheets] Created new tab: {DAILY_STATS_TAB}")
        return ws

# ─── Garmin fetchers (identical logic to garmin_to_sheets.py) ─────────────────

def fetch_sleep(client, date_str: str) -> tuple:
    try:
        data         = client.get_sleep_data(date_str)
        daily        = data.get("dailySleepDTO", {})
        score        = daily.get("sleepScores", {}).get("overall", {}).get("value")
        total_secs   = daily.get("sleepTimeSeconds")
        duration_hrs = round(total_secs / 3600, 2) if total_secs else None
        return score, duration_hrs
    except Exception:
        return None, None


def fetch_rhr(client, date_str: str):
    try:
        data = client.get_rhr_day(date_str)
        if isinstance(data, list) and data:
            entry = data[0]
            rhr = (entry.get("value") or entry.get("restingHeartRate") or entry.get("rhr"))
        elif isinstance(data, dict):
            rhr = (data.get("restingHeartRate") or data.get("value") or data.get("rhr") or
                   (data.get("allMetrics", {}) or {}).get("metricsMap", {})
                   .get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value"))
        else:
            rhr = None
        return int(rhr) if rhr else None
    except Exception:
        return None


def fetch_body_battery(client, date_str: str) -> tuple:
    try:
        data     = client.get_body_battery(date_str, date_str)
        if not data:
            return None, None
        readings = data[0].get("bodyBatteryValuesArray", [])
        values   = [r[1] for r in readings if len(r) > 1 and r[1] is not None]
        if not values:
            return None, None
        return int(max(values)), int(min(values))
    except Exception:
        return None, None


def fetch_stress(client, date_str: str):
    try:
        data     = client.get_stress_data(date_str)
        readings = data.get("stressValuesArray", [])
        valid    = [r[1] for r in readings if isinstance(r, list) and len(r) > 1 and r[1] >= 0]
        return round(sum(valid) / len(valid)) if valid else None
    except Exception:
        return None


def fetch_weight(client, date_str: str):
    try:
        data      = client.get_body_composition(date_str, date_str)
        date_list = data.get("dateWeightList", [])
        for entry in date_list:
            if entry.get("calendarDate") == date_str:
                weight_g = entry.get("weight")
                if weight_g:
                    return round(weight_g / 1000, 2)
        return None
    except Exception:
        return None

# ─── Sheets helpers ───────────────────────────────────────────────────────────

def get_existing_dates(ws: gspread.Worksheet) -> set:
    """Load all dates already in column A for fast duplicate detection."""
    dates = ws.col_values(1)
    return set(dates[1:])  # Skip header


def flush_batch(ws: gspread.Worksheet, batch: list) -> int:
    """Write a batch of rows with retry logic."""
    if not batch:
        return 0
    for attempt in range(3):
        try:
            ws.append_rows(batch, value_input_option="USER_ENTERED")
            return len(batch)
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  [Sheets] Write failed (attempt {attempt+1}/3): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    raise Exception(f"Sheets write failed after 3 attempts")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Patrick's AI Coach — Garmin Historical Backfill")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print(f"Date range: {START_DATE} → yesterday")
    print("=" * 60)

    # Build date range: START_DATE to yesterday Melbourne time
    try:
        from zoneinfo import ZoneInfo
        melb_tz = ZoneInfo("Australia/Melbourne")
    except ImportError:
        melb_tz = datetime.timezone(datetime.timedelta(hours=10))

    yesterday  = (datetime.datetime.now(melb_tz).date() - datetime.timedelta(days=1))
    all_dates  = []
    current    = START_DATE
    while current <= yesterday:
        all_dates.append(current)
        current += datetime.timedelta(days=1)

    total = len(all_dates)
    print(f"\n{total} days to process ({START_DATE} → {yesterday})\n")

    # Auth
    garmin = get_garmin_client()
    gc     = get_gspread_client()
    ws     = get_daily_stats_sheet(gc)
    time.sleep(2)

    # Load existing dates once for fast skip detection
    existing_dates = get_existing_dates(ws)
    print(f"[Sheets] {len(existing_dates)} dates already in sheet — will skip these.\n")

    added   = 0
    skipped = 0
    failed  = 0
    batch   = []

    for i, date in enumerate(all_dates, 1):
        date_str = date.isoformat()

        # Skip if already in sheet
        if date_str in existing_dates:
            skipped += 1
            if skipped % 50 == 0:
                print(f"  [{i}/{total}] Skipped {skipped} existing rows so far...")
            continue

        print(f"[{i}/{total}] {date_str}", end="")

        try:
            # Fetch each metric independently — failures on one don't block others
            sleep_score, sleep_hrs = fetch_sleep(garmin, date_str)
            time.sleep(API_SLEEP)

            rhr = fetch_rhr(garmin, date_str)
            time.sleep(API_SLEEP)

            bb_high, bb_low = fetch_body_battery(garmin, date_str)
            time.sleep(API_SLEEP)

            stress = fetch_stress(garmin, date_str)
            time.sleep(API_SLEEP)

            weight = fetch_weight(garmin, date_str)
            time.sleep(API_SLEEP)

            row = [
                date_str,
                sleep_score if sleep_score is not None else "",
                sleep_hrs   if sleep_hrs   is not None else "",
                rhr         if rhr         is not None else "",
                bb_high     if bb_high     is not None else "",
                bb_low      if bb_low      is not None else "",
                stress      if stress      is not None else "",
                weight      if weight      is not None else "",
            ]

            # Log summary — show which fields populated
            fields = []
            if sleep_score: fields.append(f"sleep={sleep_score}")
            if rhr:         fields.append(f"rhr={rhr}")
            if bb_high:     fields.append(f"bb={bb_high}/{bb_low}")
            if stress:      fields.append(f"stress={stress}")
            if weight:      fields.append(f"wt={weight}kg")
            print(f" → {', '.join(fields) if fields else 'no data'} [batch {len(batch)+1}/{BATCH_SIZE}]")

            batch.append(row)
            existing_dates.add(date_str)

            # Flush batch every BATCH_SIZE rows
            if len(batch) >= BATCH_SIZE:
                n = flush_batch(ws, batch)
                added += n
                batch  = []
                print(f"  [Sheets] Flushed {n} rows ✓")
                time.sleep(BATCH_SLEEP)

        except Exception as e:
            print(f" → FAILED: {e}")
            # Flush whatever we have before continuing
            if batch:
                added += flush_batch(ws, batch)
                batch = []
            failed += 1
            time.sleep(5)
            continue

    # Flush any remaining rows
    if batch:
        print(f"\n[Sheets] Flushing final batch of {len(batch)} rows...")
        added += flush_batch(ws, batch)

    print("\n" + "=" * 60)
    print("Garmin backfill complete.")
    print(f"  Added:   {added}")
    print(f"  Skipped: {skipped} (already existed)")
    print(f"  Failed:  {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()
