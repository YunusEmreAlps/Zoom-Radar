# Zoom Meeting Participant Report Generator

Two tools for Zoom attendance tracking:

| Tool | Needs | Use it when |
|------|-------|-------------|
| **`zoom_attendance.py`** | Nothing but the Zoom desktop app on Windows | You join someone else's meeting (any account) and want to catch people who leave after roll call |
| `zoom_activity.py` | A Server-to-Server OAuth app **in the host's Zoom account** | You (or your admin) own the account that hosts the meetings |

## Attendance Tracking Without Credentials (`zoom_attendance.py`)

`zoom_attendance.py` reads the **Participants** panel of your own Zoom desktop client through Windows UI Automation (the accessibility API screen readers use). It needs no Zoom API, no `env.yaml` and no host rights. Anything you can see in the panel, it can see.

```bash
pip install uiautomation
```

### During the meeting

1. Join the meeting and open the **Participants** panel (Alt+U). For large meetings, **Pop out** the panel and make it tall.
2. Run:

   ```bash
   python zoom_attendance.py check     # optional: check the panel can be read (names are masked)
   python zoom_attendance.py live      # start watching
   python zoom_attendance.py live --roll-call 14:45 --roll-call 15:30   # also take roll calls automatically
   ```

3. Press **Enter** (or **r**) whenever you take roll call, and **q** when the meeting ends.

```text
14:30:05  ▶️  Watching started · 24 people (excluding you)
14:31:10  🟢 Mehmet Demir joined (25)
14:45:00  📋 Roll call taken · 25 people present
14:47:12  ⚠️  Ahmet Yılmaz left — 2 min after roll call (24)
14:52:40  🔴 Ayşe Kaya left (23)
15:05:02  🔁 Ayşe Kaya came back (24)
```

Every event is written immediately to `logs/attendance_<date>_<time>.csv` (it survives a crash). When you quit, the summary is printed and saved next to it as `..._summary.csv`:

```text
Meeting: 14:30–15:20 (50 min) · Roll call: 14:45 · 25 people

⚠️ Left after roll call (1)
   Ahmet Yılmaz   17 min (34%)  left 14:47, did not return (2 min after roll call)

🔁 Left and came back (1)
   Ayşe Kaya      37 min (74%)  away 14:52–15:05 (13 min)

✅ Attended (23)
   ...
```

### After the meeting

`report` re-analyses a saved log with different rules, or a participant report the host exported from zoom.us (Reports → Usage Reports → participants → Export, **without** "Show unique users"):

```bash
python zoom_attendance.py report logs/attendance_20261023_143005.csv --tolerance 5
python zoom_attendance.py report participants_94589871454.csv --roll-call 14:45 --save
```

### Rules

| Option | Default | Meaning |
|--------|---------|---------|
| `--roll-call HH:MM` | – | Roll call time(s); people absent at every roll call are marked ❌ |
| `--tolerance MIN` | 10 | Arriving in the first / leaving in the last N minutes is fine |
| `--min-gap MIN` | 2 | Shorter absences count as reconnects |
| `--min-percent PCT` | 80 | Below this share of the meeting is "low participation" |
| `--details` | – | Also list everyone's join–leave intervals |

`live` also takes `--interval SEC` (default 5) and `--log-dir DIR` (default `logs`); `report` takes `--save`; `check` takes `--unmasked` and `--tree`.

Verdicts, most serious first: ❌ absent at roll call · ⚠️ left after roll call / left early · 🔁 left and came back · ⏰ late · 📉 low participation · ✅ attended.

### How reliable is it?

- **Timing:** the panel is read every 5 s (`--interval`). A person must be missing from 2 reads in a row before a leave is recorded, so one-off UI glitches are ignored. The leave time is the first read they were missing.
- **Panel closed or minimized:** watching pauses (⛔). Nobody is marked as left, and the gap is shown in the summary.
- **Count mismatch:** if the panel title count and the names read differ, a warning tells you to pop out and enlarge the panel.
- **Your own entry** ("me") is left out. Two people with the same display name are tracked by count. E-mails are not visible in the panel.
- **Zoom version:** verified on Zoom Workplace 7.1.9. Everything that depends on Zoom's UI lives in `zoom_ui.py`.

⚠️ The logs contain participants' names (personal data). `logs/` and exported reports are in `.gitignore`; keep them according to your company's data policy.

## Zoom API Report (`zoom_activity.py`)

Retrieves participant reports for all instances of a meeting through the Zoom REST API. It only sees meetings hosted in the Zoom account where the Server-to-Server app was created.

### Features

This script retrieves comprehensive participant activity data including:

- Join/Leave events
- Camera on/off events  
- Microphone mute/unmute events
- Screen sharing events
- Recording events
- And more detailed activity logs

### Requirements

- Python 3.7+
- Zoom account with Pro plan or higher
- Zoom OAuth Server-to-Server app

```bash
pip install requests pyyaml
```

### Setup Instructions

#### 1. Create a Server-to-Server OAuth App in Zoom

