"""
main.py — Drive Auditor + Mover Cloud Function
================================================
Single Cloud Function with two modes:
  GET /        → runs full audit (Claude match + Sheet + email)
  GET /?action=move → runs mover only (reads sheet approvals + moves files)

Deploy once. Triggered monthly by Cloud Scheduler for audit.
Move button in email calls same URL with ?action=move
"""

import os
import re
import json
import time
import random
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import yaml

import pandas as pd
from rapidfuzz import process as fuzz_process, fuzz
from unidecode import unidecode
import gspread
import anthropic
import functions_framework
from google.oauth2 import service_account
from googleapiclient.discovery import build
import base64


# ── Load config from environment variables ─────────────────────────────────────
SA_KEY_JSON = base64.b64decode(os.environ["SA_KEY_JSON_B64"]).decode("utf-8")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

EXISTING_SHEET_ID  = os.environ["EXISTING_SHEET_ID"]
SHEET_NAME_PREFIX  = os.environ["SHEET_NAME_PREFIX"]
SMTP_USER          = os.environ["SMTP_USER"]
SMTP_PASSWORD      = os.environ["SMTP_PASSWORD"]
REPORT_EMAIL       = os.environ["REPORT_EMAIL"]
FUNCTION_URL       = os.environ["FUNCTION_URL"]   # your deployed function URL

# ── Load config.yaml ──────────────────────────────────────────────────────────
_config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(_config_path) as _f:
    _cfg = yaml.safe_load(_f)

# Agency
AGENCY_NAME = _cfg["agency"]["name"]

# Drive structure
SCAN_TARGETS = [
    (t["parent"], t.get("child"))
    for t in _cfg["drive"]["scan_targets"]
]
ROOT_FOLDER      = _cfg["drive"]["root_folder"]
EXCLUDED_FOLDERS = set(_cfg["drive"]["excluded_folders"])
ARCHIVE_FOLDER   = _cfg["drive"].get("archive_folder", "[Archive files]")
SCAN_SHARED_WITH_ME = _cfg["drive"].get("scan_shared_with_me", False)

# Matching
FUZZY_THRESHOLD             = _cfg["matching"]["fuzzy_threshold"]
CLAUDE_VALIDATION_THRESHOLD = _cfg["matching"]["validation_threshold"]
CLAUDE_BATCH_SIZE           = _cfg["matching"]["ai_batch_size"]
BATCH_SIZE_DEFAULT          = _cfg["matching"]["batch_size"]
MAX_CANDIDATE_FOLDERS       = _cfg["matching"].get("max_candidate_folders", 120)
SKIP_CONFIDENCE             = _cfg["matching"]["skip_confidence"]
SUGGEST_BATCH_SIZE          = _cfg["matching"]["suggestion_batch_size"]

# AI
ANTHROPIC_MODEL = _cfg["ai"]["model"]

# Rate limiting
RATE_LIMIT_MIN_GAP = _cfg["rate_limiting"]["min_gap_seconds"]
INTER_BATCH_SLEEP  = _cfg["rate_limiting"]["inter_batch_sleep"]


# ── Auth (runs once at cold start) ────────────────────────────────────────────
sa_info = json.loads(SA_KEY_JSON)
creds   = service_account.Credentials.from_service_account_info(
    sa_info,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
)
# ── Lazy client initialization ─────────────────────────────────────────────
# Clients are initialized on first use, not at cold start.
# This avoids SSL EOF errors caused by network instability during
# GCP container cold start before the network stack is fully ready.
_drive_service    = None
_sheets_client    = None
_anthropic_client = None

def _get_drive_service():
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

def _get_sheets_client():
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = gspread.authorize(creds)
    return _sheets_client

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client

