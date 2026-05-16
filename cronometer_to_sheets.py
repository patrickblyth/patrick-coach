"""
cronometer_to_sheets.py — Daily Cronometer → Google Sheets sync.
Fetches yesterday's nutrition totals and upserts into the Daily_Stats tab.

Writes to the NUTRITION columns only — Garmin columns are left untouched.
Safe to re-run: existing rows are updated in-place, not duplicated.

Data pulled (daily totals from Cronometer export API):
  - Calories_In
  - Protein_g
  - Carbs_g
  - Fat_g
  - Fiber_g
  - Iron_mg        — critical for vegetarian endurance athletes
  - Calcium_mg     — stress fracture risk at high mileage

Daily_Stats tab column layout (full row):
  Date | Sleep_Score | Sleep_Duration_hrs | Resting_HR |
  Body_Battery_Start | Body_Battery_End | Stress_Score | Weight_kg |
  Calories_In | Protein_g | Carbs_g | Fat_g | Fiber_g | Iron_mg | Calcium_mg

Environment variables required (store as GitHub Secrets):
  CRONOMETER_EMAIL
  CRONOMETER_PASSWORD
  GOOGLE_SERVICE_ACCOUNT  — same service account JSON as other syncs
  SPREADSHEET_ID          — same sheet ID as other syncs
"""

import os
import io
import csv
import json
import time
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

# ─── Constants ────────────────────────────────────────────────────────────────

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DAILY_STATS_TAB = "Daily_Stats"

# Full header list for the Daily_Stats tab.
# Garmin script owns columns A-H; Cronometer script owns I-O.
# Both scripts upsert by date so they never clobber each other.
ALL_HEADERS = [
    "Date",                 # A — shared key
    "Sleep_Score",          # B — Garmin
    "Sleep_Duration_hrs",   # C — Garmin
    "Resting_HR",           # D — Garmin
    "Body_Battery_Start",   # E — Garmin
    "Body_Battery_End",     # F — Garmin
    "Stress_Score",         # G — Garmin
    "Weight_kg",            # H — Garmin
    "Calories_In",          # I — Cronometer
    "Protein_g",            # J — Cronometer
    "Carbs_g",              # K — Cronometer
    "Fat_g",                # L — Cronometer
    "Fiber_g",              # M — Cronometer
    "Iron_mg",              # N — Cronometer
    "Calcium_mg",           # O — Cronometer
]

# Cronometer columns this script writes (0-based index into ALL_HEADERS)
CRONO_COL_START = 8   # "Calories_In" is index 8
CRONO_COLS = ALL_HEADERS[CRONO_COL_START:]  # I through O

# ─── Cronometer auth ──────────────────────────────────────────────────────────
# Cronometer has no public API. We use the same GWT-RPC protocol that the
# web app uses internally — the same approach as the cronometer-mcp package.
# Steps: obtain CSRF token → POST credentials → generate auth token → export CSV.

CRONO_LOGIN_URL  = "https://cronometer.com/login"
CRONO_GWT_URL    = "https://cronometer.com/cronometer/app"
CRONO_EXPORT_URL = "https://cronometer.com/export"

# GWT protocol values — hardcoded per Cronometer's current web deploy.
# If auth starts failing with GWT errors, update these by inspecting
# requests to cronometer.com/cronometer/app in browser DevTools.
GWT_PERMUTATION = "7B121DC5483BF272B1BC1916DA9FA963"
GWT_HEADER      = "2D6A926E3729946302DC68073CB0D550"


