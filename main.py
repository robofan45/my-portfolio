#!/usr/bin/env python3
"""ResumeFlow - local full-stack desktop context switch tracker.

Install dependencies:
    pip install pyqt6 pywinctl psutil
"""

from __future__ import annotations

import csv
import importlib
import json
import logging
import sqlite3
import sys
import threading
import traceback
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

try:
    from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtGui import (
        QAction,
        QColor,
        QCursor,
        QDesktopServices,
        QFont,
        QIcon,
        QPainter,
        QPen,
        QPixmap,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QTimeEdit,
        QVBoxLayout,
    )
    QT_AVAILABLE = True
    QT_IMPORT_ERROR: Exception | None = None
except Exception as qt_exc:
    QT_AVAILABLE = False
    QT_IMPORT_ERROR = qt_exc

    class QObject: pass
    class QDialog: pass
    class QFrame: pass
    class QApplication: pass
    class QSystemTrayIcon: pass
    class QPoint:
        def __init__(self, x=0, y=0): self._x=x; self._y=y
        def x(self): return self._x
        def y(self): return self._y
    class QUrl:
        def __init__(self, *_): pass
        @staticmethod
        def fromLocalFile(_): return QUrl()
    class QTimer:
        def __init__(self,*_): pass
        def start(self,*_): pass
        def stop(self,*_): pass
        @property
        def timeout(self):
            class _T:
                def connect(self,*_): pass
            return _T()
    class Qt:
        class WindowType:
            Tool=0; FramelessWindowHint=0; WindowStaysOnTopHint=0
        class WidgetAttribute:
            WA_TranslucentBackground=0
        class FocusPolicy:
            StrongFocus=0
        class FocusReason:
            PopupFocusReason=0
        class AlignmentFlag:
            AlignRight=0; AlignCenter=0
        class PenStyle:
            NoPen=0
        class GlobalColor:
            transparent=0
        class TextFormat:
            RichText=0
        class ItemFlag:
            ItemIsEditable=0
    def pyqtSignal(*_args, **_kwargs):
        class _S:
            def connect(self,*_): pass
            def emit(self,*_): pass
        return _S()

try:
    import psutil
except Exception:  # optional dependency
    psutil = None


APP_NAME = "ResumeFlow"
APP_DIR = Path.home() / ".resumeflow"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "resumeflow.db"
LOG_PATH = APP_DIR / "resumeflow.log"

MIN_THRESHOLD = 30
MAX_THRESHOLD = 300
NOISE_SWITCH_SECONDS = 2.0
WATCHDOG_INTERVAL = 0.5
WATCHDOG_REFRESH_MS = 5000
ACTIVE_FALLBACK_POLL_MS = 1200
MAX_POPUP_AWAY_SECONDS = 8 * 60 * 60
OFFSET_MIN = -500
OFFSET_MAX = 500
WEB_HOST = "127.0.0.1"
WEB_PORT = 8765

LOGGER = logging.getLogger(APP_NAME)


def load_pywinctl() -> Any:
    try:
        return importlib.import_module("pywinctl")
    except Exception as exc:
        raise RuntimeError(
            "Unable to load pywinctl. Ensure desktop session/window permissions are enabled and "
            "dependency is installed: pip install pywinctl"
        ) from exc


@dataclass
class AppSettings:
    away_threshold_sec: int = 30
    popup_offset_x: int = 18
    popup_offset_y: int = 20
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    popup_auto_dismiss_on_focus_loss: bool = False
    pause_tracking: bool = False


def parse_hhmm(value: str, fallback: str) -> dtime:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except Exception:
        return datetime.strptime(fallback, "%H:%M").time()


def is_within_quiet_hours(current: dtime, start: dtime, end: dtime) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