def _reset_clients():
    """Call this when an SSL error is detected to force client rebuild."""
    global _drive_service, _sheets_client, _anthropic_client
    _drive_service    = None
    _sheets_client    = None
    _anthropic_client = None
    print("  ✓ API clients reset — will reinitialize on next call")


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_folder_id(folder_name):
    resp = _get_drive_service().files().list(
        q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    folders = resp.get("files", [])
    if not folders:
        raise ValueError(f"Folder '{folder_name}' not found in Drive.")
    return folders[0]["id"]


def get_client_folder_map(root_id):
    resp = _get_drive_service().files().list(
        q=f"'{root_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)", pageSize=1000
    ).execute()
    return {f["name"]: f["id"] for f in resp.get("files", [])}

def get_nested_folder_id(parent_name, child_name=None):
    # ── Special case: My Drive root ───────────────────────────────────────────
    if parent_name == "__MY_DRIVE_ROOT__":
        return _get_drive_service().files().get(fileId="root", fields="id").execute()["id"]

    # ── Normal folder lookup ───────────────────────────────────────────────────
    resp = _get_drive_service().files().list(
        q=f"name='{parent_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    folders = resp.get("files", [])
    if not folders:
        raise ValueError(f"Folder '{parent_name}' not found in Drive.")
    parent_id = folders[0]["id"]

    if not child_name:
        return parent_id

    resp = _get_drive_service().files().list(
        q=f"name='{child_name}' and mimeType='application/vnd.google-apps.folder' "
          f"and '{parent_id}' in parents and trashed=false",
        fields="files(id, name)"
    ).execute()
    children = resp.get("files", [])
    if not children:
        raise ValueError(f"Subfolder '{child_name}' not found inside '{parent_name}'.")
    return children[0]["id"]

def get_latest_sheet_tab(tab_name=None):
    spreadsheet = _get_sheets_client().open_by_key(EXISTING_SHEET_ID)
    audit_tabs  = [ws for ws in spreadsheet.worksheets()
                   if ws.title.startswith("Audit ")]
    if not audit_tabs:
        raise ValueError("No audit tab found. Run audit first.")

    if tab_name:
        match = next(
            (ws for ws in audit_tabs if tab_name.lower() in ws.title.lower()),
            None
        )
        if not match:
            available = ", ".join(ws.title for ws in audit_tabs)
            raise ValueError(f"Tab '{tab_name}' not found. Available: {available}")
        return spreadsheet, match

    latest = sorted(audit_tabs, key=lambda ws: ws.title)[-1]
    return spreadsheet, latest


def get_today_audit_tab():
    """
    Returns (spreadsheet, sheet) if an audit tab for TODAY exists, else (None, None).
    Matches any tab whose title starts with 'Audit YYYY-MM-DD' for today's date.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    spreadsheet = _get_sheets_client().open_by_key(EXISTING_SHEET_ID)
    for ws in spreadsheet.worksheets():
        if ws.title.startswith(f"Audit {today}"):
            print(f"  ✓ Found today's tab: '{ws.title}'")
            return spreadsheet, ws
    print(f"  No tab found for today ({today}) — first run.")
    return None, None


def fetch_unprocessed_batch(sheet, batch_size=500):
    """
    Reads sheet and returns up to batch_size rows where is_processed != '1'.
    Returns (batch_df, sheet_row_map) where:
      batch_df: DataFrame with columns file_id, stray_file_name, found_in, status
      sheet_row_map: dict {df_index: sheet_row_number (1-based, header=row 1)}
    """
    all_values = sheet.get_all_values()
    if not all_values:
        return pd.DataFrame(), {}

    headers = all_values[0]
    col = lambda name: headers.index(name) if name in headers else None

    proc_col     = col("is_processed")
    file_id_col  = col("file_id")
    name_col     = col("stray_file_name")
    found_in_col = col("found_in")
    status_col   = col("status")

    if any(c is None for c in [proc_col, file_id_col, name_col, found_in_col, status_col]):
        print("  ⚠ Required columns missing from sheet")
        return pd.DataFrame(), {}

    rows, row_map = [], {}
    df_idx = 0

    for sheet_row_idx, row in enumerate(all_values[1:], start=2):
        is_proc = row[proc_col].strip() if len(row) > proc_col else ""
        if is_proc == "1":
            continue

        rows.append({
            "file_id":         row[file_id_col]   if len(row) > file_id_col   else "",
            "stray_file_name": row[name_col]       if len(row) > name_col       else "",
            "found_in":        row[found_in_col]   if len(row) > found_in_col   else "",
            "status":          row[status_col]     if len(row) > status_col     else "UNMATCHED",
        })
        row_map[df_idx] = sheet_row_idx
        df_idx += 1

        if df_idx >= batch_size:
            break

    print(f"  Fetched {len(rows)} unprocessed rows")
    if not rows:
        return pd.DataFrame(), {}

    batch_df = pd.DataFrame(rows)
    # Initialise match columns expected by downstream functions
    batch_df["suggested_folder"]    = None
    batch_df["suggested_folder_id"] = ""
    batch_df["confidence"]          = "low"
    batch_df["reason"]              = ""
    batch_df["notes"]               = ""
    return batch_df, row_map


def _get_fresh_sheet(sheet_title):
    """
    Rebuilds the gspread client from scratch to get a completely
    fresh SSL connection. Re-opening the sheet is not enough —
    the underlying requests.Session must be recreated.
    """
    import time as _time
    for attempt in range(3):
        try:
            fresh_client      = gspread.authorize(creds)
            fresh_spreadsheet = fresh_client.open_by_key(EXISTING_SHEET_ID)
            for ws in fresh_spreadsheet.worksheets():
                if ws.title == sheet_title:
                    print(f"  ✓ Fresh sheet connection established (attempt {attempt+1})")
                    return fresh_spreadsheet, ws
        except Exception as e:
            print(f"  ⚠ Connection refresh attempt {attempt+1} failed: {e}")
            _time.sleep(3)
    raise RuntimeError("Could not establish fresh sheet connection after 3 attempts")


def mark_rows_processed(sheet, batch_df, sheet_row_map,
                        iteration_number=1):
    """
    Writes match results + is_processed=1 back to sheet.
    Splits into chunks of 200 rows per API call to avoid
    SSL timeout on large payloads.
    """
    if batch_df.empty or not sheet_row_map:
        return

    # Build all updates first
    all_updates = []
    for df_idx, sheet_row in sheet_row_map.items():
        sf = batch_df.at[df_idx, "suggested_folder"]
        sf = sf if sf and str(sf) not in ("nan", "None", "") \
             else "— could not determine —"

        notes = batch_df.at[df_idx, "notes"] \
                if "notes" in batch_df.columns else ""
        notes = notes if notes and str(notes) not in ("nan", "None") \
                else ""

        manual = ARCHIVE_FOLDER \
                 if batch_df.at[df_idx, "status"] == "UNMATCHED" else ""

        tab = sheet.title
        all_updates += [
            {"range":  f"'{tab}'!D{sheet_row}:G{sheet_row}",
             "values": [[sf,
                         batch_df.at[df_idx, "confidence"],
                         batch_df.at[df_idx, "reason"],
                         batch_df.at[df_idx, "status"]]]},
            {"range":  f"'{tab}'!I{sheet_row}:J{sheet_row}",
             "values": [[manual, notes]]},
            {"range":  f"'{tab}'!L{sheet_row}",
             "values": [["1"]]},
            {"range":  f"'{tab}'!M{sheet_row}",
             "values": [[str(iteration_number)]]},
        ]

    # Write in chunks of 200 rows (800 update entries) to avoid SSL timeout
    CHUNK_SIZE   = 200   # rows per API call
    ENTRIES_PER_ROW = 4  # D:G, I:J, L, M
    chunk_entries = CHUNK_SIZE * ENTRIES_PER_ROW   # 800 entries per chunk

    total_chunks = -(-len(all_updates) // chunk_entries)
    written_rows = 0

    for i in range(0, len(all_updates), chunk_entries):
        chunk     = all_updates[i : i + chunk_entries]
        chunk_num = i // chunk_entries + 1
        try:
            sheet.spreadsheet.values_batch_update({
                "valueInputOption": "RAW",
                "data": chunk,
            })
            rows_in_chunk = len(chunk) // ENTRIES_PER_ROW
            written_rows += rows_in_chunk
            print(f"  ✓ Sheet chunk {chunk_num}/{total_chunks} "
                  f"written ({rows_in_chunk} rows)")
            if chunk_num < total_chunks:
                time.sleep(1)   # brief pause between chunks
        except Exception as e:
            print(f"  ⚠ Sheet chunk {chunk_num} failed: {e} — retrying once...")
            time.sleep(5)
            try:
                sheet.spreadsheet.values_batch_update({
                    "valueInputOption": "RAW",
                    "data": chunk,
                })
                print(f"  ✓ Sheet chunk {chunk_num} retry succeeded")
            except Exception as e2:
                print(f"  ✗ Sheet chunk {chunk_num} retry failed: {e2}")
                print(f"  Attempting connection refresh...")
                try:
                    fresh = _get_sheets_client().open_by_key(EXISTING_SHEET_ID)
                    for ws in fresh.worksheets():
                        if ws.title == sheet.title:
                            sheet = ws
                            break
                    sheet.spreadsheet.values_batch_update({
                        "valueInputOption": "RAW",
                        "data": chunk,
                    })
                    print(f"  ✓ Chunk {chunk_num} succeeded after connection refresh")
                except Exception as e3:
                    print(f"  ✗ Chunk {chunk_num} failed after refresh: {e3}")

    print(f"  ✓ Marked {written_rows}/{len(sheet_row_map)} rows as processed")


def check_all_processed(sheet):
    """
    Returns True if every data row has is_processed = '1'.
    Used to decide whether to send the final audit email.
    """
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return True
    headers = all_values[0]
    if "is_processed" not in headers:
        return False
    proc_col = headers.index("is_processed")
    return all(
        row[proc_col].strip() == "1"
        for row in all_values[1:]
        if row and len(row) > proc_col
    )


def get_current_iteration_number(sheet):
    """
    Returns max(iteration_number) + 1 across all processed rows.
    Returns 1 if no Claude runs have been recorded yet.
    Fuzzy-matched rows have iteration_number="" and are excluded.
    """
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return 1
    headers = all_values[0]
    if "iteration_number" not in headers:
        return 1
    iter_col = headers.index("iteration_number")
    nums = []
    for row in all_values[1:]:
        if len(row) > iter_col:
            val = row[iter_col].strip()
            if val.isdigit():
                nums.append(int(val))
    return max(nums) + 1 if nums else 1


def move_file(file_id, destination_folder_id):
    for attempt in range(4):
        try:
            file_info       = _get_drive_service().files().get(fileId=file_id, fields="parents,name").execute()
            current_parents = file_info.get("parents", [])
            if current_parents:
                # Owned file — move it (swap parents)
                _get_drive_service().files().update(
                    fileId=file_id,
                    addParents=destination_folder_id,
                    removeParents=",".join(current_parents),
                    fields="id, parents"
                ).execute()
            else:
                # sharedWithMe file — SA cannot move it; create a shortcut in destination instead
                _get_drive_service().files().create(
                    body={
                        "name": file_info.get("name", "Untitled"),
                        "mimeType": "application/vnd.google-apps.shortcut",
                        "shortcutDetails": {"targetId": file_id},
                        "parents": [destination_folder_id],
                    },
                    fields="id, name"
                ).execute()
            return
        except Exception as e:
            if attempt == 3:
                raise
            wait = 10 * (2 ** attempt)  # 10s, 20s, 40s
            print(f"  ⚠ Move attempt {attempt+1} failed ({e}) — retrying in {wait}s...")
            time.sleep(wait)


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = REPORT_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(SMTP_USER, REPORT_EMAIL, msg.as_string())
    print(f"  ✓ Email sent to {REPORT_EMAIL}")


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT MODE
# ══════════════════════════════════════════════════════════════════════════════

def list_drive_children(folder_id):
    items, page_token = [], None
    while True:
        resp = _get_drive_service().files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, parents)",
            pageToken=page_token, pageSize=1000
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def list_shared_with_me_files():
    """Returns files shared directly with the SA from the user's My Drive."""
    items, page_token = [], None
    while True:
        resp = _get_drive_service().files().list(
            q="sharedWithMe=true and mimeType!='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, parents)",
            pageSize=1000,
            pageToken=page_token
        ).execute(num_retries=3)
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items



# ── My Drive pre-filter ────────────────────────────────────────────────────────
_CANDIDATE_BLACKLIST = {
    "analytics", "marketing", "management", "report", "data", "audit",
    "guide", "document", "implementation", "tracking", "roadmap", "code",
    "changes", "measurement", "strategy", "analysis", "project", "review",
    "plan", "template", "sample", "test", "services", "solutions", "group",
    "digital", "media", "studio", "consulting", "agency", "revenue", "sales",
    "growth", "performance", "dashboard", "summary", "overview", "scorecard",
}

_GENERIC_WORDS = {
    "report", "data", "template", "dashboard", "summary", "overview",
    "metrics", "analytics", "tracking", "guide", "document", "doc",
    "presentation", "sheet", "spreadsheet", "file", "notes", "meeting",
    "agenda", "minutes", "proposal", "invoice", "budget", "plan",
    "roadmap", "strategy", "review", "analysis", "scorecard", "kpi",
    "utm", "gtm", "ga4", "fb", "google", "facebook", "instagram",
    "twitter", "linkedin", "youtube", "sample", "test", "demo",
    "draft", "final", "backup", "copy", "archive", "old", "new",
    "updated", "marketing", "sales", "revenue", "growth", "performance",
    "client", "internal", "external", "shared", "info", "details",
    "access", "login", "credentials", "setup", "config", "misc",
}
_DATE_RE = re.compile(
    r'\b(\d{4}[-_]\d{2}[-_]\d{2}|\d{8}|\d{6}|\d{2}[-_]\d{2}[-_]\d{4})\b'
)


def _is_generic_candidate(candidate: str) -> bool:
    tokens = {t for t in re.split(r'\s+', candidate.lower().strip()) if len(t) > 2}
    return bool(tokens) and tokens.issubset(_CANDIDATE_BLACKLIST)


def _stem(name):
    return re.sub(
        r'\.(gdoc|gsheet|gslide|xlsx?|csv|pdf|pptx?|docx?|txt|png|jpe?g|gif|zip|mov|mp4)$',
        '', name, flags=re.IGNORECASE
    )


def is_generic_filename(name):
    s = _stem(name)
    if _DATE_RE.search(s):
        return True
    words = {w for w in re.split(r'[\s\-_\.]+', s.lower()) if len(w) > 2}
    return bool(words) and not (words - _GENERIC_WORDS)


_FILE_EXT = re.compile(
    r'\.(gdoc|gsheet|gslide|xlsx?|csv|pdf|pptx?|docx?|txt|png|jpe?g|gif|zip|mov|mp4)$',
    re.IGNORECASE
)
_NORM_DOMAIN_RE = re.compile(
    r'\.(com|net|org|co|io|au|ca|uk|de|th|nz)\b', re.IGNORECASE
)
_NORM_DATE_RE = re.compile(
    r'\b(\d{4}[-_]\d{2}[-_]\d{2}|\d{8}|\d{6}|\d{2}[-_]\d{2}[-_]\d{4})\b'
)
_NORM_BIZ_RE = re.compile(
    r'\b(inc|llc|ltd|co|corp|group|agency|digital|media|solutions|services|'
    r'technologies|tech|consulting|marketing|studio|labs)\b',
    re.IGNORECASE
)
_NORM_PUNCT_RE = re.compile(r'[^\w\s]')


def normalise(name):
    """Full normalisation: strip ext, domain suffixes, dates, biz suffixes, punctuation; unidecode."""
    s = _FILE_EXT.sub('', name)
    s = unidecode(s)
    s = _NORM_DOMAIN_RE.sub('', s)
    s = _NORM_DATE_RE.sub('', s)
    s = _NORM_BIZ_RE.sub('', s)
    s = _NORM_PUNCT_RE.sub(' ', s)
    return re.sub(r'\s+', ' ', s).strip().lower()



def has_client_token_overlap(name, client_folder_names):
    file_tokens = {t for t in re.split(r'[\s\-_\.]+', _stem(name).lower()) if len(t) > 3}
    for folder in client_folder_names:
        folder_tokens = {t for t in re.split(r'[\s\-_\.]+', folder.lower()) if len(t) > 3}
        if file_tokens & folder_tokens:
            return True
    return False


_last_request_time = 0.0

def _rate_limited_sleep(min_gap=None):
    """Enforces minimum gap between Claude requests to prevent burst 429s."""
    if min_gap is None:
        min_gap = RATE_LIMIT_MIN_GAP
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < min_gap:
        time.sleep(min_gap - elapsed)
    _last_request_time = time.time()


def _batch_claude(files, folder_names, call_fn, label="Matching",
                  batch_size_override=None):
    """
    Generic batched Claude caller. Single source of truth for retry logic.
    All Claude call paths route through here.
    """
    effective_batch = batch_size_override or CLAUDE_BATCH_SIZE
    total   = -(-len(files) // effective_batch)
    results = []

    for i in range(0, len(files), effective_batch):
        batch     = files[i : i + effective_batch]
        batch_num = i // effective_batch + 1
        print(f"  [{label}] Batch {batch_num}/{total} ({len(batch)} files)...")

        for attempt in range(5):
            try:
                _rate_limited_sleep()
                results.extend(call_fn(batch, folder_names))
                break
            except Exception as e:
                print(f"  ⚠ FULL ERROR TYPE: {type(e).__name__}")
                print(f"  ⚠ FULL ERROR STR:  {str(e)}")
                print(f"  ⚠ FULL ERROR REPR: {repr(e)}")
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str or "rate" in err_str:
                    wait = (2 ** attempt) * 10 + random.uniform(0, 5)
                    wait = max(wait, 60)
                    print(f"  ⚠ 429 [{label}] batch {batch_num} attempt {attempt+1} — waiting {wait:.0f}s")
                    time.sleep(wait)
                elif attempt == 4:
                    print(f"  ✗ [{label}] batch {batch_num} gave up after 5 attempts: {e}")
                    break
                else:
                    time.sleep(3)

        if batch_num < total:
            time.sleep(INTER_BATCH_SLEEP)

    return results


def get_candidate_folders(filename, all_folder_names, top_n=50):
    """
    Returns the top_n most relevant folder names for a given filename.
    Uses the FULL normalised filename (not just the first segment) so that
    client names appearing anywhere in the filename are scored correctly.
    e.g. "Audit - Nov - Ferguson" correctly surfaces "Ferguson Roofing".
    """
    stem      = _FILE_EXT.sub('', filename)
    norm_full = normalise(stem)

    if len(norm_full) < 3:
        return all_folder_names[:top_n]

    matches = fuzz_process.extract(
        norm_full,
        all_folder_names,
        scorer=fuzz.token_set_ratio,   # token_set_ratio instead of token_sort_ratio
        limit=top_n,                   # handles partial matches and word order better
    )
    return [m[0] for m in matches]


def _claude_call(stray_files, folder_names, iteration_num=1):
    # ── Build focused folder list for this batch ──────────────────────────────
    focused_folders = set()

    # Strategy A: top-50 fuzzy candidates per file
    for f in stray_files:
        fname = f.get("name") or f.get("stray_file_name", "")
        focused_folders.update(get_candidate_folders(fname, folder_names, top_n=50))

    # Strategy B: token overlap safety net
    batch_tokens = set()
    for f in stray_files:
        fname = f.get("name") or f.get("stray_file_name", "")
        batch_tokens.update(
            t for t in re.split(r'[\s\-_\.]+', normalise(fname))
            if len(t) > 3
        )
    for folder in folder_names:
        folder_tokens = {
            t for t in re.split(r'[\s\-_\.]+', normalise(folder))
            if len(t) > 3
        }
        if folder_tokens & batch_tokens:
            focused_folders.add(folder)

    # Cap at MAX_CANDIDATE_FOLDERS — relevance-scored, not alphabetical
    if len(focused_folders) > MAX_CANDIDATE_FOLDERS:
        batch_norm = " ".join(sorted(batch_tokens))
        scored = sorted(
            focused_folders,
            key=lambda f: fuzz.token_set_ratio(normalise(f), batch_norm),
            reverse=True,
        )
        focused_list = scored[:MAX_CANDIDATE_FOLDERS]
    else:
        focused_list = sorted(focused_folders)

    print(f"  [DEBUG] Focused list sample (first 10): {focused_list[:10]}")
    print(f"  [DEBUG] Files sample (first 3): "
          f"{[f.get('name') or f.get('stray_file_name','') for f in stray_files[:3]]}")

    folder_list = "\n".join(f"- {n}" for n in focused_list)
    file_list   = "\n".join(
        f"- {f.get('name') or f.get('stray_file_name', '')}"
        for f in stray_files
    )

    iteration_preamble = (
        f"\nIMPORTANT: These files have already been through {iteration_num - 1} "
        f"previous matching attempt(s) and could not be matched. Look more carefully "
        f"at partial name matches, abbreviations, and alternative interpretations of "
        f"the filename before returning null.\n"
        if iteration_num > 1 else ""
    )

    prompt = f"""
You are helping organize a Google Drive folder for a digital analytics agency called {AGENCY_NAME}.

CLIENT FOLDERS (most relevant folders for this batch — copied exactly from our Drive):
{folder_list}

STRAY FILES (files sitting in the parent directory that need to be sorted):
{file_list}

TASK:
For each stray file, identify which client folder it most likely belongs to.
{iteration_preamble}
RULES:
- The client/project name is usually the first part of the filename before " - "
- Ignore generic document type words: Code Changes, Audit, Guide, Report, Document,
  Implementation, Measurement Roadmap, Requirement, Client Book, UTM, GTM, GA4, etc.
- Match partial names too (e.g. "Zepz" should match "Zepz - Mobile Tracking")
- CRITICAL: The folder field MUST be copied EXACTLY character-for-character from the
  CLIENT FOLDERS list above. Do not paraphrase, abbreviate, or rephrase any folder name.
  If you cannot find a match in the list above, return null.
- If a file clearly belongs to a client, confidence = "high"
- If it is a reasonable guess, confidence = "medium"
- If you genuinely cannot tell, set folder to null and confidence = "low"
- Return ONLY valid JSON with no explanation text outside the JSON

EXAMPLES (learn from these):

CORRECT MATCHES — these should always match:
- "Organizedgains.com - Code Changes.gdoc"              → folder: "Organizedgains.com"
- "Ferguson Roofing - GTM Code Changes.gdoc"            → folder: "Ferguson Roofing"
- "Zepz - Guide for AppsFlyer.gdoc"                     → folder: "Zepz - Mobile Tracking"
- "Zepz - Audit Guide for AppsFlyer.gdoc"               → folder: "Zepz - Mobile Tracking"
- "ZepZ - Sendwve - Developer Instructions.gdoc"        → folder: "Zepz - Mobile Tracking"
- "ZEPZ - anything.gdoc"                                → folder: "Zepz - Mobile Tracking"
- "NatoMath - Premierindoorstorage - Code Changes.gdoc" → folder: "Premierindoorstorage"
- "Amazingcorps - Access Details.gdoc"                  → folder: "Amazingcorps"
- "Amazingcorps Trello Report.gdoc"                     → folder: "Amazingcorps"

CORRECT NON-MATCHES — these should always be null:
- "230401_Marketing_Metrics.gsheet"      → folder: null
- "Marketing Scorecard_20220322.xlsx"    → folder: null
- "Sample - Analytics Audit.gdoc"        → folder: null
- "UTM Tracking Guide.gdoc"              → folder: null

CRITICAL RULES:
- Client name matching is CASE INSENSITIVE
- Extra spaces around " - " separators should be ignored
- Tool names (Trello, AppsFlyer, GTM, GA4) are never client names
- Return ONLY folder names that appear verbatim in the CLIENT FOLDERS list above

REQUIRED OUTPUT FORMAT:
{{
  "mappings": [
    {{
      "file": "exact filename as given",
      "folder": "exact folder name from CLIENT FOLDERS list, or null",
      "confidence": "high or medium or low",
      "reason": "one sentence explanation"
    }}
  ]
}}
"""

    print(f"  Calling Claude ({len(focused_list)} folders, {len(stray_files)} files)...")
    _rate_limited_sleep()
    response = _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    print(f"  [DEBUG] Raw response (first 800 chars):\n{raw[:800]}")
    raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
    try:
        mappings = json.loads(raw)["mappings"]
        matched_sample = [m for m in mappings if m.get("folder")][:5]
        print(f"  [DEBUG] {len(mappings)} mappings returned, {len(matched_sample)} with folder. Sample: {matched_sample}")
        return mappings
    except json.JSONDecodeError as e:
        print(f"  ⚠ Claude returned invalid JSON: {e}")
        print(f"  Raw: {raw[:300]}")
        return []


def collect_stray_files(scan_folder_ids):
    """Returns DataFrame of files sitting directly in each scan target folder."""
    rows = []
    for label, folder_id in scan_folder_ids:
        children = list_drive_children(folder_id)
        for f in children:
            if f["mimeType"] != "application/vnd.google-apps.folder":
                rows.append({
                    "file_id":           f["id"],
                    "stray_file_name":   f["name"],
                    "found_in":          label,
                    "_parent_folder_id": folder_id,
                })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["file_id", "stray_file_name", "found_in", "_parent_folder_id"]
    )


def collect_my_drive_files(folder_names, known_folder_ids):
    """Returns pre-filtered My Drive sharedWithMe files as a DataFrame."""
    kept, skipped = [], 0
    for f in list_shared_with_me_files():
        if any(p in known_folder_ids for p in f.get("parents", [])):
            continue
        # TOKEN OVERLAP PRE-FILTER — currently DISABLED.
        # When enabled: only keeps My Drive files where at least one 4+ char token
        # from the filename appears in any Analytics-ML folder name.
        # Effect: reduces My Drive input from ~3,173 → ~900 files before Claude.
        # Trade-off: may miss valid client files with unusual naming or abbreviations.
        # To enable: uncomment the 3 lines below.
        # if not has_client_token_overlap(f["name"], folder_names):
        #     skipped += 1
        #     continue
        kept.append({
            "file_id":           f["id"],
            "stray_file_name":   f["name"],
            "found_in":          "My Drive",
            "_parent_folder_id": None,
        })
    print(f"  My Drive: {len(kept)} kept, {skipped} skipped (generic/no-overlap)")
    return pd.DataFrame(kept) if kept else pd.DataFrame(
        columns=["file_id", "stray_file_name", "found_in", "_parent_folder_id"]
    )


def collect_client_folders():
    """
    Returns DataFrame of subfolders from 3 sources combined:
      1. Analytics-ML direct subfolders
      2. Analytics-ML / Marketlytics Old clients subfolders
      3. All Projects direct subfolders
    Deduplicates by name — first source wins.
    Filters out any name in EXCLUDED_FOLDERS.
    """
    sources = [
        ("Analytics-ML", None),
        ("Analytics-ML", "Marketlytics Old clients"),
        ("All Projects",  None),
    ]
    seen = {}
    rows = []

    for parent_name, child_name in sources:
        label = f"{parent_name}/{child_name}" if child_name else parent_name
        try:
            folder_id  = get_nested_folder_id(parent_name, child_name)
            folder_map = get_client_folder_map(folder_id)
            added = 0
            for name, fid in folder_map.items():
                if name in EXCLUDED_FOLDERS:
                    continue
                if name not in seen:
                    seen[name] = fid
                    rows.append({"folder_name": name, "folder_id": fid, "source": label})
                    added += 1
            print(f"  ✓ [{label}] {added} folders added")
        except ValueError as e:
            print(f"  ⚠ Skipping source '{label}': {e}")

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["folder_name", "folder_id", "source"]
    )
    print(f"  Total unique client folders: {len(df)}")
    return df


def fuzzy_match(stray_df, folders_df, threshold=85):
    """
    Simple, reliable fuzzy match.
    Extracts only the first segment before ' - ' from each filename,
    normalises it, and scores against all Analytics-ML folder names
    using token_sort_ratio only. No multi-strategy, no partial_ratio,
    no Jaro-Winkler.
    """
    folder_names  = folders_df["folder_name"].tolist()
    folder_id_map = dict(zip(folders_df["folder_name"], folders_df["folder_id"]))
    folder_norms  = {f: normalise(f) for f in folder_names}

    stray_df["suggested_folder"]    = None
    stray_df["suggested_folder_id"] = ""
    stray_df["confidence"]          = "low"
    stray_df["reason"]              = ""
    stray_df["status"]              = "UNMATCHED"

    matched = 0
    for idx in stray_df.index:
        name = stray_df.at[idx, "stray_file_name"]

        stem           = _FILE_EXT.sub('', name)
        parts          = re.split(r'\s*[-–]\s*', stem)
        candidate_raw  = parts[0].strip()
        candidate_norm = normalise(candidate_raw)

        if len(candidate_norm) < 4 or _is_generic_candidate(candidate_norm):
            continue

        best_folder, best_score = None, 0
        for folder_raw, folder_norm in folder_norms.items():
            score = fuzz.token_sort_ratio(candidate_norm, folder_norm)
            if score > best_score:
                best_score, best_folder = score, folder_raw

        if best_score >= threshold and best_folder:
            stray_df.at[idx, "suggested_folder"]    = best_folder
            stray_df.at[idx, "suggested_folder_id"] = folder_id_map.get(best_folder, "")
            stray_df.at[idx, "confidence"]          = "high" if best_score >= 92 else "medium"
            stray_df.at[idx, "reason"]              = (
                f"Fuzzy matched '{candidate_raw}' → '{best_folder}' (score {best_score})"
            )
            stray_df.at[idx, "status"] = "MATCHED"
            matched += 1

    print(f"  ✓ Fuzzy: {matched} MATCHED, {len(stray_df) - matched} → Claude")
    return stray_df


def _validate_claude_folder(claude_name, folder_names, folder_norms,
                             threshold=CLAUDE_VALIDATION_THRESHOLD):
    """
    Validates Claude's returned folder name against the known folder list.
    Fuzzy-matches claude_name back to the nearest real folder name.
    Returns (real_folder_name, score) or (None, 0) if no match clears threshold.
    """
    norm = normalise(claude_name)
    best_folder, best_score = None, 0
    for real_name, real_norm in folder_norms.items():
        score = fuzz.token_sort_ratio(norm, real_norm)
        if score > best_score:
            best_score, best_folder = score, real_name
    if best_score >= threshold:
        return best_folder, best_score
    return None, 0


# ── DEPRECATED — not called by drive_manager() ────────────────────────────────
# The iterative loop in drive_manager() performs Claude matching inline,
# using get_candidate_folders() for a focused folder list per batch.
# Do not call this function — use the iterative loop in drive_manager() instead.
# ──────────────────────────────────────────────────────────────────────────────
def run_claude_matching(stray_df, folders_df):
    """
    Step 2 of matching — runs only on UNMATCHED rows (fuzzy couldn't place them).
    Sends filenames to Claude in batches and updates match columns in-place.
    Claude's returned folder name is validated back against the real folder list
    before accepting, preventing hallucinated or slightly-wrong names from
    reaching the mover.
    """
    unmatched_mask = stray_df["status"] == "UNMATCHED"
    if not unmatched_mask.any():
        print("  No UNMATCHED files — skipping Claude matching.")
        return stray_df

    folder_names  = folders_df["folder_name"].tolist()
    folder_id_map = dict(zip(folders_df["folder_name"], folders_df["folder_id"]))
    folder_norms  = {f: normalise(f) for f in folder_names}

    files    = (stray_df.loc[unmatched_mask, ["stray_file_name"]]
                .rename(columns={"stray_file_name": "name"})
                .to_dict("records"))
    mappings = _batch_claude(files, folder_names, _claude_call, label="Claude")
    index    = {m["file"]: m for m in mappings}

    validated, rejected = 0, 0
    for idx in stray_df.index[unmatched_mask]:
        name = stray_df.at[idx, "stray_file_name"]
        m    = index.get(name)
        if m and m.get("folder") and m.get("confidence") != SKIP_CONFIDENCE:
            real_folder, val_score = _validate_claude_folder(
                m["folder"], folder_names, folder_norms
            )
            if real_folder:
                stray_df.at[idx, "suggested_folder"]    = real_folder
                stray_df.at[idx, "suggested_folder_id"] = folder_id_map.get(real_folder, "")
                stray_df.at[idx, "confidence"]          = m["confidence"]
                stray_df.at[idx, "reason"]              = (
                    f"{m['reason']} [validated: '{m['folder']}' → '{real_folder}' score {val_score}]"
                    if real_folder != m["folder"] else m["reason"]
                )
                stray_df.at[idx, "status"] = "MATCHED"
                validated += 1
            else:
                stray_df.at[idx, "confidence"] = "low"
                stray_df.at[idx, "reason"]     = (
                    f"Claude suggested '{m['folder']}' but no real folder matched (val score {val_score})"
                )
                rejected += 1
        else:
            stray_df.at[idx, "confidence"] = m.get("confidence", "low") if m else "low"
            stray_df.at[idx, "reason"]     = m.get("reason", "No mapping returned by Claude") if m else "No mapping returned by Claude"

    print(f"  Claude: {validated} validated, {rejected} rejected (hallucinated/unresolvable folder names)")
    return stray_df


def cluster_unmatched(filenames):
    """
    Groups similar unmatched filenames by client token using token_set_ratio >= 75.
    Uses ALL segments of the filename (not just first) to handle cases where
    the client name appears in the middle or end.
    e.g. "Audit - Nov - Ferguson Roofing" correctly clusters with
         "Report - Ferguson Roofing - GTM"
    Returns {representative_filename: [list_of_filenames_in_cluster]}.
    """
    clusters = {}

    def extract_best_token(fname):
        stem   = _stem(fname)
        parts  = [p.strip() for p in re.split(r'\s*[-–]\s*', stem)]
        # Filter out generic parts — pick longest non-generic segment
        # as the most likely client name
        non_generic = [
            p for p in parts
            if p and not _is_generic_candidate(normalise(p))
            and len(normalise(p)) >= 4
        ]
        if non_generic:
            # Return longest non-generic segment — most likely the client name
            return max(non_generic, key=len)
        # Fallback: return full stem if all parts are generic
        return stem

    for fname in filenames:
        token       = extract_best_token(fname)
        matched_rep = None
        best_score  = 0
        for rep in clusters:
            rep_token = extract_best_token(rep)
            score     = fuzz.token_set_ratio(
                normalise(token), normalise(rep_token)
            )
            if score >= 75 and score > best_score:
                best_score  = score
                matched_rep = rep
        if matched_rep:
            clusters[matched_rep].append(fname)
        else:
            clusters[fname] = [fname]

    return clusters


def run_suggest_new_folders(stray_df, existing_folder_names):
    """
    Second Claude pass: clusters UNMATCHED rows then sends one representative
    per cluster to Claude. Applies the same suggestion to all cluster members.
    """
    unmatched_idx = stray_df.index[stray_df["status"] == "UNMATCHED"]
    if unmatched_idx.empty:
        print("  No unmatched files — skipping suggestion step.")
        return stray_df

    unmatched_names = stray_df.loc[unmatched_idx, "stray_file_name"].tolist()
    clusters        = cluster_unmatched(unmatched_names)
    print(f"  Clustered {len(unmatched_names)} files into {len(clusters)} groups")

    rep_records = []
    for rep in clusters:
        found_in_series = stray_df.loc[stray_df["stray_file_name"] == rep, "found_in"]
        rep_records.append({
            "stray_file_name": rep,
            "found_in":        found_in_series.iloc[0] if not found_in_series.empty else "",
        })

    suggestions  = _batch_claude(rep_records, existing_folder_names, _suggest_claude_call, label="Suggesting")
    index        = {s["file"]: s for s in suggestions}
    folder_id_map = dict(zip(
        stray_df["suggested_folder"].dropna(),
        stray_df["suggested_folder_id"].dropna()
    ))
    # Build a proper id map from folders_df via existing_folder_names
    folder_norms = {f: normalise(f) for f in existing_folder_names}

    new_count, matched_count = 0, 0
    for rep, members in clusters.items():
        s = index.get(rep)
        sf_raw = (s or {}).get("suggested_folder") or ""
        if not sf_raw or str(sf_raw).strip().lower() == "null":
            continue

        # Validate: does suggested folder already exist?
        real_folder, val_score = _validate_claude_folder(
            sf_raw, existing_folder_names, folder_norms
        )

        for fname in members:
            mask = (stray_df["stray_file_name"] == fname) & (stray_df["status"] == "UNMATCHED")
            for idx in stray_df.index[mask]:
                if real_folder:
                    # Suggestion matches an existing folder — treat as MATCHED
                    stray_df.at[idx, "suggested_folder"]    = real_folder
                    stray_df.at[idx, "suggested_folder_id"] = ""
                    stray_df.at[idx, "confidence"]          = "medium"
                    stray_df.at[idx, "reason"]              = (
                        f"Suggest step resolved to existing folder '{real_folder}' (val score {val_score}). {s.get('reason', '')}"
                    )
                    stray_df.at[idx, "status"] = "MATCHED"
                    matched_count += 1
                else:
                    # Genuinely new folder
                    stray_df.at[idx, "suggested_folder"] = sf_raw
                    stray_df.at[idx, "reason"]           = f"Suggested new folder — does not exist yet. {s.get('reason', '')}"
                    stray_df.at[idx, "status"]           = "NEW_FOLDER"
                    new_count += 1
                    print(f"  NEW_FOLDER  '{fname}'  →  '{sf_raw}'")

    print(f"  ✓ {new_count} new folder suggestions, {matched_count} resolved to existing folders")
    return stray_df


def create_sheet_tab(stray_df):
    """Creates a new audit tab in Google Sheets. Returns (spreadsheet, sheet, tab_title, sheet_url)."""
    run_date    = datetime.now().strftime("%Y-%m-%d %H:%M")
    spreadsheet = _get_sheets_client().open_by_key(EXISTING_SHEET_ID)
    sheet       = spreadsheet.add_worksheet(
        title=f"Audit {run_date}", rows=len(stray_df) + 10, cols=13
    )

    # NOTE: suggested_folder_id is used internally during matching but is
    # intentionally excluded from the sheet — reviewers only need the folder name.
    # The mover does a live name → ID lookup at move time.
    headers = ["file_id", "stray_file_name", "found_in", "suggested_folder",
               "confidence", "reason", "status", "action", "manual_folder",
               "notes", "moved", "is_processed", "iteration_number"]

    export = stray_df[["file_id", "stray_file_name", "found_in",
                        "suggested_folder", "confidence", "reason", "status"]].copy()
    export["suggested_folder"] = export["suggested_folder"].fillna("— could not determine —")
    export["action"]        = ""
    export["manual_folder"] = export["status"].apply(
        lambda s: ARCHIVE_FOLDER if s == "UNMATCHED" else ""
    )
    export["notes"]         = stray_df["notes"].fillna("") if "notes" in stray_df.columns else ""
    export["moved"]         = ""
    export["is_processed"]  = export["status"].apply(
        lambda s: "1" if s == "MATCHED" else ""
    )
    export["iteration_number"] = ""
    # Fuzzy-matched files are already processed (no Claude pass needed)
    # UNMATCHED files start empty — waiting for Claude runs

    sheet.append_row(headers)
    sheet.append_rows(export[headers].values.tolist(), value_input_option="RAW")

    # Approve/Disapprove dropdown on action column (H = index 7)
    spreadsheet.batch_update({"requests": [{
        "setDataValidation": {
            "range": {
                "sheetId":          sheet._properties["sheetId"],
                "startRowIndex":    1,
                "endRowIndex":      len(stray_df) + 1,
                "startColumnIndex": 7,
                "endColumnIndex":   8,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": "Approve"},
                        {"userEnteredValue": "Disapprove"},
                    ],
                },
                "showCustomUi": True,
                "strict":       True,
            },
        }
    }]})

    url = f"https://docs.google.com/spreadsheets/d/{EXISTING_SHEET_ID}/edit"
    print(f"  ✓ Sheet tab 'Audit {run_date}' created ({len(stray_df)} rows)")
    return spreadsheet, sheet, f"Audit {run_date}", url


