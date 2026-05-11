# 🗂️ Google Drive Sorting Utility

**Automated AI-powered Google Drive cleanup for agencies and data teams.**

Scans your Drive for stray files, uses Claude AI to identify which client each file belongs to, presents suggestions in a Google Sheet for human approval, then moves everything to the right place — on a monthly schedule, automatically.

Built by [Marketlytics](https://marketlytics.com) and open-sourced for the broader analytics community.

---

## The Problem

If you run an agency, your Google Drive looks like this:

```
Analytics-ML/
├── Ferguson Roofing/          ← correct ✓
├── Trupanion/                 ← correct ✓
├── Ferguson Roofing - GTM Code Changes.gdoc   ← stray ✗
├── 230401_Marketing_Metrics.gsheet            ← stray ✗
├── Zepz - AppsFlyer Guide.gdoc               ← stray ✗
└── Copy of Report.gdoc                        ← stray ✗
```

Files accumulate in root folders instead of their client subfolders. Finding anything becomes impossible. Knowledge walks out the door when team members leave.

This utility fixes that automatically.

---

## How It Works

```
Every month on the 5th (Cloud Scheduler × every 45 min × 4 hours)
         │
         ▼
1. Scan Drive for stray files across configured scan targets
         │
         ▼
2. Fuzzy match filenames → client folders (free, no AI)
   ~30-40% matched instantly at score ≥85
         │
         ▼
3. Claude AI matches remaining files
   700 folders → filtered to ≤120 relevant per batch → Claude picks
   Validated back against real folder list before accepting
         │
         ▼
4. Unmatched files → suggestion pass
   Similar files clustered → one Claude call per cluster
   Suggests new folder names for files with no existing destination
         │
         ▼
5. Results written to Google Sheet
   Human reviews: Approve / Disapprove / Override in manual_folder
         │
         ▼
6. Move command executes approved rows
   NEW_FOLDER → folder auto-created → file moved
   Override → file moved to manual_folder destination
   Already moved → skipped (idempotent)
```

All state lives in the Google Sheet — if a run fails, the next one picks up exactly where it left off.

---

## Architecture

```
Cloud Scheduler (×2 cron jobs)
        │  HTTP GET every 45 min
        ▼
GCP Cloud Function (Python 3.11, Gen2, 540s timeout)
        │
        ├── Google Drive API v3  ── scan + move files
        ├── Google Sheets API    ── read/write audit tab
        └── Anthropic Claude API ── AI matching + suggestions
                                    (claude-haiku-4-5-20251001)
```

**Cost:** ~$0.40–0.50/month on Anthropic Claude Haiku. GCP Cloud Functions free tier covers the compute. Google Sheets API is free.

---

## Prerequisites

- **GCP project** with Cloud Functions and Cloud Scheduler enabled
- **Google Drive** with a consistent client folder structure
- **Google Sheet** (blank, you just need the ID from the URL)
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com)
- **Gmail account** with App Password enabled (for email reports)
- **Python 3.11+** for local testing

---

## Setup — Step by Step

### 1. Clone the repo

```bash
git clone https://github.com/marketlytics/gdrive-sorting-utility.git
cd gdrive-sorting-utility
```

### 2. Create a GCP Service Account

```bash
# Create service account
gcloud iam service-accounts create gdrive-clean-utility \
  --display-name="Drive Sorting Utility"

# Download key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=gdrive-clean-utility@YOUR-PROJECT.iam.gserviceaccount.com

# Base64-encode the key for .env.yaml
base64 -w 0 sa-key.json   # Linux/Mac
# OR on Windows:
certutil -encode sa-key.json sa-key-b64.txt
```

### 3. Share your Drive folders with the SA

The service account needs access to your Drive folders. In Google Drive:
- Right-click your root client folder (e.g. `Analytics-ML`)
- Share → paste the SA email → **Editor**
- Repeat for any other scan target folders

The SA email is in `sa-key.json` under `client_email`.

### 4. Share your Google Sheet with the SA

Open the sheet → Share → paste SA email → **Editor**.