class DatabaseManager:
    def __init__(self, db_path: Path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._migrate()

    def _configure_connection(self) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.execute("PRAGMA synchronous = NORMAL")
                self.conn.execute("PRAGMA temp_store = MEMORY")
                self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS switches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        previous_window_title TEXT,
                        new_window_title TEXT,
                        micro_task TEXT
                    )
                    """
                )
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_switches_timestamp ON switches(timestamp)")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def load_settings(self) -> AppSettings:
        base = AppSettings()
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        kv = {r["key"]: r["value"] for r in rows}

        base.away_threshold_sec = max(
            MIN_THRESHOLD,
            min(MAX_THRESHOLD, self._safe_int(kv.get("away_threshold_sec"), base.away_threshold_sec)),
        )
        base.popup_offset_x = max(OFFSET_MIN, min(OFFSET_MAX, self._safe_int(kv.get("popup_offset_x"), base.popup_offset_x)))
        base.popup_offset_y = max(OFFSET_MIN, min(OFFSET_MAX, self._safe_int(kv.get("popup_offset_y"), base.popup_offset_y)))
        base.quiet_start = kv.get("quiet_start", base.quiet_start)
        base.quiet_end = kv.get("quiet_end", base.quiet_end)
        base.popup_auto_dismiss_on_focus_loss = kv.get("popup_auto_dismiss_on_focus_loss", "0") == "1"
        base.pause_tracking = kv.get("pause_tracking", "0") == "1"
        return base

    def save_settings(self, settings: AppSettings) -> None:
        values = {
            "away_threshold_sec": str(settings.away_threshold_sec),
            "popup_offset_x": str(max(OFFSET_MIN, min(OFFSET_MAX, settings.popup_offset_x))),
            "popup_offset_y": str(max(OFFSET_MIN, min(OFFSET_MAX, settings.popup_offset_y))),
            "quiet_start": settings.quiet_start,
            "quiet_end": settings.quiet_end,
            "popup_auto_dismiss_on_focus_loss": "1" if settings.popup_auto_dismiss_on_focus_loss else "0",
            "pause_tracking": "1" if settings.pause_tracking else "0",
        }
        with self._lock:
            with self.conn:
                for k, v in values.items():
                    self.conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (k, v),
                    )

    def insert_switch(self, timestamp: datetime, previous_title: str, new_title: str) -> int:
        with self._lock:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO switches(timestamp, previous_window_title, new_window_title, micro_task)
                    VALUES(?, ?, ?, NULL)
                    """,
                    (timestamp.isoformat(), previous_title, new_title),
                )
                return int(cur.lastrowid)

    def update_micro_task(self, switch_id: int, task_text: str) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute("UPDATE switches SET micro_task = ? WHERE id = ?", (task_text.strip() or None, switch_id))

    def get_hour_count(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start.replace(minute=59, second=59, microsecond=999999)
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM switches WHERE timestamp BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return int(row["c"]) if row else 0

    def get_day_count(self, day: datetime | None = None) -> int:
        day = day or datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM switches WHERE timestamp BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return int(row["c"]) if row else 0

    def get_today_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        day = datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, timestamp, previous_window_title, new_window_title, micro_task
                FROM switches
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return rows

    def reset_switches(self) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute("DELETE FROM switches")
            self.conn.execute("VACUUM")

    def get_dashboard_payload(self, limit: int = 100) -> dict[str, Any]:
        rows = self.get_today_rows(limit=limit)
        return {
            "generated_at": datetime.now().isoformat(),
            "today_count": self.get_day_count(),
            "hour_count": self.get_hour_count(),
            "rows": [
                {
                    "id": int(r["id"]),
                    "timestamp": r["timestamp"],
                    "previous_window_title": r["previous_window_title"],
                    "new_window_title": r["new_window_title"],
                    "micro_task": r["micro_task"],
                }
                for r in rows
            ],
        }

    @staticmethod
    def _safe_int(v: str | None, fallback: int) -> int:
        try:
            return int(v) if v is not None else fallback
        except ValueError:
            return fallback


class WebDashboardServer:
    def __init__(self, db: DatabaseManager, host: str = WEB_HOST, port: int = WEB_PORT):
        self.db = db
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        app_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path == "/" or path == "/dashboard":
                    body = app_ref._dashboard_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/api/health":
                    app_ref._send_json(self, {"ok": True, "service": APP_NAME, "time": datetime.now().isoformat()})
                    return

                if path == "/api/stats":
                    app_ref._send_json(
                        self,
                        {
                            "today_count": app_ref.db.get_day_count(),
                            "hour_count": app_ref.db.get_hour_count(),
                            "time": datetime.now().isoformat(),
                        },
                    )
                    return

                if path == "/api/switches":
                    limit = 100
                    try:
                        if "limit" in qs:
                            limit = max(1, min(1000, int(qs["limit"][0])))
                    except Exception:
                        limit = 100
                    app_ref._send_json(self, app_ref.db.get_dashboard_payload(limit=limit))
                    return

                app_ref._send_json(self, {"error": "Not found"}, status=404)

            def log_message(self, fmt: str, *args) -> None:
                LOGGER.debug("WEB %s", fmt % args)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("Web dashboard running at %s", self.base_url)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @staticmethod
    def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _dashboard_html() -> str:
        return """<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>ResumeFlow Dashboard</title>
<style>
body { font-family: Inter, Segoe UI, Arial, sans-serif; margin: 24px; background:#0f172a; color:#e2e8f0; }
.card { background:#111827; border:1px solid #334155; border-radius:12px; padding:16px; margin-bottom:16px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
.kpi { background:#1e293b; border-radius:10px; padding:12px; }
.kpi h3 { margin:0 0 4px 0; font-size:13px; color:#94a3b8; }
.kpi p { margin:0; font-size:24px; font-weight:700; }
button { background:#2563eb; color:white; border:none; border-radius:8px; padding:8px 12px; cursor:pointer; }
button:hover { background:#3b82f6; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:8px; border-bottom:1px solid #334155; font-size:13px; }
th { color:#93c5fd; }
.small { color:#94a3b8; font-size:12px; }
</style>
</head>
<body>
<h1>ResumeFlow Dashboard</h1>
<div class='card grid'>
  <div class='kpi'><h3>Switches this hour</h3><p id='hour'>-</p></div>
  <div class='kpi'><h3>Switches today</h3><p id='day'>-</p></div>
  <div class='kpi'><h3>Last refresh</h3><p id='refresh' class='small'>-</p></div>
</div>
<div class='card'>
  <button onclick='loadData()'>Refresh</button>
  <span class='small' style='margin-left:8px'>Data updates every 15s.</span>
</div>
<div class='card'>
  <table>
    <thead><tr><th>Time</th><th>From</th><th>To</th><th>Micro-task</th></tr></thead>
    <tbody id='rows'></tbody>
  </table>
</div>
<script>
async function loadData() {
  const data = await fetch('/api/switches?limit=150').then(r=>r.json());
  document.getElementById('hour').textContent = data.hour_count;
  document.getElementById('day').textContent = data.today_count;
  document.getElementById('refresh').textContent = new Date().toLocaleTimeString();
  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';
  for (const row of data.rows) {
    const tr = document.createElement('tr');
    const ts = (row.timestamp || '').replace('T',' ').slice(0,19);
    tr.innerHTML = `<td>${ts}</td><td>${row.previous_window_title || ''}</td><td>${row.new_window_title || ''}</td><td>${row.micro_task || ''}</td>`;
    tbody.appendChild(tr);
  }
}
loadData();
setInterval(loadData, 15000);
</script>
</body>
</html>"""


class ResumePopup(QFrame):
    submitted = pyqtSignal(int, str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._auto_dismiss = False
        self._switch_id: int | None = None

        card = QFrame(self)
        card.setObjectName("PopupCard")
        card.setStyleSheet(
            """
            #PopupCard {
                background: rgba(22, 26, 30, 240);
                border: 1px solid rgba(255,255,255,38);
                border-radius: 12px;
            }
            QLabel { color: #f5f7fa; }
            QLineEdit {
                color: #f8f9fa;
                background: rgba(255,255,255,22);
                border: 1px solid rgba(255,255,255,36);
                border-radius: 8px;
                padding: 6px 8px;
            }
            QPushButton {
                background: #3b82f6;
                border-radius: 8px;
                color: white;
                padding: 6px 14px;
            }
            QPushButton:hover { background: #60a5fa; }
            """
        )

        self.info_label = QLabel()
        self.work_label = QLabel()
        self.info_label.setWordWrap(True)
        self.work_label.setWordWrap(True)

        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText("Next micro-task (optional)")
        self.task_input.returnPressed.connect(self._submit)

        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._submit)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)
        card_layout.addWidget(self.info_label)
        card_layout.addWidget(self.work_label)
        card_layout.addWidget(QLabel("Next micro-task (optional):"))
        card_layout.addWidget(self.task_input)
        card_layout.addWidget(ok_btn, alignment=Qt.AlignmentFlag.AlignRight)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)

    def configure(self, auto_dismiss: bool) -> None:
        self._auto_dismiss = auto_dismiss

    def show_popup(self, switch_id: int, away_seconds: float, work_title: str, pos: QPoint) -> None:
        self._switch_id = switch_id
        self.info_label.setText(f"You were last here {self._format_away(away_seconds)} ago.")
        self.work_label.setText(f"You were working on: {work_title}")
        self.task_input.clear()
        self.adjustSize()
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()
        self.task_input.setFocus(Qt.FocusReason.PopupFocusReason)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        if self._auto_dismiss:
            self.hide()
        super().focusOutEvent(event)

    def _submit(self) -> None:
        if self._switch_id is not None:
            self.submitted.emit(self._switch_id, self.task_input.text())
        self.hide()

    @staticmethod
    def _format_away(seconds: float) -> str:
        s = int(max(0, seconds))
        if s >= 60:
            mins = s // 60
            rem = s % 60
            return f"{mins}m {rem}s" if rem else f"{mins}m"
        return f"{s}s"