def _suggest_claude_call(batch, existing_folder_names):
    """Single Claude call for one batch of unmatched files."""
    file_list     = "\n".join(
        f"- file: \"{r['stray_file_name']}\" | found_in: \"{r.get('found_in', '')}\""
        for r in batch
    )
    existing_list = "\n".join(f"- {n}" for n in sorted(existing_folder_names))

    prompt = f"""
You are helping a digital analytics agency called {AGENCY_NAME} organize their Google Drive.

The following files could NOT be matched to any existing client folder.
Your job is to suggest what NEW folder name should be created in Analytics-ML for each file.

EXISTING FOLDERS IN Analytics-ML (do NOT suggest these — they already exist):
{existing_list}

UNMATCHED FILES (with their current location):
{file_list}

RULES:
- Extract the client/project name from each filename (usually before the first " - ")
- If multiple files clearly belong to the same client (even with slight name variations),
  suggest the SAME folder name for all of them
- Folder name should be clean, properly cased, and professional
  e.g. "techwageracompany" → "Tech Wagera", "NJM roofing inc" → "NJM Roofing"
- If a file is truly generic with no client name (like "data.xlsx", "report.csv"),
  suggest folder: null
- Do NOT suggest folders that already exist in the list above
- Return ONLY valid JSON

OUTPUT FORMAT:
{{
  "suggestions": [
    {{
      "file": "exact filename as given",
      "suggested_folder": "Clean Folder Name to Create or null",
      "reason": "one sentence explanation"
    }}
  ]
}}
"""
    _rate_limited_sleep()
    response = _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw      = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)["suggestions"]
    except json.JSONDecodeError as e:
        print(f"  ⚠ Claude suggestion JSON error: {e}")
        return []


