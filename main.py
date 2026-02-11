#!/usr/bin/env python3
"""ResumeFlow - production-ready local desktop context switch tracker.

Install dependencies:
    pip install pyqt6 pywinctl psutil
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, time as dtime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPen, QPixmap
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

try:
    import psutil
except Exception:  # optional dependency
    psutil = None

import pywinctl

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

LOGGER = logging.getLogger(APP_NAME)


@dataclass
class AppSettings:
    away_threshold_sec: int = 30
    popup_offset_x: int = 18
    popup_offset_y: int = 20
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    popup_auto_dismiss_on_focus_loss: bool = False
    pause_tracking: bool = False


class DatabaseManager:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._migrate()

    def _configure_connection(self) -> None:
        with self.conn:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.execute("PRAGMA temp_store = MEMORY")
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self) -> None:
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
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_switches_timestamp ON switches(timestamp)"
            )

    def close(self) -> None:
        self.conn.close()

    def load_settings(self) -> AppSettings:
        base = AppSettings()
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        kv = {r["key"]: r["value"] for r in rows}

        base.away_threshold_sec = max(
            MIN_THRESHOLD,
            min(MAX_THRESHOLD, self._safe_int(kv.get("away_threshold_sec"), base.away_threshold_sec)),
        )
        base.popup_offset_x = self._safe_int(kv.get("popup_offset_x"), base.popup_offset_x)
        base.popup_offset_y = self._safe_int(kv.get("popup_offset_y"), base.popup_offset_y)
        base.quiet_start = kv.get("quiet_start", base.quiet_start)
        base.quiet_end = kv.get("quiet_end", base.quiet_end)
        base.popup_auto_dismiss_on_focus_loss = kv.get("popup_auto_dismiss_on_focus_loss", "0") == "1"
        base.pause_tracking = kv.get("pause_tracking", "0") == "1"
        return base

    def save_settings(self, settings: AppSettings) -> None:
        values = {
            "away_threshold_sec": str(settings.away_threshold_sec),
            "popup_offset_x": str(settings.popup_offset_x),
            "popup_offset_y": str(settings.popup_offset_y),
            "quiet_start": settings.quiet_start,
            "quiet_end": settings.quiet_end,
            "popup_auto_dismiss_on_focus_loss": "1" if settings.popup_auto_dismiss_on_focus_loss else "0",
            "pause_tracking": "1" if settings.pause_tracking else "0",
        }
        with self.conn:
            for k, v in values.items():
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v),
                )

    def insert_switch(self, timestamp: datetime, previous_title: str, new_title: str) -> int:
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
        with self.conn:
            self.conn.execute(
                "UPDATE switches SET micro_task = ? WHERE id = ?",
                (task_text.strip() or None, switch_id),
            )

    def get_hour_count(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start.replace(minute=59, second=59, microsecond=999999)
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM switches WHERE timestamp BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row["c"]) if row else 0

    def get_day_count(self, day: datetime | None = None) -> int:
        day = day or datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM switches WHERE timestamp BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row["c"]) if row else 0

    def get_today_rows(self, limit: int = 100) -> list[sqlite3.Row]:
        day = datetime.now()
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        return self.conn.execute(
            """
            SELECT id, timestamp, previous_window_title, new_window_title, micro_task
            FROM switches
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (start.isoformat(), end.isoformat(), limit),
        ).fetchall()

    def reset_switches(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM switches")
            self.conn.execute("VACUUM")

    @staticmethod
    def _safe_int(v: str | None, fallback: int) -> int:
        try:
            return int(v) if v is not None else fallback
        except ValueError:
            return fallback


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
        self.info_label.setWordWrap(True)
        self.work_label = QLabel()
        self.work_label.setWordWrap(True)

        text_label = QLabel("Next micro-task (optional):")
        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText("e.g., Finalize section intro")
        self.task_input.returnPressed.connect(self._submit)

        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._submit)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)
        card_layout.addWidget(self.info_label)
        card_layout.addWidget(self.work_label)
        card_layout.addWidget(text_label)
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
        self.offset_x.setRange(-500, 500)
        self.offset_x.setValue(settings.popup_offset_x)

        self.offset_y = QSpinBox()
        self.offset_y.setRange(-500, 500)
        self.offset_y.setValue(settings.popup_offset_y)

        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_start.setTime(datetime.strptime(settings.quiet_start, "%H:%M").time())

        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_end.setTime(datetime.strptime(settings.quiet_end, "%H:%M").time())

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
    def __init__(self, rows: list[sqlite3.Row], total: int, on_export: callable):
        super().__init__()
        self._on_export = on_export
        self.setWindowTitle("Today's Report")
        self.setMinimumSize(960, 500)

        heading = QLabel(f"Context Switch Score today: <b>{total}</b>")
        heading.setTextFormat(Qt.TextFormat.RichText)

        self.table = QTableWidget(len(rows), 4)
        self.table.setHorizontalHeaderLabels(["Time", "From", "To", "Micro-task"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        for i, row in enumerate(rows):
            stamp = row["timestamp"].replace("T", " ")[:19]
            values = [stamp, row["previous_window_title"] or "", row["new_window_title"] or "", row["micro_task"] or ""]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, c, item)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self._on_export)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)

        foot = QHBoxLayout()
        foot.addWidget(export_btn)
        foot.addStretch()
        foot.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.table)
        layout.addLayout(foot)