1. Sign in to [Zoom App Marketplace](https://marketplace.zoom.us/)
2. Click **"Develop"** → **"Build App"**
3. Choose **"Server-to-Server OAuth"**
4. Fill in the app information
5. Note down your:
   - **Account ID**
   - **Client ID**
   - **Client Secret**
6. Add the following scopes:
   - `meeting:read:admin`
   - `report:read:admin`
   - `user:read:admin`

#### 2. Configure Credentials

Create an `env.yaml` file in the project directory with your credentials:

```yaml
# env.yaml
zoom:
  account_id: "your_account_id_here"
  client_id: "your_client_id_here"
  client_secret: "your_client_secret_here"
```

**⚠️ IMPORTANT:** Never commit `env.yaml` to version control! It's already added to `.gitignore`.

Alternatively, pass them on the command line (these override `env.yaml`):

```bash
python zoom_activity.py <meeting_id> --account-id <id> --client-id <id> --client-secret <secret>
```

If any credential is missing (or still `xxxxx`), the script stops and lists which ones are missing.

#### 3. Run the Script

```bash
# Basic usage
python zoom_activity.py <meeting_id>

# Show what happens behind the scenes (token, every request, pagination, parsing)
python zoom_activity.py <meeting_id> --debug

# Save to CSV file
python zoom_activity.py <meeting_id> --save

# Use custom config file
python zoom_activity.py <meeting_id> --config my-config.yaml

# Example
python zoom_activity.py 94589871454 --debug
```

### Sample Output

```bash
Found 2 meeting instance(s)

================================================================================
Meeting Instance 1: 10/15/2025, 02:30:15 PM
Meeting UUID: abc123xyz
================================================================================

📊 John Doe:
   Time         Activity             Details
   ------------------------------------------------------------
   02:32:10 PM  🟢 Join              Duration: 45 minutes
   02:35:00 PM  📹 Camera On         
   02:40:15 PM  🎤 Mic Unmute        
   03:10:30 PM  🔇 Mic Mute          
   03:17:22 PM  🔴 Leave             
   Total meeting time: 45.2 minutes

📊 Jane Smith:
   Time         Activity             Details
   ------------------------------------------------------------
   02:35:05 PM  🟢 Join              Duration: 43 minutes
   02:36:00 PM  📹 Camera On         
   03:17:55 PM  🔴 Leave             
   Total meeting time: 42.8 minutes
```

### Background Log (`--debug`)

With `--debug` the script logs what happens behind the scenes: where each credential came from, the token request, every API call (URL, parameters, worker thread, result count, duration), pagination, retries and parsing. The log goes to stderr, so the report itself (stdout) stays clean. Secrets are never printed.

```bash
14:08:52.522 [MainThread] → POST https://zoom.us/oauth/token (grant_type=account_credentials, account_id=abcd…)
14:08:52.522 [MainThread] → GET https://api.zoom.us/v2/past_meetings/94589871454/instances
14:08:52.823 [MainThread] ← /past_meetings/94589871454/instances: meetings=2 (300 ms)
14:08:52.824 [worker_0] → GET https://api.zoom.us/v2/report/meetings/abc123%3D%3D/participants {'page_size': 300}
14:08:53.127 [worker_0] ← /report/meetings/abc123%3D%3D/participants: participants=2 (303 ms)
14:08:53.434 [MainThread] Parse: 2 katılımcı + 7 event → 8 aktivite (1 tekrar birleştirildi, 0 event atlandı)
```

## Troubleshooting

| Error                                           | Solution                                                           |
|-------------------------------------------------|--------------------------------------------------------------------|
| `Panel not found` / `Cannot read the Participants panel` | Open the Participants panel (Alt+U) in the meeting window |
| `Panel shows N people but only M names could be read`  | Pop out the Participants panel and make it taller         |
| `live watching needs uiautomation`                     | `pip install uiautomation` (Windows only)                 |
| `Eksik kimlik bilgisi: ...`                     | Fill in the listed credentials in `env.yaml` or pass them as flags |
| `Import "yaml" could not be resolved`           | Install PyYAML: `pip install pyyaml`                               |
| `Token alınamadı`                               | Verify your credentials are correct and app has required scopes    |
| `Toplantı bulunamadı` + `devam ediyor`          | Reports exist only for ended meetings; rerun after it ends         |
| `Toplantı bulunamadı` + `bu Zoom hesabında yok` | Check the ID; meetings hosted by other accounts are not accessible |
| `Toplantı bulunamadı` + `bitmiş bir oturumu yok`| Not held yet, just ended (wait a few minutes), or host-only        |

## File Structure

```
Zoom User Activity/
├── zoom_attendance.py        # Attendance CLI: live / report / check
├── attendance.py             # Attendance analysis (Zoom-independent)
├── zoom_ui.py                # Reads Zoom's Participants panel (UI Automation)
├── test_zoom_attendance.py   # Attendance unit tests (offline)
├── zoom_activity.py          # Zoom API report script
├── test_zoom_activity.py     # API script unit tests
├── logs/                     # Attendance logs (DO NOT COMMIT – personal data)
├── env.yaml                  # API credentials (DO NOT COMMIT!)
├── .gitignore                # Protects sensitive files
└── README.md                 # This file
```
