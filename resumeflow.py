from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil
from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal, QTime
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "ResumeFlow"
CONFIG_DIR = Path.home() / ".resumeflow"
CONFIG_PATH = CONFIG_DIR / "settings.json"
DB_PATH = CONFIG_DIR / "resumeflow.db"


@dataclass
class WindowSnapshot:
    title: str
    app_name: str
    observed_at: dt.datetime

    @property
    def window_key(self) -> str:
        return f"{self.app_name}::{self.title}".strip()


@dataclass
class Settings:
    away_threshold_seconds: int = 30
    popup_position: str = "near_cursor"  # near_cursor | top_right | bottom_right
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"

    @classmethod
    def load(cls) -> "Settings":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            settings = cls()
            settings.save()
            return settings

        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

        defaults = dataclasses.asdict(cls())
        defaults.update(raw if isinstance(raw, dict) else {})
        settings = cls(**defaults)
        settings.away_threshold_seconds = max(30, min(300, int(settings.away_threshold_seconds)))
        if settings.popup_position not in {"near_cursor", "top_right", "bottom_right"}:
            settings.popup_position = "near_cursor"
        return settings

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(dataclasses.asdict(self), fh, indent=2)


class SwitchDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS switches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                switched_at TEXT NOT NULL,
                from_window TEXT,
                to_window TEXT,
                from_app TEXT,
                to_app TEXT,
                away_seconds REAL,
                resume_note TEXT
            )
            """
        )
        self.conn.commit()

    def log_switch(
        self,
        switched_at: dt.datetime,
        from_snapshot: Optional[WindowSnapshot],
        to_snapshot: WindowSnapshot,
        away_seconds: float,
        resume_note: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO switches (
                switched_at, from_window, to_window, from_app, to_app, away_seconds, resume_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                switched_at.isoformat(),
                from_snapshot.title if from_snapshot else "",
                to_snapshot.title,
                from_snapshot.app_name if from_snapshot else "",
                to_snapshot.app_name,
                away_seconds,
                resume_note,
            ),
        )
        self.conn.commit()

    def switch_count_for_day(self, day: dt.date) -> int:
        start = dt.datetime.combine(day, dt.time.min)
        end = dt.datetime.combine(day, dt.time.max)
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM switches WHERE switched_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        return int(cur.fetchone()[0])

    def switches_per_hour_for_day(self, day: dt.date) -> float:
        now = dt.datetime.now()
        start = dt.datetime.combine(day, dt.time.min)
        end = dt.datetime.combine(day, dt.time.max)
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM switches WHERE switched_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        total = int(cur.fetchone()[0])
        elapsed_hours = max((now - start).total_seconds() / 3600.0, 1.0)
        return total / elapsed_hours

    def weekly_report(self) -> list[tuple[str, int]]:
        now = dt.datetime.now()
        start = (now - dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        cur = self.conn.execute(
            """
            SELECT substr(switched_at, 1, 10) AS day, COUNT(*)
            FROM switches
            WHERE switched_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (start.isoformat(),),
        )
        return [(str(day), int(count)) for day, count in cur.fetchall()]


