"""
strava_to_sheets.py
===================
Phase 1 — Strava → Google Sheets Sync
Patrick's AI Performance Coach ("The Patrick Edition")

Fetches yesterday's Strava activities (runs + tennis), enriches them with
lap-level data, and appends a flattened row to the `Workouts` Google Sheet tab.

Designed to run as a GitHub Actions cron job at 02:00 AEST daily.

Environment variables (stored as GitHub Actions Secrets):
  STRAVA_CLIENT_ID       — Strava API app client ID
  STRAVA_CLIENT_SECRET   — Strava API app client secret
  STRAVA_REFRESH_TOKEN   — Long-lived Strava refresh token (see setup below)
  GOOGLE_SERVICE_ACCOUNT — Full JSON content of the GCP service account key
  SPREADSHEET_ID         — Google Sheets document ID (from the URL)
"""

import os
import json
import math
import datetime
import time
import gspread
from google.oauth2.service_account import Credentials
import requests

# ─── Constants ───────────────────────────────────────────────────────────────

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"

# Google Sheets scopes required for read/write
GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WORKOUTS_TAB = "Workouts"   # Must match your Sheet tab name exactly

# ─── Strava Authentication ────────────────────────────────────────────────────

def get_strava_access_token() -> str:
    """
    Exchange the long-lived refresh token for a fresh short-lived access token.
    Strava tokens expire after 6 hours, so we always refresh at runtime.
    """
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    token_data = resp.json()
    print(f"[Strava] Access token acquired, expires at {token_data['expires_at']}")
    return token_data["access_token"]


def strava_get(endpoint: str, access_token: str, params: dict = None) -> dict | list:
    """Generic authenticated GET against the Strava v3 API."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(f"{STRAVA_API_BASE}{endpoint}", headers=headers, params=params or {})
    resp.raise_for_status()
    return resp.json()

# ─── Data Fetching ────────────────────────────────────────────────────────────

def get_yesterday_epoch_range() -> tuple[int, int]:
    """Return Unix timestamps for the start and end of yesterday (local midnight)."""
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    start_dt  = datetime.datetime.combine(yesterday, datetime.time.min)
    end_dt    = datetime.datetime.combine(yesterday, datetime.time.max)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def fetch_yesterday_activities(access_token: str) -> list[dict]:
    """Fetch all Strava activities recorded yesterday."""
    after, before = get_yesterday_epoch_range()
    activities = strava_get("/athlete/activities", access_token, params={
        "after":    after,
        "before":   before,
        "per_page": 30,
    })
    print(f"[Strava] Found {len(activities)} activities for yesterday.")
    return activities


def fetch_activity_laps(activity_id: int, access_token: str) -> list[dict]:
    """
    Fetch lap-level splits for a given activity.
    Returns a minimal list of dicts safe to serialise into a single Sheet cell.
    """
    laps_raw = strava_get(f"/activities/{activity_id}/laps", access_token)
    laps = []
    for lap in laps_raw:
        elapsed   = lap.get("elapsed_time", 0)           # seconds
        distance  = lap.get("distance", 0)               # metres
        avg_hr    = lap.get("average_heartrate")
        avg_watts = lap.get("average_watts")

        laps.append({
            "lap":      lap.get("lap_index", 0),
            "time":     format_duration(elapsed),
            "dist_m":   round(distance),
            "pace":     format_pace(elapsed, distance),  # min/km
            "avg_hr":   round(avg_hr)   if avg_hr   else None,
            "avg_w":    round(avg_watts) if avg_watts else None,
        })
    return laps


def fetch_full_activity(activity_id: int, access_token: str) -> dict:
    """Fetch the detailed activity object (includes GAP, effort, etc.)."""
    return strava_get(f"/activities/{activity_id}", access_token)

# ─── Formatting Helpers ───────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    """Convert seconds → 'H:MM:SS' or 'M:SS'."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_pace(elapsed_sec: int, distance_m: float) -> str | None:
    """Return pace as 'M:SS /km' or None if distance is zero."""
    if not distance_m or distance_m < 1:
        return None
    sec_per_km = (elapsed_sec / distance_m) * 1000
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d}"


def map_activity_type(strava_type: str) -> str:
    """
    Normalise Strava's verbose sport types → our Sheet vocabulary.
    Strava v3 uses 'sport_type' (e.g. 'TrailRun', 'Run', 'Tennis').
    """
    mapping = {
        "Run":           "Run",
        "TrailRun":      "Run",
        "VirtualRun":    "Run",
        "Tennis":        "Tennis",
        "Workout":       "Workout",
        "Ride":          "Ride",
        "VirtualRide":   "Ride",
        "Walk":          "Walk",
        "Hike":          "Hike",
    }
    return mapping.get(strava_type, strava_type)