### 5. Configure secrets

```bash
cp .env.yaml.example .env.yaml
```

Edit `.env.yaml` with your values. See `.env.yaml.example` for what each field means.

### 6. Configure your Drive structure

Edit `config.yaml`:

```yaml
agency:
  name: "Your Agency Name"

drive:
  scan_targets:
    - parent: "Your Root Folder"
      child: null
    - parent: "Your Root Folder"
      child: "Old Clients Subfolder"

  root_folder: "Your Root Folder"

  excluded_folders:
    - "Internal Projects"
    - "Templates"
    # add any folder that should never be a move destination
```

### 7. Install dependencies

```bash
pip install -r requirements.txt
```

### 8. Run locally first

```bash
# Audit mode — scans Drive and fills the sheet
py -u run_local.py

# Mover mode — moves approved rows
py -u run_local.py move
```

Check the sheet to verify results before deploying.

---

## Deploy to GCP

```bash
gcloud functions deploy gdrive_sorting_utility \
  --gen2 \
  --runtime=python311 \
  --region=us-central1 \
  --source=. \
  --entry-point=drive_manager \
  --trigger-http \
  --allow-unauthenticated \
  --timeout=540s \
  --memory=1024MB \
  --env-vars-file=.env.yaml
```

### Set up Cloud Scheduler

