"""
strava_backfill.py
==================
One-time historical backfill — Strava → Google Sheets
Patrick's AI Performance Coach

Fetches ALL historical Strava activities and appends them to the Workouts tab.
- Skips activities already in the sheet (safe to re-run)
- Respects Strava's rate limit (100 requests / 15 min)
- Processes oldest-first so the sheet fills in chronological order
- Logs progress so you can see where it's up to

Run manually from GitHub Actions (workflow_dispatch) — NOT on a cron schedule.
Uses the same environment variables as strava_to_sheets.py.

Strava API rate limits:
  - 100 requests per 15 minutes
  - 1000 requests per day
  For large histories, the script will pause automatically when nearing limits.
"""

import os
import json
import time
import datetime
import gspread
from google.oauth2.service_account import Credentials
import requests

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WORKOUTS_TAB = "Workouts"

# ─── Rate limit tracking ──────────────────────────────────────────────────────
# Strava returns rate limit headers on every response.
# We track them and pause proactively before hitting the wall.

class RateLimiter:
    def __init__(self):
        self.requests_this_window = 0
        self.window_start = time.time()

    def update(self, headers: dict):
        usage = headers.get("X-RateLimit-Usage", "")
        if usage:
            parts = usage.split(",")
            if parts:
                self.requests_this_window = int(parts[0].strip())

    def wait_if_needed(self):
        # If we've used 85+ of the 100 per-15-min limit, pause until the window resets
        if self.requests_this_window >= 85:
            elapsed = time.time() - self.window_start
            wait = max(0, 910 - elapsed)  # 15 min + 10s buffer
            if wait > 0:
                print(f"[Rate limit] Approaching limit ({self.requests_this_window}/100). "
                      f"Pausing {wait:.0f}s until window resets...")
                time.sleep(wait)
            self.requests_this_window = 0
            self.window_start = time.time()

rate_limiter = RateLimiter()

# ─── Auth (identical to daily script) ─────────────────────────────────────────

def get_strava_access_token() -> str:
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_gspread_client() -> gspread.Client:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    return gspread.authorize(creds)


def get_workouts_sheet(client: gspread.Client) -> gspread.Worksheet:
    wb = client.open_by_key(os.environ["SPREADSHEET_ID"])
    return wb.worksheet(WORKOUTS_TAB)

# ─── Strava helpers (identical to daily script) ───────────────────────────────

def strava_get(endpoint: str, access_token: str, params: dict = None):
    headers = {"Authorization": f"Bearer {access_token}"}
    resp    = requests.get(f"{STRAVA_API_BASE}{endpoint}", headers=headers, params=params or {})
    resp.raise_for_status()
    rate_limiter.update(resp.headers)
    return resp.json()


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_pace(elapsed_sec: int, distance_m: float) -> str | None:
    if not distance_m or distance_m < 1:
        return None
    sec_per_km = (elapsed_sec / distance_m) * 1000
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d}"


def map_activity_type(strava_type: str) -> str:
    mapping = {
        "Run": "Run", "TrailRun": "Run", "VirtualRun": "Run",
        "Tennis": "Tennis", "Workout": "Workout",
        "Ride": "Ride", "VirtualRide": "Ride",
        "Walk": "Walk", "Hike": "Hike",
    }
    return mapping.get(strava_type, strava_type)


def extract_gap(detailed: dict) -> str | None:
    gap_ms = detailed.get("average_grade_adjusted_speed")
    if gap_ms and gap_ms > 0:
        sec_per_km = 1000 / gap_ms
        return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"
    return None


def fetch_activity_laps(activity_id: int, access_token: str) -> list[dict]:
    laps_raw = strava_get(f"/activities/{activity_id}/laps", access_token)
    laps = []
    for lap in laps_raw:
        elapsed  = lap.get("elapsed_time", 0)
        distance = lap.get("distance", 0)
        avg_hr   = lap.get("average_heartrate")
        avg_w    = lap.get("average_watts")
        laps.append({
            "lap":    lap.get("lap_index", 0),
            "time":   format_duration(elapsed),
            "dist_m": round(distance),
            "pace":   format_pace(elapsed, distance),
            "avg_hr": round(avg_hr) if avg_hr else None,
            "avg_w":  round(avg_w)  if avg_w  else None,
        })
    return laps


