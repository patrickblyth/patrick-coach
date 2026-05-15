"""
garmin_to_sheets.py — Daily Garmin → Google Sheets sync.
Fetches yesterday's wellness data and upserts a row in the Daily_Stats tab.

Data pulled:
  - HRV status + overnight average (from get_hrv_data)
  - Sleep score + duration in hours (from get_sleep_data)
  - Resting heart rate (from get_rhr_day)
  - Body battery at start and end of day (from get_body_battery)
  - Average stress score (from get_stress_data)
  - Weight in kg (from get_body_composition — Garmin scale sync)

Safe to re-run: if a row for yesterday already exists it is updated in-place,
not duplicated. This means triggering twice in a day is harmless.

Environment variables required (store as GitHub Secrets):
  GARMIN_EMAIL
  GARMIN_PASSWORD
  GOOGLE_SERVICE_ACCOUNT  — same service account JSON as Strava sync
  SPREADSHEET_ID          — same sheet ID as Strava sync
"""

import os
import json
import datetime
import time
import gspread
import garminconnect
from google.oauth2.service_account import Credentials

# ─── Constants ────────────────────────────────────────────────────────────────

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DAILY_STATS_TAB = "Daily_Stats"

HEADERS = [
    "Date",
    "HRV_Status",
    "HRV_Overnight_Avg_ms",
    "Sleep_Score",
    "Sleep_Duration_hrs",
    "Resting_HR",
    "Body_Battery_Start",
    "Body_Battery_End",
    "Stress_Score",
    "Weight_kg",
]

# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_garmin_client() -> garminconnect.Garmin:
    """
    Authenticate with Garmin Connect using email + password.
    Compatible with garminconnect 0.2.x and 0.3.x.

    0.3.x changed the constructor signature — tokenstore is now the first
    positional arg. We pass it as None to force a fresh credential login,
    which is correct for a stateless GitHub Actions runner (no token cache).
    """
    email    = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    # 0.3.x signature: Garmin(email, password) — same as 0.2.x on the surface,
    # but the third positional arg is is_cn (bool), not tokenstore.
    # Passing None caused "is_cn must be a boolean". Just use keyword args to be safe.
    client = garminconnect.Garmin(email=email, password=password)
    client.login()
    print("[Garmin] Authenticated successfully.")
    return client


def get_gspread_client() -> gspread.Client:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    return gspread.authorize(creds)


def get_daily_stats_sheet(gc: gspread.Client) -> gspread.Worksheet:
    """Open or create the Daily_Stats tab with correct headers."""
    wb = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    try:
        ws = wb.worksheet(DAILY_STATS_TAB)
        print(f"[Sheets] Opened existing tab: {DAILY_STATS_TAB}")
        return ws
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=DAILY_STATS_TAB, rows=2000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        print(f"[Sheets] Created new tab: {DAILY_STATS_TAB}")
        return ws

# ─── Garmin data fetchers ─────────────────────────────────────────────────────