def _validate_new_folder_call(pairs):
    """Calls Claude to validate NEW_FOLDER suggestions.
    Returns list of {file, folder, valid} dicts.
    A suggestion is valid if at least one significant word from the folder name
    appears in the file name (or vice versa), or there is a clear abbreviated/
    domain-name relationship.
    """
    pair_list = "\n".join(
        f"- file: \"{p['file']}\" | folder: \"{p['folder']}\""
        for p in pairs
    )
    prompt = f"""
You are validating whether suggested folder names are justified for Google Drive files
at a digital analytics agency called {AGENCY_NAME}.

RULE: A folder suggestion is VALID if at least one significant word from the folder name
appears in the file name (or vice versa), OR there is a clear abbreviated / domain-name
relationship between them.
A suggestion is INVALID if there is no meaningful word overlap or recognisable connection.

EXAMPLES:
- file="Events", folder="Vocal" → valid: false  (no shared word; "Vocal" not in "Events")
- file="Trupanion.com.au - GA4 Funnel Events Breakdown", folder="Trupanion" → valid: true  ("Trupanion" appears in filename)
- file="Teamwork - Custom Fields test", folder="Teamwork Data Studio" → valid: true  ("Teamwork" appears in filename)
- file="AgentSea - UTM Tagging", folder="Agenstsea" → valid: true  (same client, minor spelling variant)
- file="Copy of code", folder="Danone" → valid: false  (no connection)
- file="Comparison Sheet", folder="Connecting Threads" → valid: false  (generic filename, no client signal)

PAIRS TO VALIDATE:
{pair_list}

Return ONLY valid JSON — no prose, no markdown fences:
{{
  "validations": [
    {{
      "file": "exact filename as given",
      "folder": "exact folder name as given",
      "valid": true
    }}
  ]
}}
"""
    _rate_limited_sleep()
    response = _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)["validations"]
    except json.JSONDecodeError as e:
        print(f"  ⚠ Validation JSON error: {e}")
        return []


