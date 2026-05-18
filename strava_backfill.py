"""
strava_backfill.py
==================
One-time historical backfill — Strava → Google Sheets
Patrick's AI Performance Coach

Fetches ALL historical Strava activities and appends them to the Workouts tab.
- Skips activities already in the sheet (safe to re-run)
- Respects Strava's rate limit (100 requests / 15 min, 1000 requests / day)
- Uses smart pagination — only fetches activities newer than most recent in sheet
- Skips laps fetch for non-run/ride/hike activities (saves API requests)
- Exits gracefully when daily limit is hit — no lost activities, clean rerun next day
- Processes oldest-first so the sheet fills in chronological order

Run manually from GitHub Actions (workflow_dispatch) — NOT on a cron schedule.
Retrigger daily until backfill is complete. Safe to rerun — skips existing activities.

Strava API rate limits:
  - 100 requests per 15 minutes
  - 1000 requests per day (resets midnight UTC)
  - Each activity costs 2 requests (detail + laps) for Run/Ride/Hike
  - Each activity costs 1 request (detail only) for Walk/Tennis/Workout/etc
  - Theoretical max per run: ~490 Run activities, or ~990 non-run activities
"""

import os
import json
import time
import datetime
import sys
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

# Activity types that have meaningful lap data worth fetching
LAP_ACTIVITY_TYPES = {"Run", "TrailRun", "VirtualRun", "Ride", "VirtualRide", "Hike"}

# ─── Rate limit tracking ──────────────────────────────────────────────────────
# Strava enforces two limits:
#   - 100 requests per 15 minutes  (short window)
#   - 1000 requests per day        (daily window)
# Headers returned on every response:
#   X-RateLimit-Limit:   "100,1000"
#   X-RateLimit-Usage:   "42,381"   ← 15-min used, daily used

class DailyLimitReached(Exception):
    """Raised when Strava's daily API limit is hit — triggers graceful exit."""
    pass


class RateLimiter:
    def __init__(self):
        self.short_used   = 0
        self.daily_used   = 0
        self.window_start = time.time()

    def update(self, headers: dict):
        usage = headers.get("X-RateLimit-Usage", "")
        if usage and "," in usage:
            parts = usage.split(",")
            self.short_used = int(parts[0].strip())
            self.daily_used = int(parts[1].strip())
            print(f"  [API usage] 15-min: {self.short_used}/100  daily: {self.daily_used}/1000")

    def wait_if_needed(self):
        # Daily limit — raise exception so we can exit cleanly rather than
        # sleeping until midnight (which would get killed by GitHub Actions timeout)
        if self.daily_used >= 980:
            raise DailyLimitReached(
                f"Daily limit reached ({self.daily_used}/1000). "
                "Exiting cleanly — retrigger tomorrow after midnight UTC."
            )

        # 15-minute limit — pause until the window rolls over
        if self.short_used >= 85:
            elapsed = time.time() - self.window_start
            wait    = max(0, 910 - elapsed)  # 15 min + 10s buffer
            if wait > 0:
                print(f"[Rate limit] 15-min limit ({self.short_used}/100). "
                      f"Pausing {wait:.0f}s...")
                time.sleep(wait)
            self.short_used   = 0
            self.window_start = time.time()


rate_limiter = RateLimiter()

# ─── Auth ─────────────────────────────────────────────────────────────────────

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
    sa_info     = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds       = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    http_client = gspread.BackoffClient(auth=creds)
    return gspread.Client(auth=creds, http_client=http_client)


def get_workouts_sheet(client: gspread.Client) -> gspread.Worksheet:
    wb = client.open_by_key(os.environ["SPREADSHEET_ID"])
    return wb.worksheet(WORKOUTS_TAB)

# ─── Strava helpers ───────────────────────────────────────────────────────────

def strava_get(endpoint: str, access_token: str, params: dict = None):
    """Authenticated GET with automatic retry on 429 rate limit responses."""
    headers = {"Authorization": f"Bearer {access_token}"}

    for attempt in range(3):
        resp = requests.get(
            f"{STRAVA_API_BASE}{endpoint}",
            headers=headers,
            params=params or {}
        )
        rate_limiter.update(resp.headers)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 910))
            print(f"\n[429 Rate limited] Strava says wait {retry_after}s. "
                  f"Pausing... (attempt {attempt + 1}/3)")
            time.sleep(retry_after + 10)
            continue

        resp.raise_for_status()
        return resp.json()

    raise Exception(f"Strava API failed after 3 attempts on {endpoint}")


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
    """Fetch laps — only call this for Run/Ride/Hike type activities."""
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

# ─── Sheet helpers ────────────────────────────────────────────────────────────

def get_existing_ids_and_latest_date(ws: gspread.Worksheet) -> tuple[set[str], str | None]:
    """
    Returns all existing activity IDs (for duplicate detection) and the date
    of the most recent activity already in the sheet (for smart pagination).
    """
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        # Only header row or empty — full backfill needed
        return set(), None

    rows        = all_values[1:]  # Skip header
    ids         = {row[0] for row in rows if row[0]}
    dates       = [row[1] for row in rows if len(row) > 1 and row[1]]
    latest_date = max(dates) if dates else None

    return ids, latest_date


BATCH_SIZE = 20  # Rows per Sheets API call


def flush_batch(ws: gspread.Worksheet, batch: list[list]) -> int:
    """Write a batch of rows to the sheet, with retry on failure."""
    if not batch:
        return 0
    for attempt in range(3):
        try:
            ws.append_rows(batch, value_input_option="USER_ENTERED")
            print(f"  [Sheets] Wrote {len(batch)} rows ✓")
            return len(batch)
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  [Sheets] Write failed (attempt {attempt + 1}/3): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    raise Exception(f"Sheets write failed after 3 attempts — batch of {len(batch)} rows lost")