Two jobs are needed (cron can't express "every 45 minutes" in one expression):

```bash
# Job A — fires at :00 past each hour
gcloud scheduler jobs create http drive-audit-a \
  --schedule="0 13-16 5 * *" \
  --uri="https://YOUR-REGION-YOUR-PROJECT.cloudfunctions.net/gdrive_sorting_utility" \
  --http-method=GET \
  --time-zone="UTC" \
  --location=us-central1

# Job B — fires at :45 past each hour
gcloud scheduler jobs create http drive-audit-b \
  --schedule="45 13-16 5 * *" \
  --uri="https://YOUR-REGION-YOUR-PROJECT.cloudfunctions.net/gdrive_sorting_utility" \
  --http-method=GET \
  --time-zone="UTC" \
  --location=us-central1
```

Adjust the hour range (`13-16`) to match when you want the audit to run in UTC.

### Trigger manually

```bash
# Run audit
curl https://YOUR-FUNCTION-URL/gdrive_sorting_utility

# Run mover (after reviewing the sheet)
curl "https://YOUR-FUNCTION-URL/gdrive_sorting_utility?action=move"

# Target a specific audit tab
curl "https://YOUR-FUNCTION-URL/gdrive_sorting_utility?action=move&tab=2026-05-05"
```

---

## Google Sheet — Reviewer Guide

After each audit run, open the sheet and review the new tab (`Audit YYYY-MM-DD HH:MM`).

| Column | Purpose |
|---|---|
| `stray_file_name` | Original filename |
| `suggested_folder` | AI-suggested destination |
| `confidence` | high / medium / low |
| `reason` | Why this match was made |
| `status` | MATCHED / UNMATCHED / NEW_FOLDER |
| `action` | **Set this:** Approve or Disapprove |
| `manual_folder` | Override the suggestion — type an exact folder name OR paste a Drive folder ID from the URL |
| `notes` | System notes — "validation failed" = weak match, review carefully |
| `moved` | Written "yes" after successful move |

### Three outcomes

**MATCHED** — AI found an existing client folder. Review the reason column. If it makes sense → Approve.

**NEW_FOLDER** — File belongs to a client with no existing folder. Approve to auto-create the folder and move the file. Check that a similar folder doesn't already exist first.

**UNMATCHED** — No client signal in the filename (generic files, date-only names). Pre-filled with your archive folder. Approve to archive, Disapprove to leave in place, or type a folder name in `manual_folder` if you know where it belongs.

### manual_folder — two modes

**Folder name** (for direct subfolders):
```
Ferguson Roofing
```

**Folder ID** (for nested/deep folders — copy from Drive URL):
```
https://drive.google.com/drive/folders/1ABC123xyz...?
                                       └── paste this part
```

---

## File Naming Convention

For best results, name all new files:

```
ClientName - DocumentType - YYYY-MM.ext
```

Examples:
```
Ferguson Roofing - GTM Audit - 2026-05.gdoc
Trupanion - Measurement Plan - 2025-11.gdoc
Zepz - AppsFlyer Implementation - 2026-03.gdoc
```

With this convention, the fuzzy matcher alone handles ~80%+ of files with no AI cost.

---

## Environment Variables

| Variable | Description |
|---|---|
| `SA_KEY_JSON_B64` | Base64-encoded GCP service account JSON |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `EXISTING_SHEET_ID` | Google Sheets ID for audit tabs |
| `SHEET_NAME_PREFIX` | Tab name prefix (default: "Audit") |
| `SMTP_USER` | Gmail address for outbound reports |
| `SMTP_PASSWORD` | Gmail App Password |
| `REPORT_EMAIL` | Recipient for all report emails |
| `FUNCTION_URL` | Deployed Cloud Function URL |

---

## Cost Estimate

| Component | Cost |
|---|---|
| Claude Haiku (3,000 files/month) | ~$0.40–0.50 |
| GCP Cloud Functions | Free tier (540s × 32 runs) |
| Cloud Scheduler | Free tier (3 jobs) |
| Sheets API | Free |
| **Total** | **~$0.50/month** |

A $20 Anthropic credit lasts approximately 22 months of monthly audits.

---

## Local Development

```
gdrive-sorting-utility/
├── main.py              # All Cloud Function code
├── run_local.py         # Local test runner
├── config.yaml          # Your Drive structure config
├── requirements.txt     # Python dependencies
├── .env.yaml            # Secrets (never commit)
├── .env.yaml.example    # Secrets template
├── .gcloudignore        # Files excluded from GCP deploy
└── sa-key.json          # Service account key (never commit)
```

Run locally:
```bash
# Audit
py -u run_local.py

# Mover
py -u run_local.py move

# Mover targeting specific tab
py -u run_local.py move 2026-05-05
```

---

## How the AI Matching Works

### Folder pre-filtering (700 → ≤120)

Before each Claude call, the full folder list is reduced to the most relevant candidates:

- **Strategy A:** For each file in the batch, score all 700 folders using `token_set_ratio` on the full normalised filename. Keep top 50 per file.
- **Strategy B:** Add any folder sharing a 4+ character token with any file in the batch (safety net for Strategy A misses).
- **Cap at 120:** If the union exceeds 120, rank by relevance and trim.

Claude only ever sees ≤120 folders — never all 700. This dramatically reduces hallucination.

### Validation gate

Every folder name Claude returns is fuzzy-matched back against the real folder list. Anything scoring below 85 is rejected and the row stays UNMATCHED. Claude cannot hallucinate a destination.

### Client name extraction

The normaliser strips file extensions, .com/.io domain suffixes, date patterns, and business suffixes (Inc, LLC, Ltd) before scoring. `extract_best_token()` finds the client name even when it appears in the middle or end of a filename:

```
"Audit - Nov - Ferguson Roofing - GTM" → "Ferguson Roofing"
"Report - 2026-03 - Trupanion AU"      → "Trupanion AU"
```

---

## Contributing

PRs welcome. Open an issue first for anything beyond small fixes.

Particularly wanted:
- Support for SharePoint / OneDrive
- Multi-language filename normalisation
- Slack notification instead of email
- Web UI for the approval step

---

## Built With

- [Anthropic Claude](https://anthropic.com) — AI matching
- [Google Drive API v3](https://developers.google.com/drive) — file scanning and moving
- [gspread](https://github.com/burnash/gspread) — Google Sheets integration
- [RapidFuzz](https://github.com/maxbachmann/RapidFuzz) — fuzzy matching
- [GCP Cloud Functions](https://cloud.google.com/functions) — serverless execution

---

## License

MIT — see [LICENSE](LICENSE)

---

Built by [Marketlytics](https://marketlytics.com) · Pakistan's leading data analytics agency