def _normalize_folder_names_call(clusters_batch):
    """
    Takes a list of clusters (each a list of similar folder name variants).
    Returns {variant -> canonical} for every variant in the batch.
    """
    cluster_lines = "\n".join(
        f"Group {i+1}: {' | '.join(members)}"
        for i, members in enumerate(clusters_batch)
    )
    prompt = f"""
You are cleaning up folder name variants for Google Drive at a digital analytics agency called {AGENCY_NAME}.

For each group of similar names, pick ONE canonical folder name.
Rules:
- Fix typos and spacing  (e.g. "SobaHome" → "Soba Homes", "Truppanion" → "Trupanion")
- Use proper title casing and spacing
- Keep meaningful regional/product suffixes that distinguish clients
  (e.g. "Trupanion AU" and "Trupanion US" should stay separate)
- Remove generic business suffixes like "Ltd", "Inc", "LLC" unless clearly part of the brand
- If a group has only one member, still return it cleaned up

GROUPS:
{cluster_lines}

Return ONLY valid JSON — no prose, no markdown fences:
{{
  "normalizations": [
    {{
      "canonical": "Clean Canonical Name",
      "variants": ["VariantA", "VariantB"]
    }}
  ]
}}
"""
    _rate_limited_sleep()
    response = _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)["normalizations"]
    except json.JSONDecodeError as e:
        print(f"  ⚠ Normalization JSON error: {e}")
        return {}
    result = {}
    for item in data:
        canonical = item.get("canonical", "")
        for variant in item.get("variants", []):
            if variant and canonical:
                result[variant] = canonical
    return result


def _normalize_claude_call(clusters_batch, _unused):
    """Adapter so _batch_claude() can call _normalize_folder_names_call()."""
    raw_clusters = [item["members"] for item in clusters_batch]
    result = _normalize_folder_names_call(raw_clusters)
    return [result] if result else []