class SettingsDialog(QDialog):
    settings_saved = pyqtSignal(AppSettings)
    reset_requested = pyqtSignal()

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.setWindowTitle("ResumeFlow Settings")
        self.setMinimumWidth(430)

        self.threshold = QSpinBox()
        self.threshold.setRange(MIN_THRESHOLD, MAX_THRESHOLD)
        self.threshold.setSuffix(" sec")
        self.threshold.setValue(settings.away_threshold_sec)

        self.offset_x = QSpinBox()
        self.offset_x.setRange(OFFSET_MIN, OFFSET_MAX)
        self.offset_x.setValue(settings.popup_offset_x)

        self.offset_y = QSpinBox()
        self.offset_y.setRange(OFFSET_MIN, OFFSET_MAX)
        self.offset_y.setValue(settings.popup_offset_y)

        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_start.setTime(parse_hhmm(settings.quiet_start, "22:00"))

        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_end.setTime(parse_hhmm(settings.quiet_end, "07:00"))

        self.auto_dismiss = QCheckBox("Auto-dismiss popup on focus loss")
        self.auto_dismiss.setChecked(settings.popup_auto_dismiss_on_focus_loss)

        form = QFormLayout()
        form.addRow("Away threshold:", self.threshold)
        form.addRow("Popup offset X:", self.offset_x)
        form.addRow("Popup offset Y:", self.offset_y)
        form.addRow("Quiet hours start:", self.quiet_start)
        form.addRow("Quiet hours end:", self.quiet_end)
        form.addRow("", self.auto_dismiss)

        reset_btn = QPushButton("Reset Database")
        reset_btn.clicked.connect(self.reset_requested.emit)

        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(self._save)
        controls.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(reset_btn)
        layout.addWidget(controls)

    def _save(self) -> None:
        self.settings_saved.emit(
            AppSettings(
                away_threshold_sec=self.threshold.value(),
                popup_offset_x=self.offset_x.value(),
                popup_offset_y=self.offset_y.value(),
                quiet_start=self.quiet_start.time().toString("HH:mm"),
                quiet_end=self.quiet_end.time().toString("HH:mm"),
                popup_auto_dismiss_on_focus_loss=self.auto_dismiss.isChecked(),
            )
        )
        self.accept()