def get_cronometer_session() -> tuple[requests.Session, int]:
    """
    Authenticate with Cronometer and return (session, user_id).
    The session holds cookies for subsequent export requests.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Get anti-CSRF token from login page
    resp = session.get(CRONO_LOGIN_URL)
    resp.raise_for_status()
    # Parse anticsrf from hidden input: <input name="anticsrf" value="...">
    import re
    match = re.search(r'name="anticsrf"\s+value="([^"]+)"', resp.text)
    if not match:
        raise Exception("Could not find anticsrf token on Cronometer login page")
    anticsrf = match.group(1)
    print(f"  [Cronometer] Got anticsrf token")

    # Step 2: POST credentials
    resp = session.post(CRONO_LOGIN_URL, data={
        "username":  os.environ["CRONOMETER_EMAIL"],
        "password":  os.environ["CRONOMETER_PASSWORD"],
        "anticsrf":  anticsrf,
    }, allow_redirects=True)
    resp.raise_for_status()
    if "login" in resp.url.lower() and "error" in resp.text.lower():
        raise Exception("Cronometer login failed — check credentials")
    print(f"  [Cronometer] Logged in successfully")

    # Step 3: GWT authenticate call to get user ID
    gwt_payload = (
        f"7|0|6|https://cronometer.com/cronometer/|{GWT_HEADER}|"
        f"com.cronometer.shared.rpc.CronometerService|authenticate|"
        f"com.cronometer.shared.user.AuthTokenType/2065601159|FOOD_DIARY|1|2|3|4|1|5|6|0|"
    )
    resp = session.post(CRONO_GWT_URL, data=gwt_payload, headers={
        "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
        "X-GWT-Permutation": GWT_PERMUTATION,
        "X-GWT-Module-Base": "https://cronometer.com/cronometer/",
    })
    resp.raise_for_status()
    # Response format: //OK[<token>,<user_id>,...]
    user_id_match = re.search(r'//OK\["[^"]+",(\d+)', resp.text)
    if not user_id_match:
        raise Exception(f"Could not parse user ID from GWT authenticate response: {resp.text[:200]}")
    user_id = int(user_id_match.group(1))
    print(f"  [Cronometer] User ID: {user_id}")

    return session, user_id


def generate_auth_token(session: requests.Session, user_id: int) -> str:
    """Generate a short-lived export token via GWT RPC."""
    gwt_payload = (
        f"7|0|7|https://cronometer.com/cronometer/|{GWT_HEADER}|"
        f"com.cronometer.shared.rpc.CronometerService|generateAuthorizationToken|"
        f"I|com.cronometer.shared.user.AuthTokenType/2065601159|java.lang.String/2004016611|"
        f"1|2|3|4|3|5|6|7|{user_id}|0|FOOD_DIARY|"
    )
    resp = session.post(CRONO_GWT_URL, data=gwt_payload, headers={
        "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
        "X-GWT-Permutation": GWT_PERMUTATION,
        "X-GWT-Module-Base": "https://cronometer.com/cronometer/",
    })
    resp.raise_for_status()
    import re
    token_match = re.search(r'//OK\["([^"]+)"', resp.text)
    if not token_match:
        raise Exception(f"Could not parse auth token: {resp.text[:200]}")
    token = token_match.group(1)
    print(f"  [Cronometer] Got export auth token")
    return token


def fetch_daily_nutrition_csv(
    session: requests.Session,
    token: str,
    date_str: str
) -> str:
    """Download the daily nutrition CSV for a single date."""
    resp = session.get(CRONO_EXPORT_URL, params={
        "authToken": token,
        "report":    "dailySummary",
        "start":     date_str,
        "end":       date_str,
    })
    resp.raise_for_status()
    return resp.text

# ─── Nutrition parsing ────────────────────────────────────────────────────────

# Cronometer CSV column names for the fields we want.
# These are the exact header strings in the daily summary export.
CRONO_FIELD_MAP = {
    "Energy (kcal)":  "Calories_In",
    "Protein (g)":    "Protein_g",
    "Carbs (g)":      "Carbs_g",
    "Fat (g)":        "Fat_g",
    "Fiber (g)":      "Fiber_g",
    "Iron (mg)":      "Iron_mg",
    "Calcium (mg)":   "Calcium_mg",
}


def parse_nutrition(csv_text: str, date_str: str) -> dict:
    """
    Parse the daily summary CSV and extract the fields we care about.
    Returns a dict keyed by our column names, values as floats or "".
    """
    result = {col: "" for col in CRONO_COLS}

    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        # Date column in Cronometer export is "Date"
        if row.get("Date", "").strip() != date_str:
            continue
        for crono_col, our_col in CRONO_FIELD_MAP.items():
            raw = row.get(crono_col, "").strip()
            if raw:
                try:
                    result[our_col] = round(float(raw), 2)
                except ValueError:
                    result[our_col] = ""
        print(f"  [Cronometer] Parsed: {result}")
        return result

    print(f"  [Cronometer] No data found for {date_str} in export")
    return result

# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_gspread_client() -> gspread.Client:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds   = Credentials.from_service_account_info(sa_info, scopes=GSHEETS_SCOPES)
    return gspread.authorize(creds)


def get_daily_stats_sheet(gc: gspread.Client) -> gspread.Worksheet:
    """
    Open the Daily_Stats tab. If it doesn't exist, create it with full headers.
    If it exists but is missing the Cronometer columns, add them.
    """
    wb = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    try:
        ws = wb.worksheet(DAILY_STATS_TAB)
        print(f"[Sheets] Opened existing tab: {DAILY_STATS_TAB}")

        # Check if Cronometer columns exist; add any that are missing
        existing_headers = ws.row_values(1)
        missing = [h for h in ALL_HEADERS if h not in existing_headers]
        if missing:
            print(f"[Sheets] Adding missing columns: {missing}")
            for header in missing:
                col_idx = ALL_HEADERS.index(header) + 1  # 1-based
                # Ensure sheet has enough columns
                if ws.col_count < col_idx:
                    ws.add_cols(col_idx - ws.col_count)
                ws.update_cell(1, col_idx, header)
        return ws

    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=DAILY_STATS_TAB, rows=2000, cols=len(ALL_HEADERS))
        ws.append_row(ALL_HEADERS)
        print(f"[Sheets] Created new tab: {DAILY_STATS_TAB}")
        return ws


def find_existing_row(ws: gspread.Worksheet, date_str: str) -> int | None:
    """Return 1-based row index for date_str, or None if not found."""
    dates = ws.col_values(1)
    for i, val in enumerate(dates, start=1):
        if val == date_str:
            return i
    return None


def upsert_nutrition(ws: gspread.Worksheet, nutrition: dict, date_str: str):
    """
    Write Cronometer nutrition columns into the row for date_str.
    If the row doesn't exist, creates it with date in col A and nutrition
    in cols I-O, leaving Garmin columns blank (Garmin script fills those).
    If the row exists, updates only the Cronometer columns in-place.
    """
    existing_row = find_existing_row(ws, date_str)

    # Build the values for Cronometer columns only
    crono_values = [nutrition.get(col, "") for col in CRONO_COLS]

    if existing_row:
        # Update columns I-O in the existing row
        start_col = CRONO_COL_START + 1  # 1-based
        end_col   = start_col + len(CRONO_COLS) - 1
        cell_range = f"{col_letter(start_col)}{existing_row}:{col_letter(end_col)}{existing_row}"
        ws.update(cell_range, [crono_values], value_input_option="USER_ENTERED")
        print(f"[Sheets] Updated nutrition columns in row {existing_row} for {date_str} ✓")
    else:
        # Build a full row — Garmin columns blank, Cronometer columns filled
        full_row = [""] * len(ALL_HEADERS)
        full_row[0] = date_str  # Date
        for i, col in enumerate(CRONO_COLS):
            full_row[CRONO_COL_START + i] = nutrition.get(col, "")
        ws.append_row(full_row, value_input_option="USER_ENTERED")
        print(f"[Sheets] Appended new row for {date_str} with nutrition data ✓")


def col_letter(n: int) -> str:
    """Convert 1-based column index to letter (1=A, 9=I, 15=O etc)."""
    result = ""
    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Patrick's AI Coach — Cronometer → Sheets Sync")
    print(f"Run time: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    # Yesterday in Melbourne local time (same logic as garmin_to_sheets.py)
    try:
        from zoneinfo import ZoneInfo
        melb_tz = ZoneInfo("Australia/Melbourne")
    except ImportError:
        melb_tz = datetime.timezone(datetime.timedelta(hours=10))
    melb_now  = datetime.datetime.now(melb_tz)
    yesterday = (melb_now.date() - datetime.timedelta(days=1)).isoformat()
    print(f"\nFetching Cronometer data for: {yesterday}\n")

    # Cronometer auth
    print("[Cronometer] Authenticating...")
    session, user_id = get_cronometer_session()
    time.sleep(1)  # Brief pause after login

    # Generate export token
    token = generate_auth_token(session, user_id)
    time.sleep(1)

    # Fetch and parse daily nutrition
    print(f"\n[Cronometer] Fetching daily nutrition export...")
    csv_text  = fetch_daily_nutrition_csv(session, token, yesterday)
    nutrition = parse_nutrition(csv_text, yesterday)

    print(f"\n[Nutrition] {nutrition}")

    # Write to Sheets
    gc = get_gspread_client()
    ws = get_daily_stats_sheet(gc)
    print()
    upsert_nutrition(ws, nutrition, yesterday)

    print("\n" + "=" * 60)
    print("Cronometer sync complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
