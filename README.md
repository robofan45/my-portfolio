# ResumeFlow

ResumeFlow is a lightweight, local-first desktop assistant that helps you recover context after task switching.

## Features
- Active window monitoring:
  - Windows: `pygetwindow` + `psutil`
  - macOS: `AppKit` + `psutil`
  - Linux: best-effort fallback via `xprop`
- Resume popup when returning to a previously active window after an away threshold.
- "Ready to Resume" single-line micro-task field before dismissing the popup.
- Daily **Context Switch Score** and real-time switch rate in the system tray.
- Local SQLite logging for weekly reports.
- Settings for away threshold (30s–5m), popup position, and quiet hours.

## Run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python resumeflow.py
```

## Data Storage
- Settings: `~/.resumeflow/settings.json`
- Database: `~/.resumeflow/resumeflow.db`

## Privacy
- No cloud calls.
- All data is stored locally.