class TodayReportDialog(QDialog):
    def __init__(self, rows: list[sqlite3.Row], total: int, on_export: Callable[[], None]):
        super().__init__()
        self.setWindowTitle("Today's Report")
        self.setMinimumSize(960, 500)

        heading = QLabel(f"Context Switch Score today: <b>{total}</b>")
        heading.setTextFormat(Qt.TextFormat.RichText)

        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(["Time", "From", "To", "Micro-task"])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)

        for i, row in enumerate(rows):
            stamp = row["timestamp"].replace("T", " ")[:19]
            values = [stamp, row["previous_window_title"] or "", row["new_window_title"] or "", row["micro_task"] or ""]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(i, c, item)

        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)

        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(on_export)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)

        foot = QHBoxLayout()
        foot.addWidget(export_btn)
        foot.addStretch()
        foot.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(table)
        layout.addLayout(foot)


class WindowTracker(QObject):
    switched = pyqtSignal(dict)

    def __init__(self, pywinctl_module: Any) -> None:
        super().__init__()
        self._pywinctl = pywinctl_module
        self._watchdogs: dict[str, Any] = {}
        self._windows: dict[str, Any] = {}
        self._active_key: str | None = None

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_watchdogs)

        self._fallback_timer = QTimer(self)
        self._fallback_timer.timeout.connect(self._poll_active_fallback)

    def start(self) -> None:
        self.refresh_watchdogs()
        self._refresh_timer.start(WATCHDOG_REFRESH_MS)
        self._fallback_timer.start(ACTIVE_FALLBACK_POLL_MS)

        active = self._safe_active_window()
        if active is not None:
            self._active_key = self._window_key(active)

    def stop(self) -> None:
        self._refresh_timer.stop()
        self._fallback_timer.stop()
        for key in list(self._watchdogs.keys()):
            window = self._windows.get(key)
            if window is not None:
                try:
                    window.watchdog.stop()
                except Exception:
                    LOGGER.debug("Failed stopping watchdog", exc_info=True)
        self._watchdogs.clear()
        self._windows.clear()

    def refresh_watchdogs(self) -> None:
        try:
            windows = self._pywinctl.getAllWindows()
        except Exception:
            LOGGER.debug("getAllWindows failed", exc_info=True)
            return

        seen: set[str] = set()
        for window in windows:
            key = self._window_key(window)
            seen.add(key)
            self._windows[key] = window
            if key in self._watchdogs:
                continue

            def _active_cb(is_active: bool, k: str = key) -> None:
                if is_active:
                    self._on_activated(k)

            try:
                window.watchdog.start(isActiveCB=_active_cb, interval=WATCHDOG_INTERVAL)
                self._watchdogs[key] = window.watchdog
            except Exception:
                LOGGER.debug("Failed creating watchdog for window", exc_info=True)

        stale = set(self._watchdogs.keys()) - seen
        for key in stale:
            window = self._windows.get(key)
            if window is not None:
                try:
                    window.watchdog.stop()
                except Exception:
                    LOGGER.debug("Failed stopping stale watchdog", exc_info=True)
            self._watchdogs.pop(key, None)
            self._windows.pop(key, None)

    def _poll_active_fallback(self) -> None:
        active = self._safe_active_window()
        if active is None:
            return
        key = self._window_key(active)
        if key != self._active_key:
            self._windows[key] = active
            self._on_activated(key)

    def _on_activated(self, key: str) -> None:
        now = datetime.now()
        prev_key = self._active_key
        if key == prev_key:
            return
        self._active_key = key

        prev_win = self._windows.get(prev_key) if prev_key else None
        new_win = self._windows.get(key)

        self.switched.emit(
            {
                "timestamp": now,
                "previous_key": prev_key,
                "new_key": key,
                "previous_title": self._window_label(prev_win),
                "new_title": self._window_label(new_win),
            }
        )

    @staticmethod
    def _window_key(window: Any) -> str:
        try:
            return str(window.getHandle())
        except Exception:
            return str(id(window))

    @staticmethod
    def _window_label(window: Any) -> str:
        if window is None:
            return "Unknown"

        try:
            title = (window.title or "").strip()
            if title:
                return title
        except Exception:
            pass

        try:
            app_name = (window.getAppName() or "").strip()
            if app_name:
                return app_name
        except Exception:
            pass

        if psutil is not None:
            try:
                pid = window.getPID()
                if pid:
                    return psutil.Process(pid).name()
            except Exception:
                pass

        return "Unknown Window"

    def _safe_active_window(self) -> Any | None:
        try:
            return self._pywinctl.getActiveWindow()
        except Exception:
            return None