def fetch_hrv(client: garminconnect.Garmin, date_str: str) -> tuple[str, int | None]:
    """
    Returns (hrv_status, hrv_overnight_avg_ms).
    Status is one of: 'Balanced', 'Low', 'Poor', 'Unbalanced', or '' if unavailable.
    """
    try:
        data    = client.get_hrv_data(date_str)
        # Log raw structure so we can see exactly what Garmin returned
        print(f"  [HRV] raw keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        summary = data.get("hrvSummary", {}) if isinstance(data, dict) else {}
        if not summary:
            # Some firmware versions return a flat structure rather than nested
            summary = data

        # Status: try multiple known key names across firmware versions
        status_str = (
            summary.get("status") or
            summary.get("hrvStatus") or
            summary.get("lastNightStatus") or
            ""
        )
        # Overnight avg HRV in ms: try multiple known keys
        overnight = (
            summary.get("lastNight") or
            summary.get("lastNightAvg") or
            summary.get("hrvLastNight") or
            None
        )
        print(f"  [HRV] status={status_str!r}  overnight_avg={overnight}ms")
        return str(status_str) if status_str else "", overnight
    except Exception as e:
        print(f"  [HRV] Unavailable: {e}")
        return "", None


def fetch_sleep(client: garminconnect.Garmin, date_str: str) -> tuple[int | None, float | None]:
    """
    Returns (sleep_score, sleep_duration_hrs).
    Duration is derived from total sleep seconds → rounded to 2dp.
    """
    try:
        data          = client.get_sleep_data(date_str)
        daily         = data.get("dailySleepDTO", {})
        score         = daily.get("sleepScores", {}).get("overall", {}).get("value")
        total_seconds = daily.get("sleepTimeSeconds")
        duration_hrs  = round(total_seconds / 3600, 2) if total_seconds else None
        print(f"  [Sleep] score={score}  duration={duration_hrs}hrs")
        return score, duration_hrs
    except Exception as e:
        print(f"  [Sleep] Unavailable: {e}")
        return None, None


def fetch_rhr(client: garminconnect.Garmin, date_str: str) -> int | None:
    """Returns resting heart rate as an integer, or None."""
    try:
        data = client.get_rhr_day(date_str)
        # Log raw structure to diagnose key names
        print(f"  [RHR] raw: {str(data)[:200]}")
        if isinstance(data, list) and data:
            entry = data[0]
            rhr = (entry.get("value") or
                   entry.get("restingHeartRate") or
                   entry.get("rhr"))
        elif isinstance(data, dict):
            rhr = (data.get("restingHeartRate") or
                   data.get("value") or
                   data.get("rhr") or
                   # Some versions nest under allMetrics
                   (data.get("allMetrics", {}) or {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value"))
        else:
            rhr = None
        rhr_int = int(rhr) if rhr else None
        print(f"  [RHR] {rhr_int} bpm")
        return rhr_int
    except Exception as e:
        print(f"  [RHR] Unavailable: {e}")
        return None


def fetch_body_battery(client: garminconnect.Garmin, date_str: str) -> tuple[int | None, int | None]:
    """
    Returns (battery_start, battery_end) for the day.

    battery_start — first reading after midnight, reflecting how charged you
                    woke up after sleep.
    battery_end   — last reading before midnight, reflecting how depleted you
                    were by end of day. The difference (start - end) is a proxy
                    for how taxing the day was; Gemini can derive this on the fly.

    get_body_battery accepts [start_date, end_date].
    Each entry in the returned list has a 'bodyBatteryValuesArray' of
    [timestamp_ms, value] pairs covering that date.
    """
    try:
        data = client.get_body_battery(date_str, date_str)
        if not data:
            print("  [Body Battery] No data returned.")
            return None, None
        readings = data[0].get("bodyBatteryValuesArray", [])
        if not readings:
            print("  [Body Battery] Empty readings array.")
            return None, None
        # Pull value from [timestamp_ms, value] pairs, skipping any None values
        values = [r[1] for r in readings if len(r) > 1 and r[1] is not None]
        if not values:
            print("  [Body Battery] All readings were null.")
            return None, None
        start = int(values[0])
        end   = int(values[-1])
        print(f"  [Body Battery] Start={start}  End={end}  Drain={start - end}")
        return start, end
    except Exception as e:
        print(f"  [Body Battery] Unavailable: {e}")
        return None, None


def fetch_stress(client: garminconnect.Garmin, date_str: str) -> int | None:
    """
    Returns average stress score for the day (0–100), or None.
    Garmin's stress API returns readings every 3 minutes; we average the valid ones.
    Readings of -1 or -2 mean 'unavailable' (off-wrist) and are excluded.
    """
    try:
        data = client.get_stress_data(date_str)
        readings = data.get("stressValuesArray", [])
        valid = [r[1] for r in readings if isinstance(r, list) and len(r) > 1 and r[1] >= 0]
        if not valid:
            print("  [Stress] No valid readings.")
            return None
        avg = round(sum(valid) / len(valid))
        print(f"  [Stress] Avg={avg} (from {len(valid)} readings)")
        return avg
    except Exception as e:
        print(f"  [Stress] Unavailable: {e}")
        return None


def fetch_weight(client: garminconnect.Garmin, date_str: str) -> float | None:
    """
    Returns weight in kg from Garmin scale sync, or None if not weighed that day.
    get_body_composition returns the most recent weigh-in within the date range.
    We check the date matches to avoid carrying forward a stale reading.
    """
    try:
        data     = client.get_body_composition(date_str, date_str)
        entries  = data.get("totalAverage", {})  # Can also be in dateWeightList
        # Primary path: dateWeightList contains per-day entries
        date_list = data.get("dateWeightList", [])
        for entry in date_list:
            if entry.get("calendarDate") == date_str:
                weight_g = entry.get("weight")  # Garmin stores in grams
                if weight_g:
                    kg = round(weight_g / 1000, 2)
                    print(f"  [Weight] {kg} kg")
                    return kg
        print("  [Weight] No weigh-in recorded for this date.")
        return None
    except Exception as e:
        print(f"  [Weight] Unavailable: {e}")
        return None

# ─── Sheet upsert ─────────────────────────────────────────────────────────────

def find_existing_row(ws: gspread.Worksheet, date_str: str) -> int | None:
    """
    Returns the 1-based row index of an existing row for date_str, or None.
    Column A contains date strings like '2024-11-01'.
    """
    dates = ws.col_values(1)  # includes header
    for i, val in enumerate(dates, start=1):
        if val == date_str:
            return i
    return None


def upsert_row(ws: gspread.Worksheet, row_data: list, date_str: str):
    """
    If a row for this date already exists, update it in-place.
    Otherwise append a new row. Ensures no duplicates on re-runs.
    """
    existing_row = find_existing_row(ws, date_str)
    if existing_row:
        # Update all columns in the existing row
        col_count = len(row_data)
        cell_range = f"A{existing_row}:{chr(64 + col_count)}{existing_row}"
        ws.update(cell_range, [row_data], value_input_option="USER_ENTERED")
        print(f"[Sheets] Updated existing row {existing_row} for {date_str} ✓")
    else:
        ws.append_row(row_data, value_input_option="USER_ENTERED")
        print(f"[Sheets] Appended new row for {date_str} ✓")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Patrick's AI Coach — Garmin → Sheets Sync")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    # Yesterday's date in Melbourne local time (AEST = UTC+10, AEDT = UTC+11).
    # GitHub Actions runs UTC, so we must convert explicitly — otherwise at
    # 02:30 AEST on May 16th, UTC is still May 15th and "yesterday UTC" = May 14th,
    # which is two days behind the correct Melbourne date of May 15th.
    # We use the zoneinfo module (stdlib in Python 3.9+) for correct DST handling
    # so the date is right year-round across the AEST/AEDT boundary.
    try:
        from zoneinfo import ZoneInfo
        melb_tz = ZoneInfo("Australia/Melbourne")
    except ImportError:
        # Fallback for older Pythons — fixed +10 offset (close enough at 02:30)
        melb_tz = datetime.timezone(datetime.timedelta(hours=10))
    melb_now  = datetime.datetime.now(melb_tz)
    yesterday = (melb_now.date() - datetime.timedelta(days=1)).isoformat()
    print(f"\nFetching Garmin data for: {yesterday}\n")

    # Auth
    garmin = get_garmin_client()
    gc     = get_gspread_client()
    ws     = get_daily_stats_sheet(gc)

    # Small sleep after login — Garmin's servers occasionally need a moment
    time.sleep(2)

    # Fetch all data (each call is independent; failures don't block others)
    print("\n[Fetching Garmin data]")
    hrv_status, hrv_avg    = fetch_hrv(garmin, yesterday)
    sleep_score, sleep_hrs = fetch_sleep(garmin, yesterday)
    rhr                    = fetch_rhr(garmin, yesterday)
    bb_start, bb_end       = fetch_body_battery(garmin, yesterday)
    stress                 = fetch_stress(garmin, yesterday)
    weight                 = fetch_weight(garmin, yesterday)

    # Build row (order must match HEADERS)
    row = [
        yesterday,
        hrv_status  if hrv_status  else "",
        hrv_avg     if hrv_avg     else "",
        sleep_score if sleep_score else "",
        sleep_hrs   if sleep_hrs   else "",
        rhr         if rhr         else "",
        bb_start    if bb_start    is not None else "",
        bb_end      if bb_end      is not None else "",
        stress      if stress      else "",
        weight      if weight      else "",
    ]

    print(f"\n[Row to write] {row}")

    # Upsert into sheet
    print()
    upsert_row(ws, row, yesterday)

    print("\n" + "=" * 60)
    print("Garmin sync complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