class WindowTracker(QObject):
    switched = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
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
            windows = pywinctl.getAllWindows()
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

    @staticmethod
    def _safe_active_window() -> Any | None:
        try:
            return pywinctl.getActiveWindow()
        except Exception:
            return None


class TrayController(QObject):
    def __init__(self, db: DatabaseManager, tracker: WindowTracker):
        super().__init__()
        self.db = db
        self.tracker = tracker
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

        self.action_score = QAction("Context Switch Score: 0", self.menu)
        self.action_score.setEnabled(False)

        self.action_quit = QAction("Quit", self.menu)
        self.action_quit.triggered.connect(self.quit)

        self.menu.addAction(self.action_pause)
        self.menu.addAction(self.action_settings)
        self.menu.addAction(self.action_report)
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
            if away_seconds >= self.settings.away_threshold_sec and not self._is_in_quiet_hours(ts.time()):
                cursor = QCursor.pos()
                popup_pos = QPoint(
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
        start = datetime.strptime(self.settings.quiet_start, "%H:%M").time()
        end = datetime.strptime(self.settings.quiet_end, "%H:%M").time()
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _on_popup_submit(self, switch_id: int, text: str) -> None:
        if text.strip():
            self.db.update_micro_task(switch_id, text)

    def update_status(self) -> None:
        hour = self.db.get_hour_count()
        day = self.db.get_day_count()
        state = "Paused" if self.settings.pause_tracking else "Active"
        self.tray.setToolTip(f"ResumeFlow ({state})\nThis hour: {hour}\nToday: {day}")
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

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "previous_window_title", "new_window_title", "micro_task"])
            for row in rows:
                writer.writerow([row["timestamp"], row["previous_window_title"], row["new_window_title"], row["micro_task"]])

        QMessageBox.information(None, "ResumeFlow", f"Report exported:\n{path}")

    def quit(self) -> None:
        self.tracker.stop()
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


def main() -> int:
    setup_logging()
    app = build_app()
    install_exception_hook()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "System tray is unavailable on this system.")
        return 1

    db = DatabaseManager(DB_PATH)
    tracker = WindowTracker()
    controller = TrayController(db, tracker)
    _ = controller

    try:
        tracker.start()
        LOGGER.info("ResumeFlow started")
    except Exception as exc:
        LOGGER.exception("Window tracking startup failed")
        QMessageBox.warning(None, APP_NAME, f"Window tracking startup failed:\n{exc}")

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