class HeadlessTracker:
    """Non-GUI tracker so app can run in server mode without Qt/OpenGL."""

    def __init__(self, db: DatabaseManager, pywinctl_module: Any):
        self.db = db
        self._pywinctl = pywinctl_module
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_key: str | None = None
        self._active_title: str = "Unknown"
        self._last_switch_time: datetime | None = None
        self._last_switch_pair: tuple[str, str] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                win = self._pywinctl.getActiveWindow()
                if win is None:
                    time.sleep(1.0)
                    continue
                key = self._window_key(win)
                title = self._window_label(win)
                if self._active_key is None:
                    self._active_key = key
                    self._active_title = title
                elif key != self._active_key:
                    now = datetime.now()
                    if not self._is_noise_switch(now, self._active_key, key):
                        self.db.insert_switch(now, self._active_title, title)
                        self._last_switch_time = now
                        self._last_switch_pair = (self._active_key, key)
                    self._active_key = key
                    self._active_title = title
            except Exception:
                LOGGER.debug("Headless tracker poll failed", exc_info=True)
            time.sleep(1.0)

    def _is_noise_switch(self, now: datetime, prev_key: str, new_key: str) -> bool:
        if self._last_switch_time is None or self._last_switch_pair is None:
            return False
        if (now - self._last_switch_time).total_seconds() > NOISE_SWITCH_SECONDS:
            return False
        return self._last_switch_pair == (new_key, prev_key)

    @staticmethod
    def _window_key(window: Any) -> str:
        try:
            return str(window.getHandle())
        except Exception:
            return str(id(window))

    @staticmethod
    def _window_label(window: Any) -> str:
        if window is None:
            return "Unknown"
        try:
            t = (window.title or "").strip()
            if t:
                return t
        except Exception:
            pass
        try:
            n = (window.getAppName() or "").strip()
            if n:
                return n
        except Exception:
            pass
        return "Unknown Window"