def normalize_new_folder_names(stray_df):
    """
    Clusters NEW_FOLDER suggested_folder variants using fuzzy matching (threshold 80),
    then asks Claude to pick one canonical name per cluster.
    Singletons are skipped (no API call needed).
    Updates suggested_folder in-place and returns the DataFrame.
    """
    nf_mask = stray_df["status"] == "NEW_FOLDER"
    if not nf_mask.any():
        return stray_df

    unique_names = [n for n in stray_df.loc[nf_mask, "suggested_folder"].dropna().unique() if n]
    if len(unique_names) <= 1:
        return stray_df

    print(f"\nStep 6c — Normalizing {len(unique_names)} unique NEW_FOLDER name(s)...")

    norms = {n: normalise(n) for n in unique_names}
    used  = set()
    clusters = []

    for name in unique_names:
        if name in used:
            continue
        cluster = [name]
        used.add(name)
        for other in unique_names:
            if other in used:
                continue
            if fuzz.token_sort_ratio(norms[name], norms[other]) >= 80:
                cluster.append(other)
                used.add(other)
        clusters.append(cluster)

    multi = [c for c in clusters if len(c) > 1]
    print(f"  {len(multi)} variant cluster(s) to normalize, {len(clusters) - len(multi)} singleton(s) unchanged")

    if not multi:
        print("  Nothing to normalize.")
        return stray_df

    norm_map = {}
    batch_input = [{"members": c} for c in multi]
    raw_results = _batch_claude(
        batch_input,
        [],
        _normalize_claude_call,
        label="Normalizing",
        batch_size_override=50,
    )
    for result in raw_results:
        if isinstance(result, dict):
            norm_map.update(result)

    applied = 0
    for idx in stray_df.index[nf_mask]:
        old = stray_df.at[idx, "suggested_folder"]
        new = norm_map.get(old)
        if new and new != old:
            stray_df.at[idx, "suggested_folder"] = new
            applied += 1

    print(f"  ✓ {applied} row(s) updated across {len(multi)} cluster(s)")
    return stray_df


def _validate_claude_call(pairs_batch, _unused):
    """Adapter so _batch_claude() can call _validate_new_folder_call()."""
    return _validate_new_folder_call(pairs_batch)


def validate_new_folder_suggestions(stray_df, batch_size=50):
    """
    Claude validation pass on all NEW_FOLDER rows.
    Checks word-level relevance between file name and suggested folder name.
    Rows that fail validation get 'validation failed' appended to their notes column.
    """
    nf_idx = stray_df.index[stray_df["status"] == "NEW_FOLDER"]
    if nf_idx.empty:
        print("  No NEW_FOLDER rows — skipping validation.")
        return stray_df

    if "notes" not in stray_df.columns:
        stray_df["notes"] = ""

    pairs = [
        {
            "file":   stray_df.at[idx, "stray_file_name"],
            "folder": stray_df.at[idx, "suggested_folder"],
        }
        for idx in nf_idx
    ]
    print(f"\nStep 6b — Validating {len(pairs)} NEW_FOLDER suggestions...")

    results = _batch_claude(
        pairs,
        [],
        _validate_claude_call,
        label="Validating",
        batch_size_override=batch_size,
    )
    valid_map = {r["file"]: bool(r.get("valid", True)) for r in results if "file" in r}

    failed = 0
    for idx in nf_idx:
        fname = stray_df.at[idx, "stray_file_name"]
        if not valid_map.get(fname, True):
            existing = str(stray_df.at[idx, "notes"] or "").strip()
            stray_df.at[idx, "notes"] = (existing + " | validation failed").lstrip(" | ")
            failed += 1

    print(f"  ✓ Validation complete: {len(pairs) - failed} valid, {failed} flagged")
    return stray_df


def process_batch(batch_df, folders_df, folder_names, sheet,
                  sheet_row_map, iteration_number=1):
    """
    Runs a SINGLE Claude matching pass on a batch of UNMATCHED files.
    One pass per Cloud Function invocation — iterations spread across runs.

    Pipeline:
      1. Single Claude pass (batches of 50, inter-batch sleep between batches)
      2. Validate Claude folder names against real list
      3. Verification pass — reset invalid matches
      4. Write results + is_processed=1 + iteration_number to sheet
    """
    print(f"\n  Processing {len(batch_df)} files — iteration {iteration_number}")

    folder_id_map = dict(zip(folders_df["folder_name"], folders_df["folder_id"]))
    folder_norms  = {f: normalise(f) for f in folder_names}

    # ── Single Claude pass ────────────────────────────────────────────────────
    unmatched_mask = batch_df["status"] == "UNMATCHED"
    if unmatched_mask.any():
        files_for_claude = (
            batch_df.loc[unmatched_mask, ["stray_file_name"]]
            .rename(columns={"stray_file_name": "name"})
            .to_dict("records")
        )

        def _claude_call_wrapper(batch, fn):
            return _claude_call(batch, fn, iteration_num=iteration_number)

        mappings = _batch_claude(
            files_for_claude,
            folder_names,
            _claude_call_wrapper,
            label=f"Claude run {iteration_number}",
            batch_size_override=50,
        )
        mapping_index = {m["file"]: m for m in mappings}

        validated, rejected, low_conf = 0, 0, 0
        for idx in batch_df.index[unmatched_mask]:
            name = batch_df.at[idx, "stray_file_name"]
            m    = mapping_index.get(name)
            if m and m.get("folder") and m.get("confidence") != SKIP_CONFIDENCE:
                real_folder, val_score = _validate_claude_folder(
                    m["folder"], folder_names, folder_norms
                )
                if real_folder:
                    batch_df.at[idx, "suggested_folder"]    = real_folder
                    batch_df.at[idx, "suggested_folder_id"] = folder_id_map.get(real_folder, "")
                    batch_df.at[idx, "confidence"]          = m["confidence"]
                    batch_df.at[idx, "reason"]              = (
                        f"[run {iteration_number}] {m['reason']}"
                    )
                    batch_df.at[idx, "status"]              = "MATCHED"
                    validated += 1
                else:
                    batch_df.at[idx, "reason"] = (
                        f"Claude suggested '{m['folder']}' — "
                        f"no real folder matched (val score {val_score})"
                    )
                    rejected += 1
            elif m:
                batch_df.at[idx, "confidence"] = m.get("confidence", "low")
                batch_df.at[idx, "reason"]     = m.get("reason", "No mapping returned")
                low_conf += 1
            else:
                low_conf += 1

        print(f"  Claude run {iteration_number}: {validated} matched, "
              f"{rejected} rejected, {low_conf} low-confidence/no-return")

    # ── Verification pass ─────────────────────────────────────────────────────
    valid_folder_set = set(folder_names)
    reset_count = 0
    for idx in batch_df.index[batch_df["status"] == "MATCHED"]:
        sf = batch_df.at[idx, "suggested_folder"]
        if sf and sf not in valid_folder_set:
            batch_df.at[idx, "status"]          = "UNMATCHED"
            batch_df.at[idx, "suggested_folder"] = None
            batch_df.at[idx, "confidence"]       = "low"
            batch_df.at[idx, "reason"]           = (
                f"Verification failed: '{sf}' not in folder list"
            )
            reset_count += 1
    if reset_count:
        print(f"  ⚠ Verification reset {reset_count} rows → UNMATCHED")

    # ── Write all rows to sheet ───────────────────────────────────────────────
    # Every row in this batch is marked is_processed=1 regardless of outcome.
    # UNMATCHED rows will appear in the final suggestion pass.
    mark_rows_processed(sheet, batch_df, sheet_row_map,
                        iteration_number=iteration_number)

    matched   = int((batch_df["status"] == "MATCHED").sum())
    unmatched = int((batch_df["status"] == "UNMATCHED").sum())
    print(f"\n  Run {iteration_number} batch done: "
          f"MATCHED={matched} UNMATCHED={unmatched}")
    return batch_df


def run_final_suggestion_pass(sheet, folders_df, folder_names,
                              batch_size=500):
    """
    Processes up to batch_size UNMATCHED rows per invocation.
    Writes after each stage (suggest, normalize, validate) to avoid
    SSL timeout on long-running connections.
    Returns remaining unmatched count (0 = all done).
    """
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return 0

    headers    = all_values[0]
    col        = lambda name: headers.index(name) if name in headers else None
    status_col = col("status")
    name_col   = col("stray_file_name")
    found_col  = col("found_in")

    if any(c is None for c in [status_col, name_col, found_col]):
        print("  ⚠ Required columns missing — skipping suggestion pass")
        return 0

    # Collect up to batch_size UNMATCHED rows
    unmatched_rows = []
    for i, row in enumerate(all_values[1:], start=2):
        if len(row) > status_col and row[status_col].strip() == "UNMATCHED":
            unmatched_rows.append({
                "stray_file_name": row[name_col] if len(row) > name_col else "",
                "found_in":        row[found_col] if len(row) > found_col else "",
                "_sheet_row":      i,
            })
        if len(unmatched_rows) >= batch_size:
            break

    if not unmatched_rows:
        print("  No UNMATCHED rows — suggestion pass complete.")
        return 0

    total_unmatched = sum(
        1 for r in all_values[1:]
        if len(r) > status_col and r[status_col].strip() == "UNMATCHED"
    )
    print(f"\n  Suggestion pass: processing {len(unmatched_rows)} of "
          f"{total_unmatched} remaining UNMATCHED files...")

    # Build DataFrame
    suggestion_df = pd.DataFrame(unmatched_rows)
    suggestion_df["suggested_folder"]    = None
    suggestion_df["suggested_folder_id"] = ""
    suggestion_df["confidence"]          = "low"
    suggestion_df["reason"]              = ""
    suggestion_df["notes"]               = ""
    suggestion_df["status"]              = "UNMATCHED"

    sheet_row_map = {
        i: row["_sheet_row"]
        for i, row in enumerate(unmatched_rows)
    }

    iter_num = get_current_iteration_number(sheet)

    # Stage 1: Suggest — write immediately after
    suggestion_df = run_suggest_new_folders(suggestion_df, folder_names)
    print("  Writing suggestion results...")
    _, sheet = _get_fresh_sheet(sheet.title)
    mark_rows_processed(sheet, suggestion_df, sheet_row_map,
                        iteration_number=iter_num)

    # Stage 2: Normalize — write immediately after
    suggestion_df = normalize_new_folder_names(suggestion_df)
    print("  Writing normalize results...")
    _, sheet = _get_fresh_sheet(sheet.title)
    mark_rows_processed(sheet, suggestion_df, sheet_row_map,
                        iteration_number=iter_num)

    # Stage 3: Validate — write immediately after
    suggestion_df = validate_new_folder_suggestions(suggestion_df)
    print("  Writing validation results...")
    _, sheet = _get_fresh_sheet(sheet.title)
    mark_rows_processed(sheet, suggestion_df, sheet_row_map,
                        iteration_number=iter_num)

    nf        = int((suggestion_df["status"] == "NEW_FOLDER").sum())
    um        = int((suggestion_df["status"] == "UNMATCHED").sum())
    remaining = total_unmatched - len(unmatched_rows)
    print(f"  ✓ Batch done: NEW_FOLDER={nf} UNMATCHED={um} "
          f"| {remaining} more UNMATCHED rows in next run")
    return remaining


