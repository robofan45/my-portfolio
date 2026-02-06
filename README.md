# ResumeFlow

ResumeFlow is a lightweight desktop app that helps you recover context after task switching.

## Features
- Active window focus monitoring:
  - Windows: `pygetwindow` + `psutil`
  - macOS: `AppKit` + `psutil`
- Resume popup when you return to a prior window after an away threshold.
- "Ready to Resume" micro-task input before dismissing the popup.
- Daily context switch tracking and score in the system tray.
- Local SQLite logging for weekly reporting.
- Adjustable settings for away threshold, popup position, and quiet hours.

## Run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python resumeflow.py
```

## Data Storage
- Settings: `~/.resumeflow/settings.json`
- Events DB: `~/.resumeflow/resumeflow.db`

## Notes
- No cloud services are used; all data stays local.
- The app is designed to be lightweight and polls once per second.