def build_row(activity: dict, detailed: dict, laps: list[dict]) -> list:
    dist_km  = round(activity.get("distance", 0) / 1000, 2)
    avg_hr   = activity.get("average_heartrate")
    max_hr   = activity.get("max_heartrate")
    calories = detailed.get("calories") or detailed.get("kilojoules")
    effort   = activity.get("suffer_score")
    gap      = extract_gap(detailed)
    avg_pace = format_pace(activity.get("moving_time", 0), activity.get("distance", 0))

    return [
        activity["id"],
        activity.get("start_date_local", "")[:10],
        activity.get("name", ""),
        map_activity_type(activity.get("sport_type", activity.get("type", ""))),
        dist_km,
        format_duration(activity.get("moving_time", 0)),
        round(activity.get("total_elevation_gain", 0)),
        round(avg_hr)    if avg_hr    else "",
        round(max_hr)    if max_hr    else "",
        round(calories)  if calories  else "",
        effort           if effort    else "",
        gap              if gap       else "",
        avg_pace         if avg_pace  else "",
        json.dumps(laps, separators=(",", ":")),
    ]

# ─── Backfill logic ───────────────────────────────────────────────────────────

def fetch_all_activities(access_token: str) -> list[dict]:
    """
    Page through the full Strava activity history, oldest first.
    Strava returns max 200 per page.
    """
    all_activities = []
    page = 1
    print("[Strava] Fetching activity list (this may take a moment for large histories)...")

    while True:
        batch = strava_get("/athlete/activities", access_token, params={
            "per_page": 200,
            "page":     page,
        })

        if not batch:
            break

        all_activities.extend(batch)
        print(f"  Page {page}: {len(batch)} activities fetched (running total: {len(all_activities)})")
        page += 1

        # Small pause between pages to be a good API citizen
        time.sleep(1)

        if len(batch) < 200:
            break  # Last page

    # Reverse so we process oldest → newest (chronological order in sheet)
    all_activities.reverse()
    print(f"[Strava] {len(all_activities)} total activities found. Processing oldest-first.\n")
    return all_activities


def get_existing_ids(ws: gspread.Worksheet) -> set[str]:
    """Fetch all activity IDs already in the sheet for fast duplicate detection."""
    ids = ws.col_values(1)  # Column A
    return set(ids[1:])     # Skip header row


def main():
    print("=" * 60)
    print("Patrick's AI Coach — Historical Backfill")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    access_token = get_strava_access_token()
    gc           = get_gspread_client()
    ws           = get_workouts_sheet(gc)

    # Load all activities from Strava
    activities = fetch_all_activities(access_token)

    # Load existing IDs once (much faster than checking the sheet per row)
    existing_ids = get_existing_ids(ws)
    print(f"[Sheets] {len(existing_ids)} activities already in sheet — will skip these.\n")

    skipped   = 0
    added     = 0
    failed    = 0
    total     = len(activities)

    for i, act in enumerate(activities, 1):
        activity_id = str(act["id"])
        name        = act.get("name", "Untitled")
        date        = act.get("start_date_local", "")[:10]

        print(f"[{i}/{total}] {date} — {name}", end="")

        if activity_id in existing_ids:
            print(" → skipped (already in sheet)")
            skipped += 1
            continue

        try:
            # Fetch detailed activity + laps
            detailed = strava_get(f"/activities/{act['id']}", access_token)
            rate_limiter.wait_if_needed()

            laps = fetch_activity_laps(act["id"], access_token)
            rate_limiter.wait_if_needed()

            row = build_row(act, detailed, laps)
            ws.append_row(row, value_input_option="USER_ENTERED")

            existing_ids.add(activity_id)  # Update local set
            added += 1
            print(f" → added ({len(laps)} laps)")

            # Polite pause between activities — keeps us well under rate limits
            # and avoids hammering the Sheets API
            time.sleep(2)

        except Exception as e:
            print(f" → FAILED: {e}")
            failed += 1
            time.sleep(5)  # Longer pause after an error
            continue

    print("\n" + "=" * 60)
    print(f"Backfill complete.")
    print(f"  Added:   {added}")
    print(f"  Skipped: {skipped} (already existed)")
    print(f"  Failed:  {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()