def send_audit_email(sheet_url, matched, unmatched, total):
    move_url = f"{FUNCTION_URL}?action=move"
    run_date = datetime.now().strftime("%B %Y")
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:600px;margin:0 auto">
      <div style="background:linear-gradient(135deg,#1e40af,#3b82f6);
                  padding:24px;border-radius:8px 8px 0 0">
        <h2 style="color:#fff;margin:0">🗂️ Monthly Drive Audit</h2>
        <p style="color:rgba(255,255,255,0.8);margin:4px 0 0">
          {ROOT_FOLDER} · {run_date}
        </p>
      </div>
      <div style="background:#f8fafc;padding:24px;border:1px solid #e2e8f0;
                  border-top:none;border-radius:0 0 8px 8px">

        <div style="display:flex;gap:12px;margin-bottom:24px">
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#64748b">{total}</div>
            <div style="color:#94a3b8;font-size:13px">Stray Files</div>
          </div>
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#22c55e">{matched}</div>
            <div style="color:#94a3b8;font-size:13px">Matched</div>
          </div>
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#f59e0b">{unmatched}</div>
            <div style="color:#94a3b8;font-size:13px">Need Review</div>
          </div>
        </div>

        <p style="color:#374151;margin:0 0 20px">
          Claude has analysed your stray files. Open the sheet, review each row,
          and set the <strong>Action</strong> column to
          <em>Approve</em> or <em>Disapprove</em>.
        </p>

        <a href="{sheet_url}"
           style="display:inline-block;background:#2563eb;color:#fff;
                  padding:14px 28px;border-radius:6px;text-decoration:none;
                  font-weight:600;font-size:15px">
          👉 Open Approval Sheet
        </a>

        <div style="margin-top:16px"></div>

        <a href="{move_url}"
           style="display:inline-block;background:#16a34a;color:#fff;
                  padding:14px 28px;border-radius:6px;text-decoration:none;
                  font-weight:600;font-size:15px">
          🚚 Move Approved Files
        </a>

        <p style="color:#ef4444;font-size:12px;margin-top:16px">
          ⚠️ Only click <strong>Move Approved Files</strong> AFTER you have
          reviewed the sheet and set all Action dropdowns.
        </p>
      </div>
    </div>"""
    send_email(
        f"[Drive Audit] {run_date} — {total} files need review",
        html
    )


def create_folder_in_analytics_ml(folder_name):
    """Creates a new subfolder inside the configured root folder."""
    analytics_ml_id = get_nested_folder_id(ROOT_FOLDER)
    folder = _get_drive_service().files().create(
        body={
            "name":     folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents":  [analytics_ml_id],
        },
        fields="id"
    ).execute()
    print(f"  📁 Created new folder: '{folder_name}' in Analytics-ML")
    return folder["id"]


def run_mover(tab_name=None):
    # Build full folder map across ALL scan targets
    all_client_folders = {}
    for parent_name, child_name in SCAN_TARGETS:
        label = f"{parent_name}/{child_name}" if child_name else parent_name
        try:
            fid      = get_nested_folder_id(parent_name, child_name)
            children = list_drive_children(fid)
            for f in children:
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    all_client_folders[f["name"]] = f["id"]
        except ValueError as e:
            print(f"  ⚠ Skipping scan target '{label}': {e}")

    _, sheet = get_latest_sheet_tab(tab_name)
    rows     = sheet.get_all_records()
    print(f"  Reading approvals from tab: '{sheet.title}'")
    print(f"  Total rows: {len(rows)}\n")

    all_values = sheet.get_all_values()
    headers    = all_values[0] if all_values else []
    try:
        moved_col_idx = headers.index("moved") + 1   # 1-based for gspread
    except ValueError:
        moved_col_idx = None   # old sheet without moved column

    moved_files, skipped_files, error_files = [], [], []

    for row_num, row in enumerate(rows, start=2):
        action    = str(row.get("action",        "")).strip().lower()
        file_id   = str(row.get("file_id",        "")).strip()
        fname     = row.get("stray_file_name",   "")
        suggested = row.get("suggested_folder",  "")
        manual    = str(row.get("manual_folder",  "")).strip()
        status    = str(row.get("status",         "")).strip()

        already_moved = str(row.get("moved", "")).strip().lower()
        if already_moved == "yes":
            skipped_files.append({"file": fname, "reason": "Already moved in previous run"})
            continue

        # manual_folder overrides suggested_folder if filled in
        folder = manual if manual and manual != "— could not determine —" else suggested

        if action != "approve":
            reason = "Disapproved" if action == "disapprove" else f"No action ('{action}')"
            print(f"  ⏭  {reason}: {fname}")
            skipped_files.append({"file": fname, "reason": reason})
            continue

        # Check if manual_folder contains a raw Drive folder ID
        # (IDs are 25-44 char alphanumeric strings starting with a letter)
        import re as _re
        def _is_folder_id(val):
            """Detects if val looks like a Drive folder ID (25+ alphanumeric chars)."""
            import re as _re
            return bool(
                val
                and _re.match(r'^[a-zA-Z0-9_\-]{20,}$', val.strip())
                and ' ' not in val.strip()
            )

        def _verify_folder_id(fid):
            """Verifies a folder ID exists in Drive and returns its name, or None."""
            try:
                meta = _get_drive_service().files().get(
                    fileId=fid.strip(),
                    fields="id, name, mimeType"
                ).execute()
                if meta.get("mimeType") == "application/vnd.google-apps.folder":
                    print(f"  📎 Folder ID resolved: '{meta['name']}' ({fid.strip()})")
                    return fid.strip()
                else:
                    print(f"  ⚠ ID {fid.strip()} is not a folder — it's {meta.get('mimeType')}")
                    return None
            except Exception as e:
                print(f"  ⚠ Folder ID {fid.strip()} not found or inaccessible: {e}")
                return None

        # Determine dest_id — folder ID takes priority over name lookup
        if _is_folder_id(folder):
            dest_id = _verify_folder_id(folder)
        else:
            dest_id = all_client_folders.get(folder)
            if not dest_id and folder:
                # Attempt live Drive search as fallback for names not in scan targets
                try:
                    resp = _get_drive_service().files().list(
                        q=f"name='{folder}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                        fields="files(id, name)",
                        pageSize=5
                    ).execute()
                    hits = resp.get("files", [])
                    if hits:
                        dest_id = hits[0]["id"]
                        print(f"  🔍 Live search found '{folder}': {dest_id}")
                    else:
                        print(f"  ⚠ Live search also found nothing for '{folder}'")
                except Exception as e:
                    print(f"  ⚠ Live search failed for '{folder}': {e}")

        # If not found and status is NEW_FOLDER, create it in Analytics-ML
        if not dest_id and status == "NEW_FOLDER" and folder:
            try:
                dest_id = create_folder_in_analytics_ml(folder)
                all_client_folders[folder] = dest_id   # cache to avoid duplicate creation
            except Exception as e:
                err = f"Failed to create folder '{folder}': {e}"
                print(f"  ⚠  {err}")
                error_files.append({"file": fname, "error": err})
                continue

        if not dest_id:
            err = f"Folder not found in Drive: '{folder}'"
            print(f"  ⚠  {err}")
            error_files.append({"file": fname, "error": err})
            continue

        try:
            move_file(file_id, dest_id)
            print(f"  🚚 Moved: '{fname}'  →  '{folder}'")
            moved_files.append({"file": fname, "folder": folder})
            if moved_col_idx:
                sheet.update_cell(row_num, moved_col_idx, "yes")
            time.sleep(0.5)  # avoid Drive API burst rate limit
        except Exception as e:
            err = str(e) or repr(e) or f"{type(e).__name__}: {e.args}"
            print(f"  ❌ Error moving '{fname}': {err}")
            error_files.append({"file": fname, "error": err})

    send_mover_email(moved_files, skipped_files, error_files)
    return moved_files, skipped_files, error_files


def send_mover_email(moved_files, skipped_files, error_files):
    def table_rows(items, keys, colors):
        return "".join(
            "<tr>" + "".join(
                f'<td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;color:{colors[i]}">'
                f'{item[k]}</td>'
                for i, k in enumerate(keys)
            ) + "</tr>"
            for item in items
        )

    moved_rows   = table_rows(moved_files,   ["file","folder"], ["#1e293b","#166534"])
    skipped_rows = table_rows(skipped_files, ["file","reason"], ["#1e293b","#92400e"])
    error_rows   = table_rows(error_files,   ["file","error"],  ["#1e293b","#991b1b"])

    def section(title, color, bg, header_bg, rows, cols):
        if not rows:
            return ""
        headers = "".join(f'<th style="padding:10px 12px;text-align:left">{c}</th>' for c in cols)
        return f"""
        <h3 style="color:{color};margin-top:24px">{title}</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:{bg}">
          <thead><tr style="background:{header_bg}">{headers}</tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:700px;margin:0 auto">
      <div style="background:linear-gradient(135deg,#166534,#22c55e);
                  padding:24px;border-radius:8px 8px 0 0">
        <h1 style="color:#fff;margin:0;font-size:20px">🚚 Drive Mover Report</h1>
        <p style="color:rgba(255,255,255,.8);margin:4px 0 0;font-size:13px">
          {datetime.now().strftime("%B %d, %Y — %H:%M")}
        </p>
      </div>
      <div style="background:#f8fafc;padding:24px;border:1px solid #e2e8f0;
                  border-top:none;border-radius:0 0 8px 8px">

        <div style="display:flex;gap:12px;margin-bottom:24px">
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#22c55e">{len(moved_files)}</div>
            <div style="color:#94a3b8;font-size:12px">Moved</div>
          </div>
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#f59e0b">{len(skipped_files)}</div>
            <div style="color:#94a3b8;font-size:12px">Skipped</div>
          </div>
          <div style="flex:1;background:#fff;border-radius:8px;padding:16px;
                      text-align:center;border:1px solid #e2e8f0">
            <div style="font-size:28px;font-weight:700;color:#ef4444">{len(error_files)}</div>
            <div style="color:#94a3b8;font-size:12px">Errors</div>
          </div>
        </div>

        {section("✅ Moved",    "#166534","#f0fdf4","#dcfce7", moved_rows,   ["File","Moved To"])}
        {section("⏭ Skipped",  "#92400e","#fffbeb","#fef3c7", skipped_rows, ["File","Reason"])}
        {section("❌ Errors",   "#991b1b","#fef2f2","#fee2e2", error_rows,   ["File","Error"])}

      </div>
    </div>"""

    send_email(
        f"[Drive Mover] {len(moved_files)} moved · {len(error_files)} errors · "
        f"{datetime.now().strftime('%b %d')}",
        html
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLOUD FUNCTION ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

@functions_framework.http
def drive_manager(request):
    """
    Single HTTP entry point.
    ?action=move  → runs mover
    (no param)    → runs audit
    """
    action = request.args.get("action", "audit")
    print(f"\n{'='*50}")
    print(f"  Mode: {action.upper()} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")

    try:
        # ── MOVER MODE ────────────────────────────────────────────────────────
        if action == "move":
            tab_name = request.args.get("tab", None)
            moved, skipped, errors = run_mover(tab_name=tab_name)
            return (
                json.dumps({
                    "status":  "success",
                    "mode":    "mover",
                    "moved":   len(moved),
                    "skipped": len(skipped),
                    "errors":  len(errors),
                }),
                200,
                {"Content-Type": "application/json"}
            )

        else:
            BATCH_SIZE = BATCH_SIZE_DEFAULT

            # Step 1: Scan targets
            scan_folder_ids = []
            for parent_name, child_name in SCAN_TARGETS:
                label = f"{parent_name}/{child_name}" if child_name else parent_name
                try:
                    fid = get_nested_folder_id(parent_name, child_name)
                    scan_folder_ids.append((label, fid))
                    print(f"  ✓ {label}")
                except ValueError as e:
                    print(f"  ⚠ Skipping: {e}")

            if not scan_folder_ids:
                return (json.dumps({"status": "error",
                                    "message": "No scan folders found."}),
                        500, {"Content-Type": "application/json"})

            # Step 2: Client folder pool (needed every run for Claude matching)
            print("\nStep 2 — Building client folder list...")
            folders_df   = collect_client_folders()
            folder_names = folders_df["folder_name"].tolist()
            print(f"  {len(folders_df)} folders loaded")

            # Step 3: Check if today's tab exists
            print("\nStep 3 — Checking for today's audit tab...")
            spreadsheet, sheet = get_today_audit_tab()

            if sheet is None:
                # ── FIRST RUN ─────────────────────────────────────────────────
                print("\nFirst run — collecting all stray files...")
                scan_df   = collect_stray_files(scan_folder_ids)
                known_ids = (set(folders_df["folder_id"]) |
                             {fid for _, fid in scan_folder_ids})
                if SCAN_SHARED_WITH_ME:
                    my_drive_df = collect_my_drive_files(folder_names, known_ids)
                else:
                    my_drive_df = pd.DataFrame(
                        columns=["file_id", "stray_file_name", "found_in", "_parent_folder_id"]
                    )
                    print("  SharedWithMe scan disabled (set scan_shared_with_me: true in config.yaml to enable)")
                stray_df    = pd.concat([scan_df, my_drive_df], ignore_index=True)
                print(f"  {len(stray_df)} total stray files")

                if stray_df.empty:
                    return (json.dumps({"status": "success",
                                        "message": "No stray files found."}),
                            200, {"Content-Type": "application/json"})

                # Fuzzy match ALL files — free, no API cost
                print(f"\nFuzzy matching all {len(stray_df)} files "
                      f"(threshold {FUZZY_THRESHOLD})...")
                stray_df = fuzzy_match(stray_df, folders_df,
                                       threshold=FUZZY_THRESHOLD)
                fuzzy_matched = int((stray_df["status"] == "MATCHED").sum())
                print(f"  Fuzzy: {fuzzy_matched} MATCHED, "
                      f"{len(stray_df) - fuzzy_matched} → Claude queue")

                # Create sheet — fuzzy MATCHED rows get is_processed=1
                print("\nCreating sheet tab...")
                spreadsheet, sheet, tab_title, sheet_url = \
                    create_sheet_tab(stray_df)

            else:
                # ── SUBSEQUENT RUN ────────────────────────────────────────────
                tab_title = sheet.title
                sheet_url = (f"https://docs.google.com/spreadsheets/d/"
                             f"{EXISTING_SHEET_ID}/edit")
                print(f"\nContinuing — tab: '{tab_title}'")

                # If tab is marked complete, nothing left to do
                if tab_title.endswith("✓"):
                    print("  ✓ Audit already complete — email was sent in a previous run.")
                    return (
                        json.dumps({
                            "status":  "success",
                            "mode":    "audit",
                            "message": "Audit already complete for today.",
                            "tab":     tab_title,
                            "sheet":   sheet_url,
                        }),
                        200,
                        {"Content-Type": "application/json"}
                    )

            # ── Decide what this run should do ────────────────────────────────
            if check_all_processed(sheet):
                all_values      = sheet.get_all_values()
                headers_row     = all_values[0]
                status_col      = headers_row.index("status")
                unmatched_count = sum(
                    1 for r in all_values[1:]
                    if len(r) > status_col
                    and r[status_col].strip() == "UNMATCHED"
                )

                if unmatched_count > 0:
                    # Process next 500 UNMATCHED — resumable across runs
                    print(f"\n  Suggestion pass — {unmatched_count} UNMATCHED remain...")
                    _, sheet  = _get_fresh_sheet(sheet.title)
                    remaining = run_final_suggestion_pass(
                        sheet, folders_df, folder_names, batch_size=500
                    )
                    if remaining > 0:
                        # More UNMATCHED left — do NOT send email yet
                        msg = (f"Suggestion batch complete. "
                               f"~{remaining} UNMATCHED rows remain for next run.")
                        print(f"\n  {msg}")
                    else:
                        # Suggestion pass fully done — tally and send email
                        _, sheet      = _get_fresh_sheet(sheet.title)
                        all_values    = sheet.get_all_values()
                        status_col    = all_values[0].index("status")
                        matched_cnt   = sum(1 for r in all_values[1:]
                                            if len(r) > status_col
                                            and r[status_col].strip() == "MATCHED")
                        unmatched_cnt = sum(1 for r in all_values[1:]
                                            if len(r) > status_col
                                            and r[status_col].strip() == "UNMATCHED")
                        nf_cnt        = sum(1 for r in all_values[1:]
                                            if len(r) > status_col
                                            and r[status_col].strip() == "NEW_FOLDER")
                        total_cnt     = len(all_values) - 1
                        print(f"\n✅ Sending audit email — "
                              f"MATCHED={matched_cnt} NEW_FOLDER={nf_cnt} "
                              f"UNMATCHED={unmatched_cnt}")
                        send_audit_email(sheet_url, matched_cnt,
                                         unmatched_cnt, total_cnt)
                        # Mark tab as complete — prevents re-sending email on subsequent runs
                        try:
                            sheet.spreadsheet.batch_update({"requests": [{
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": sheet._properties["sheetId"],
                                        "title":   sheet.title + " ✓",
                                    },
                                    "fields": "title",
                                }
                            }]})
                            print(f"  ✓ Tab marked complete: '{sheet.title} ✓'")
                        except Exception as e:
                            print(f"  ⚠ Could not rename tab: {e}")
                        msg = "Audit complete. Email sent."
                else:
                    # No UNMATCHED rows at all — send email directly
                    _, sheet      = _get_fresh_sheet(sheet.title)
                    all_values    = sheet.get_all_values()
                    status_col    = all_values[0].index("status")
                    matched_cnt   = sum(1 for r in all_values[1:]
                                        if len(r) > status_col
                                        and r[status_col].strip() == "MATCHED")
                    nf_cnt        = sum(1 for r in all_values[1:]
                                        if len(r) > status_col
                                        and r[status_col].strip() == "NEW_FOLDER")
                    total_cnt     = len(all_values) - 1
                    print(f"\n✅ All done — sending audit email...")
                    send_audit_email(sheet_url, matched_cnt, 0, total_cnt)
                    # Mark tab as complete — prevents re-sending email on subsequent runs
                    try:
                        sheet.spreadsheet.batch_update({"requests": [{
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet._properties["sheetId"],
                                    "title":   sheet.title + " ✓",
                                },
                                "fields": "title",
                            }
                        }]})
                        print(f"  ✓ Tab marked complete: '{sheet.title} ✓'")
                    except Exception as e:
                        print(f"  ⚠ Could not rename tab: {e}")
                    msg = "Audit complete. Email sent."

            else:
                # Normal run — fetch next 500 and process
                iteration_number = get_current_iteration_number(sheet)
                print(f"\n  Run iteration number: {iteration_number}")

                batch_df, sheet_row_map = fetch_unprocessed_batch(
                    sheet, BATCH_SIZE
                )

                if batch_df.empty:
                    print("  No unprocessed rows found.")
                    msg = "No unprocessed rows — nothing to do."
                else:
                    batch_df = process_batch(
                        batch_df, folders_df, folder_names,
                        sheet, sheet_row_map,
                        iteration_number=iteration_number,
                    )
                    # Count remaining for progress report
                    remaining = sum(
                        1 for r in sheet.get_all_values()[1:]
                        if len(r) < 12 or r[11].strip() != "1"
                    )
                    msg = (f"Batch complete (run {iteration_number}). "
                           f"~{remaining} rows still unprocessed.")
                    print(f"\n  {msg}")

            return (
                json.dumps({
                    "status":  "success",
                    "mode":    "audit",
                    "message": msg,
                    "tab":     tab_title,
                    "sheet":   sheet_url,
                }),
                200,
                {"Content-Type": "application/json"}
            )

    except Exception as e:
        err_str = str(e)
        print(f"❌ Error: {err_str}")
        # If SSL error — reset clients so next invocation gets fresh connections
        if "SSL" in err_str or "EOF" in err_str or "ssl" in err_str.lower():
            print("  SSL error detected — resetting API clients for next run")
            _reset_clients()
        return (
            json.dumps({"status": "error", "message": err_str}),
            500,
            {"Content-Type": "application/json"}
        )