class TrayController(QObject):
    def __init__(self, db: DatabaseManager, tracker: WindowTracker, web_server: WebDashboardServer):
        super().__init__()
        self.db = db
        self.tracker = tracker
        self.web_server = web_server
        self.settings = self.db.load_settings()

        self.popup = ResumePopup()
        self.popup.configure(self.settings.popup_auto_dismiss_on_focus_loss)
        self.popup.submitted.connect(self._on_popup_submit)

        self.last_left_at: dict[str, datetime] = {}
        self.last_switch_time: datetime | None = None
        self.last_switch_pair: tuple[str, str] | None = None

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._build_count_icon(0))

        self.menu = QMenu()
        self.action_pause = QAction("Pause Tracking", self.menu)
        self.action_pause.setCheckable(True)
        self.action_pause.setChecked(self.settings.pause_tracking)
        self.action_pause.triggered.connect(self.toggle_pause)

        self.action_settings = QAction("Open Settings", self.menu)
        self.action_settings.triggered.connect(self.open_settings)

        self.action_report = QAction("View Today's Report", self.menu)
        self.action_report.triggered.connect(self.open_report)

        self.action_dashboard = QAction("Open Web Dashboard", self.menu)
        self.action_dashboard.triggered.connect(self.open_web_dashboard)

        self.action_open_data = QAction("Open Data Folder", self.menu)
        self.action_open_data.triggered.connect(self.open_data_folder)

        self.action_open_logs = QAction("Open Log File", self.menu)
        self.action_open_logs.triggered.connect(self.open_log_file)

        self.action_score = QAction("Context Switch Score: 0", self.menu)
        self.action_score.setEnabled(False)

        self.action_quit = QAction("Quit", self.menu)
        self.action_quit.triggered.connect(self.quit)

        self.menu.addAction(self.action_pause)
        self.menu.addAction(self.action_settings)
        self.menu.addAction(self.action_report)
        self.menu.addAction(self.action_dashboard)
        self.menu.addAction(self.action_open_data)
        self.menu.addAction(self.action_open_logs)
        self.menu.addAction(self.action_score)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self.tracker.switched.connect(self._on_switch)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(30_000)
        self.update_status()

    def toggle_pause(self, checked: bool) -> None:
        self.settings.pause_tracking = checked
        self.db.save_settings(self.settings)
        self.update_status()
        self.tray.showMessage(APP_NAME, "Tracking paused" if checked else "Tracking resumed", QSystemTrayIcon.MessageIcon.Information, 1500)

    def _on_switch(self, event: dict[str, Any]) -> None:
        if self.settings.pause_tracking:
            return

        ts: datetime = event["timestamp"]
        prev_key: str | None = event["previous_key"]
        new_key: str = event["new_key"]
        prev_title: str = event["previous_title"]
        new_title: str = event["new_title"]

        if prev_key:
            self.last_left_at[prev_key] = ts

        if not prev_key or prev_key == new_key:
            return

        if self._is_noise_switch(ts, prev_key, new_key):
            return

        switch_id = self.db.insert_switch(ts, prev_title, new_title)
        self.last_switch_time = ts
        self.last_switch_pair = (prev_key, new_key)

        away_start = self.last_left_at.get(new_key)
        if away_start is not None:
            away_seconds = (ts - away_start).total_seconds()
            if self.settings.away_threshold_sec <= away_seconds <= MAX_POPUP_AWAY_SECONDS and not self._is_in_quiet_hours(ts.time()):
                cursor = QCursor.pos()
                popup_pos = self._safe_popup_pos(
                    cursor.x() + self.settings.popup_offset_x,
                    cursor.y() + self.settings.popup_offset_y,
                )
                self.popup.show_popup(switch_id, away_seconds, new_title, popup_pos)

        self.update_status()

    def _is_noise_switch(self, now: datetime, prev_key: str, new_key: str) -> bool:
        if self.last_switch_time is None or self.last_switch_pair is None:
            return False
        if (now - self.last_switch_time).total_seconds() > NOISE_SWITCH_SECONDS:
            return False
        return self.last_switch_pair == (new_key, prev_key)

    def _is_in_quiet_hours(self, current: dtime) -> bool:
        start = parse_hhmm(self.settings.quiet_start, "22:00")
        end = parse_hhmm(self.settings.quiet_end, "07:00")
        return is_within_quiet_hours(current, start, end)

    def _safe_popup_pos(self, x: int, y: int) -> QPoint:
        screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
        if screen is None:
            return QPoint(x, y)

        geo = screen.availableGeometry()
        width = max(self.popup.width(), 280)
        height = max(self.popup.height(), 140)
        clamped_x = max(geo.left(), min(x, geo.right() - width))
        clamped_y = max(geo.top(), min(y, geo.bottom() - height))
        return QPoint(clamped_x, clamped_y)

    def _on_popup_submit(self, switch_id: int, text: str) -> None:
        if text.strip():
            self.db.update_micro_task(switch_id, text)

    def update_status(self) -> None:
        hour = self.db.get_hour_count()
        day = self.db.get_day_count()
        state = "Paused" if self.settings.pause_tracking else "Active"
        self.tray.setToolTip(f"ResumeFlow ({state})\nThis hour: {hour}\nToday: {day}\nDashboard: {self.web_server.base_url}")
        self.action_score.setText(f"Context Switch Score: {day}")
        self.tray.setIcon(self._build_count_icon(hour))

    @staticmethod
    def _build_count_icon(hour_count: int) -> QIcon:
        text = str(min(hour_count, 99))
        pix = QPixmap(64, 64)
        pix.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#16a34a"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 60, 60)

        painter.setPen(QPen(QColor("white")))
        painter.setFont(QFont("Sans Serif", 24, QFont.Weight.Bold))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return QIcon(pix)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings)
        dialog.settings_saved.connect(self._save_settings)
        dialog.reset_requested.connect(self._confirm_reset_db)
        dialog.exec()

    def _save_settings(self, settings: AppSettings) -> None:
        settings.pause_tracking = self.settings.pause_tracking
        self.settings = settings
        self.db.save_settings(self.settings)
        self.popup.configure(self.settings.popup_auto_dismiss_on_focus_loss)

    def _confirm_reset_db(self) -> None:
        reply = QMessageBox.question(
            None,
            "Reset Database",
            "Delete all logged context switches?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.db.reset_switches()
            self.update_status()

    def open_report(self) -> None:
        rows = self.db.get_today_rows(limit=200)
        total = self.db.get_day_count()
        dialog = TodayReportDialog(rows, total, self.export_today_csv)
        dialog.exec()

    def export_today_csv(self) -> None:
        rows = self.db.get_today_rows(limit=10_000)
        default = str(APP_DIR / f"resumeflow-report-{datetime.now().strftime('%Y%m%d')}.csv")
        path, _ = QFileDialog.getSaveFileName(None, "Export Today's Report", default, "CSV Files (*.csv)")
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "previous_window_title", "new_window_title", "micro_task"])
                for row in rows:
                    writer.writerow([row["timestamp"], row["previous_window_title"], row["new_window_title"], row["micro_task"]])
        except Exception as exc:
            LOGGER.exception("Failed to export CSV")
            QMessageBox.critical(None, APP_NAME, f"Failed to export report:\n{exc}")
            return

        QMessageBox.information(None, APP_NAME, f"Report exported:\n{path}")

    def open_web_dashboard(self) -> None:
        QDesktopServices.openUrl(QUrl(self.web_server.base_url))

    def open_data_folder(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR)))

    def open_log_file(self) -> None:
        if not LOG_PATH.exists():
            LOG_PATH.touch(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH)))

    def quit(self) -> None:
        self.tracker.stop()
        self.web_server.stop()
        self.tray.hide()
        self.db.close()
        QApplication.quit()