# ─── Smart pagination ─────────────────────────────────────────────────────────

def fetch_unsynced_activities(access_token: str, after_date: str | None) -> list[dict]:
    """
    Fetch only activities not yet in the sheet using Strava's `after` parameter.

    If after_date is None (empty sheet), fetches everything.
    If after_date is set, fetches only activities after that date — dramatically
    reducing the number of list-pagination API requests on subsequent runs.

    Returns activities oldest-first for chronological sheet ordering.
    """
    all_activities = []
    page           = 1

    # Convert date string to epoch for Strava's `after` param
    after_epoch = None
    if after_date:
        dt          = datetime.datetime.strptime(after_date, "%Y-%m-%d")
        after_epoch = int(dt.timestamp())
        print(f"[Strava] Fetching activities after {after_date} (epoch {after_epoch})...")
    else:
        print("[Strava] No existing data — fetching full activity history...")

    while True:
        params = {"per_page": 200, "page": page}
        if after_epoch:
            params["after"] = after_epoch

        batch = strava_get("/athlete/activities", access_token, params=params)

        if not batch:
            break

        all_activities.extend(batch)
        print(f"  Page {page}: {len(batch)} activities fetched "
              f"(running total: {len(all_activities)})")
        page += 1

        if len(batch) < 200:
            break  # Last page — no need to fetch another

    # Reverse so we process oldest → newest
    all_activities.reverse()
    print(f"[Strava] {len(all_activities)} unsynced activities to process.\n")
    return all_activities

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Patrick's AI Coach — Historical Backfill (Optimised)")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    access_token = get_strava_access_token()
    gc           = get_gspread_client()
    ws           = get_workouts_sheet(gc)

    # Load existing IDs and most recent date in one sheet read
    existing_ids, latest_date = get_existing_ids_and_latest_date(ws)
    print(f"[Sheets] {len(existing_ids)} activities already in sheet.")
    if latest_date:
        print(f"[Sheets] Most recent activity date: {latest_date} — using for smart pagination.")
    print()

    # Fetch only activities not yet in sheet
    activities = fetch_unsynced_activities(access_token, latest_date)

    if not activities:
        print("[Info] No new activities to process. Backfill is complete!")
        return

    skipped = 0
    added   = 0
    failed  = 0
    total   = len(activities)
    batch   = []

    try:
        for i, act in enumerate(activities, 1):
            activity_id   = str(act["id"])
            name          = act.get("name", "Untitled")
            date          = act.get("start_date_local", "")[:10]
            sport_type    = act.get("sport_type", act.get("type", ""))
            fetch_laps    = sport_type in LAP_ACTIVITY_TYPES

            print(f"[{i}/{total}] {date} — {name} ({sport_type})", end="")

            # Belt-and-braces duplicate check (smart pagination handles most,
            # but overlapping dates on the boundary need this safety net)
            if activity_id in existing_ids:
                print(" → skipped (already in sheet)")
                skipped += 1
                continue

            try:
                # Check rate limits before each activity
                rate_limiter.wait_if_needed()

                # Fetch detailed activity (1 request)
                detailed = strava_get(f"/activities/{act['id']}", access_token)
                rate_limiter.wait_if_needed()

                # Fetch laps only for Run/Ride/Hike (1 request) — skip for Tennis/Walk/etc
                if fetch_laps:
                    laps = fetch_activity_laps(act["id"], access_token)
                    rate_limiter.wait_if_needed()
                    lap_note = f"{len(laps)} laps"
                else:
                    laps     = []
                    lap_note = "no laps (non-run)"

                row = build_row(act, detailed, laps)
                batch.append(row)
                existing_ids.add(activity_id)
                print(f" → queued ({lap_note}) [batch {len(batch)}/{BATCH_SIZE}]")

                # Flush to Sheets every BATCH_SIZE rows
                if len(batch) >= BATCH_SIZE:
                    added += flush_batch(ws, batch)
                    batch  = []

            except DailyLimitReached:
                # Re-raise so outer try catches it and exits cleanly
                raise

            except Exception as e:
                print(f" → FAILED: {e}")
                # Flush progress before continuing so we don't lose it
                if batch:
                    added += flush_batch(ws, batch)
                    batch  = []
                failed += 1
                time.sleep(5)
                continue

    except DailyLimitReached as e:
        # Graceful exit — flush whatever we have, report, and exit cleanly
        print(f"\n[Rate limit] {e}")
        if batch:
            print(f"[Sheets] Flushing final batch of {len(batch)} rows before exit...")
            added += flush_batch(ws, batch)
            batch  = []
        print("\n" + "=" * 60)
        print(f"Partial run complete — hit daily limit.")
        print(f"  Added this run:  {added}")
        print(f"  Skipped:         {skipped} (already existed)")
        print(f"  Failed:          {failed}")
        print(f"  Remaining:       ~{total - added - skipped - failed} activities")
        print(f"  Next run:        Retrigger after midnight UTC")
        print("=" * 60)
        sys.exit(0)  # Clean exit — GitHub Actions won't mark this as failed

    # Flush any remaining rows
    if batch:
        print(f"\n[Sheets] Flushing final batch of {len(batch)} rows...")
        added += flush_batch(ws, batch)

    print("\n" + "=" * 60)
    print(f"Backfill complete!")
    print(f"  Added:   {added}")
    print(f"  Skipped: {skipped} (already existed)")
    print(f"  Failed:  {failed}")
    print(f"  Sheets API calls: ~{max(1, added // BATCH_SIZE + 1)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