class ActiveWindowProvider:
    @staticmethod
    def current_window() -> Optional[WindowSnapshot]:
        now = dt.datetime.now()
        if sys.platform.startswith("win"):
            return ActiveWindowProvider._current_window_windows(now)
        if sys.platform == "darwin":
            return ActiveWindowProvider._current_window_mac(now)
        if sys.platform.startswith("linux"):
            return ActiveWindowProvider._current_window_linux(now)
        return None

    @staticmethod
    def _current_window_windows(now: dt.datetime) -> Optional[WindowSnapshot]:
        try:
            import pygetwindow as gw
            import ctypes
        except Exception:
            return None

        try:
            window = gw.getActiveWindow()
            if not window or not window.title:
                return None
            app_name = "Unknown"
            hwnd = getattr(window, "_hWnd", None)
            if hwnd:
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                app_name = psutil.Process(pid.value).name()
            return WindowSnapshot(title=window.title.strip(), app_name=app_name.strip(), observed_at=now)
        except Exception:
            return None

    @staticmethod
    def _current_window_mac(now: dt.datetime) -> Optional[WindowSnapshot]:
        try:
            from AppKit import NSWorkspace
        except Exception:
            return None

        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if not app:
                return None
            app_name = app.localizedName() or "Unknown"
            pid = app.processIdentifier()
            title = app_name
            try:
                proc = psutil.Process(pid)
                title = f"{app_name} - {proc.name()}"
            except Exception:
                pass
            return WindowSnapshot(title=title.strip(), app_name=app_name.strip(), observed_at=now)
        except Exception:
            return None

    @staticmethod
    def _current_window_linux(now: dt.datetime) -> Optional[WindowSnapshot]:
        # Best-effort fallback for development environments.
        try:
            win_id = subprocess.check_output(["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True)
            win_id = win_id.strip().split()[-1]
            if win_id == "0x0":
                return None
            title_out = subprocess.check_output(["xprop", "-id", win_id, "WM_NAME"], text=True)
            title = title_out.split("=", 1)[-1].strip().strip('"')
            return WindowSnapshot(title=title or "Unknown", app_name="LinuxApp", observed_at=now)
        except Exception:
            return None


class FloatingResumePopup(QWidget):
    submitted = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        card = QWidget()
        card.setStyleSheet(
            """
            QWidget { background: #202124; color: #f5f6f7; border: 1px solid #3b3f44; border-radius: 8px; font-size: 12px; }
            QLineEdit { background: #111316; border: 1px solid #3b3f44; border-radius: 4px; padding: 4px; color: #f5f6f7; }
            QPushButton { background: #2663eb; border: none; border-radius: 4px; color: white; padding: 4px 8px; }
            """
        )

        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.checklist_input = QLineEdit()
        self.checklist_input.setPlaceholderText("Ready to Resume: type your next micro-task")
        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.clicked.connect(self._on_submit)

        layout = QVBoxLayout(card)
        layout.addWidget(self.message_label)
        layout.addWidget(self.checklist_input)
        layout.addWidget(dismiss_btn, alignment=Qt.AlignmentFlag.AlignRight)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(card)

    def show_message(self, text: str, position: str):
        self.message_label.setText(text)
        self.checklist_input.clear()
        self.adjustSize()

        cursor = QCursor.pos()
        screen = QApplication.primaryScreen().availableGeometry()
        if position == "top_right":
            self.move(screen.right() - self.width() - 20, screen.top() + 20)
        elif position == "bottom_right":
            self.move(screen.right() - self.width() - 20, screen.bottom() - self.height() - 20)
        else:
            self.move(cursor.x() + 12, cursor.y() + 12)

        self.show()
        self.raise_()

    def _on_submit(self):
        self.submitted.emit(self.checklist_input.text().strip())
        self.hide()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ResumeFlow Settings")
        self.settings = settings

        self.threshold = QSpinBox()
        self.threshold.setRange(30, 300)
        self.threshold.setValue(settings.away_threshold_seconds)
        self.threshold.setSuffix(" sec")

        self.position = QComboBox()
        self.position.addItems(["near_cursor", "top_right", "bottom_right"])
        self.position.setCurrentText(settings.popup_position)

        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_start.setTime(QTime.fromString(settings.quiet_start, "HH:mm"))

        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_end.setTime(QTime.fromString(settings.quiet_end, "HH:mm"))

        form = QFormLayout()
        form.addRow("Away threshold", self.threshold)
        form.addRow("Popup position", self.position)
        form.addRow("Quiet hours start", self.quiet_start)
        form.addRow("Quiet hours end", self.quiet_end)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(buttons)

    def apply(self):
        self.settings.away_threshold_seconds = int(self.threshold.value())
        self.settings.popup_position = self.position.currentText()
        self.settings.quiet_start = self.quiet_start.time().toString("HH:mm")
        self.settings.quiet_end = self.quiet_end.time().toString("HH:mm")
        self.settings.save()


class MonitorThread(QThread):
    switched = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.running = True
        self.previous: Optional[WindowSnapshot] = None

    def run(self):
        while self.running:
            current = ActiveWindowProvider.current_window()
            if current and self.previous and current.window_key != self.previous.window_key:
                self.switched.emit(self.previous, current)
            if current:
                self.previous = current
            self.msleep(1000)

    def stop(self):
        self.running = False


class ResumeFlowApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.settings = Settings.load()
        self.db = SwitchDatabase(DB_PATH)
        self.popup = FloatingResumePopup()
        self.popup.submitted.connect(self.on_popup_submitted)

        self.pending_resume_context: Optional[tuple[WindowSnapshot, float]] = None
        self.last_seen_by_window: dict[str, WindowSnapshot] = {}
        self.switches_today = self.db.switch_count_for_day(dt.date.today())
        self.last_switch_from: Optional[WindowSnapshot] = None

        self.tray = QSystemTrayIcon(self._build_icon())
        self.menu = QMenu()
        self.score_action = QAction("Context Switch Score: --")
        self.score_action.setEnabled(False)
        self.switches_action = QAction("Switches this hour: --")
        self.switches_action.setEnabled(False)

        self.menu.addAction(self.score_action)
        self.menu.addAction(self.switches_action)
        self.menu.addSeparator()

        weekly_action = QAction("Show weekly report")
        weekly_action.triggered.connect(self.show_weekly_report)
        self.menu.addAction(weekly_action)

        settings_action = QAction("Settings")
        settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(settings_action)

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.quit)
        self.menu.addAction(quit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self.monitor = MonitorThread()
        self.monitor.switched.connect(self.on_switch)
        self.monitor.start()

        self.score_timer = QTimer()
        self.score_timer.timeout.connect(self.refresh_score)
        self.score_timer.start(60_000)
        self.refresh_score()

    def _build_icon(self) -> QIcon:
        pix = QPixmap(64, 64)
        pix.fill(QColor("#1b1f24"))
        painter = QPainter(pix)
        painter.setPen(QColor("#5bc0ff"))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "RF")
        painter.end()
        return QIcon(pix)

    def is_quiet_hours(self) -> bool:
        now = dt.datetime.now().time()
        start = dt.datetime.strptime(self.settings.quiet_start, "%H:%M").time()
        end = dt.datetime.strptime(self.settings.quiet_end, "%H:%M").time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    def on_switch(self, from_snapshot: WindowSnapshot, to_snapshot: WindowSnapshot):
        now = dt.datetime.now()
        away_seconds = 0.0

        previous_seen = self.last_seen_by_window.get(to_snapshot.window_key)
        if previous_seen:
            away_seconds = (to_snapshot.observed_at - previous_seen.observed_at).total_seconds()

        self.db.log_switch(now, from_snapshot, to_snapshot, away_seconds)
        self.switches_today += 1
        self.last_switch_from = from_snapshot
        self.last_seen_by_window[from_snapshot.window_key] = from_snapshot
        self.last_seen_by_window[to_snapshot.window_key] = to_snapshot

        if previous_seen and away_seconds >= self.settings.away_threshold_seconds and not self.is_quiet_hours():
            minutes = max(1, int(away_seconds // 60))
            context = previous_seen.title or to_snapshot.title
            message = (
                f"You were last here {minutes} minute(s) ago. "
                f"You were working on: {context}"
            )
            self.pending_resume_context = (to_snapshot, away_seconds)
            self.popup.show_message(message, self.settings.popup_position)

        self.refresh_score()

    def on_popup_submitted(self, note: str):
        if not self.pending_resume_context:
            return
        snapshot, elapsed = self.pending_resume_context
        self.db.log_switch(dt.datetime.now(), self.last_switch_from, snapshot, elapsed, note)
        self.pending_resume_context = None

    def refresh_score(self):
        day = dt.date.today()
        per_hour = self.db.switches_per_hour_for_day(day)
        score = min(int(per_hour * 10), 100)
        self.score_action.setText(f"Context Switch Score: {score}/100")
        self.switches_action.setText(f"Switches this hour: {per_hour:.1f}")
        self.tray.setToolTip(f"{APP_NAME}: {self.switches_today} switches today | Score {score}")

    def show_weekly_report(self):
        report = self.db.weekly_report()
        if not report:
            self.tray.showMessage(APP_NAME, "No weekly switch data yet.", QSystemTrayIcon.MessageIcon.Information)
            return
        lines = [f"{day}: {count}" for day, count in report]
        self.tray.showMessage(APP_NAME, "Weekly context switches\n" + "\n".join(lines), QSystemTrayIcon.MessageIcon.Information)

    def open_settings(self):
        dialog = SettingsDialog(self.settings)
        if dialog.exec():
            dialog.apply()

    def quit(self):
        self.monitor.stop()
        self.monitor.wait(2000)
        self.tray.hide()
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def main():
    app = ResumeFlowApp()
    raise SystemExit(app.run())


if __name__ == "__main__":
    main()