def setup_logging() -> None:
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)

    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


def install_exception_hook() -> None:
    def _hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        LOGGER.error("Unhandled exception:\n%s", text)
        QMessageBox.critical(None, APP_NAME, f"Unexpected error:\n{exc_value}")

    sys.excepthook = _hook


def build_app() -> QApplication:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    return app


def run_headless(db: DatabaseManager) -> int:
    LOGGER.warning("Starting in headless mode (Qt unavailable or --headless requested): %s", QT_IMPORT_ERROR)
    web_server = WebDashboardServer(db)
    try:
        web_server.start()
    except OSError as exc:
        LOGGER.exception("Web dashboard failed to bind in headless mode")
        print(f"Failed to start dashboard server: {exc}")
        db.close()
        return 1

    tracker: HeadlessTracker | None = None
    try:
        pywinctl_module = load_pywinctl()
        tracker = HeadlessTracker(db, pywinctl_module)
        tracker.start()
        LOGGER.info("Headless window tracker started")
    except Exception:
        LOGGER.warning("Headless tracker unavailable; running dashboard-only mode", exc_info=True)

    print(f"ResumeFlow running in headless mode at {web_server.base_url}")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if tracker:
            tracker.stop()
        web_server.stop()
        db.close()
    return 0


def main() -> int:
    setup_logging()

    headless_forced = "--headless" in sys.argv
    db = DatabaseManager(DB_PATH)

    if headless_forced or not QT_AVAILABLE:
        return run_headless(db)

    app = build_app()
    install_exception_hook()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "System tray is unavailable on this system.")
        db.close()
        return 1

    try:
        pywinctl_module = load_pywinctl()
    except RuntimeError as exc:
        QMessageBox.critical(
            None,
            APP_NAME,
            f"{exc}\n\nTips:\n- Run inside a desktop session (not headless SSH).\n- Linux: ensure DISPLAY is set and X11/Wayland access is allowed.\n- macOS: enable Accessibility permissions for terminal/python.",
        )
        LOGGER.exception("Failed to import pywinctl")
        db.close()
        return 1

    tracker = WindowTracker(pywinctl_module)

    web_server = WebDashboardServer(db)
    try:
        web_server.start()
    except OSError as exc:
        LOGGER.exception("Web dashboard failed to bind")
        QMessageBox.warning(None, APP_NAME, f"Web dashboard failed to start on {web_server.base_url}:\n{exc}")

    _ = TrayController(db, tracker, web_server)

    try:
        tracker.start()
        LOGGER.info("ResumeFlow started")
    except Exception as exc:
        LOGGER.exception("Window tracking startup failed")
        QMessageBox.warning(None, APP_NAME, f"Window tracking startup failed:\n{exc}")

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