def extract_gap(detailed_activity: dict) -> str | None:
    """
    Grade Adjusted Pace — Strava exposes this as 'average_grade_adjusted_speed'
    in m/s on the detailed activity object (if power meter or Premium athlete).
    Falls back to None gracefully.
    """
    gap_ms = detailed_activity.get("average_grade_adjusted_speed")  # m/s
    if gap_ms and gap_ms > 0:
        sec_per_km = 1000 / gap_ms
        m = int(sec_per_km // 60)
        s = int(sec_per_km % 60)
        return f"{m}:{s:02d}"
    return None

# ─── Google Sheets Auth & Write ───────────────────────────────────────────────

def get_gspread_client() -> gspread.Client:
    """
    Authenticate with Google Sheets using the service account JSON stored
    as a GitHub Secret (GOOGLE_SERVICE_ACCOUNT env var).
    """
    sa_json   = os.environ["GOOGLE_SERVICE_ACCOUNT"]
    sa_info   = json.loads(sa_json)
    creds     = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    client    = gspread.authorize(creds)
    return client


def get_workouts_sheet(client: gspread.Client) -> gspread.Worksheet:
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    wb = client.open_by_key(spreadsheet_id)
    try:
        ws = wb.worksheet(WORKOUTS_TAB)
    except gspread.WorksheetNotFound:
        # Create the tab if it doesn't exist yet, and write headers
        ws = wb.add_worksheet(title=WORKOUTS_TAB, rows=5000, cols=20)
        ws.append_row([
            "Activity_ID", "Date", "Name", "Type",
            "Distance_km", "Duration", "Elevation_Gain",
            "Avg_HR", "Max_HR", "Calories",
            "Relative_Effort", "GAP", "Avg_Pace",
            "Lap_Data",
        ])
        print(f"[Sheets] Created new '{WORKOUTS_TAB}' tab with headers.")
    return ws


def build_row(activity: dict, detailed: dict, laps: list[dict]) -> list:
    """
    Compose a single flat row matching the Workouts tab schema.

    Lap data is JSON-stringified into a single cell — this keeps the sheet
    flat so Gemini can parse it as a table, while preserving interval detail.
    """
    date_str  = activity.get("start_date_local", "")[:10]  # YYYY-MM-DD
    dist_km   = round(activity.get("distance", 0) / 1000, 2)
    duration  = format_duration(activity.get("moving_time", 0))
    elev      = round(activity.get("total_elevation_gain", 0))
    avg_hr    = activity.get("average_heartrate")
    max_hr    = activity.get("max_heartrate")
    calories  = detailed.get("calories") or detailed.get("kilojoules")
    effort    = activity.get("suffer_score")            # Strava Relative Effort
    gap       = extract_gap(detailed)
    avg_pace  = format_pace(activity.get("moving_time", 0), activity.get("distance", 0))
    lap_json  = json.dumps(laps, separators=(",", ":"))  # compact JSON → single cell

    return [
        activity["id"],
        date_str,
        activity.get("name", ""),
        map_activity_type(activity.get("sport_type", activity.get("type", ""))),
        dist_km,
        duration,
        elev,
        round(avg_hr) if avg_hr else "",
        round(max_hr) if max_hr else "",
        round(calories) if calories else "",
        effort if effort else "",
        gap if gap else "",
        avg_pace if avg_pace else "",
        lap_json,
    ]


def activity_already_logged(ws: gspread.Worksheet, activity_id: int) -> bool:
    """Check col A for the activity ID to avoid duplicate rows on re-runs."""
    ids = ws.col_values(1)   # All values in column 1 (Activity_ID)
    return str(activity_id) in ids

# ─── Main Orchestration ───────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Patrick's AI Coach — Strava → Sheets Sync")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    # 1. Auth
    access_token = get_strava_access_token()
    gc           = get_gspread_client()
    ws           = get_workouts_sheet(gc)

    # 2. Fetch yesterday's activities
    activities = fetch_yesterday_activities(access_token)

    if not activities:
        print("[Info] No activities found for yesterday. Exiting cleanly.")
        return

    # 3. Process each activity
    rows_added = 0
    for act in activities:
        activity_id = act["id"]

        if activity_already_logged(ws, activity_id):
            print(f"[Skip] Activity {activity_id} already in sheet.")
            continue

        print(f"[Processing] {act.get('name')} ({activity_id}) — {act.get('sport_type')}")

        # Fetch detailed activity (for GAP, calories, etc.)
        detailed = fetch_full_activity(activity_id, access_token)

        # Fetch laps (only meaningful for runs; still safe for other types)
        laps = fetch_activity_laps(activity_id, access_token)
        print(f"  → {len(laps)} laps fetched")

        # Build and append row
        row = build_row(act, detailed, laps)
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"  → Appended to '{WORKOUTS_TAB}' ✓")
        rows_added += 1

        # Be polite to both APIs — avoid rate limiting
        time.sleep(1.5)

    print(f"\n[Done] {rows_added} new row(s) added to '{WORKOUTS_TAB}'.")


if __name__ == "__main__":
    main()
