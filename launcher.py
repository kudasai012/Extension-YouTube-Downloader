#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
YT Downloader — Launcher
=========================
Современный GUI-лаунчер (PySide6) для локального сервера yt-dlp.

Возможности:
  • Одна кнопка Run/Stop — запускает и останавливает сервер
  • Живой журнал сервера (сворачиваемый)
  • Индикатор состояния сервера (пинг http://127.0.0.1:5001/ping)
  • Сворачивание в системный трей вместо закрытия
  • Настройки с переключателями + кнопка «Сохранить»:
      - запускать сервер при старте программы
      - запускать программу вместе с Windows (в трее)

Расположение:
  yt-downloader/
  ├── launcher.py        <-- этот файл лежит здесь, в корне проекта
  ├── server/
  │   └── server.py
  └── extension/

Установка зависимостей:
  pip install PySide6

Запуск:
  python launcher.py            — обычный запуск (окно показывается)
  python launcher.py --tray     — старт сразу в трее (используется автозапуском)
"""

import sys
import shutil
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl, QObject, Signal, QProcess, QSettings,
    QPropertyAnimation, QEasingCurve, Property,
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit, QSystemTrayIcon, QMenu,
    QFrame, QAbstractButton, QGraphicsDropShadowEffect, QSizePolicy,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply


# ───────────────────────── Константы ─────────────────────────

APP_NAME = "YT Downloader"
AUTOSTART_REG_NAME = "YTDownloaderLauncher"
SERVER_PING_URL = "http://127.0.0.1:5001/ping"

BASE_DIR = Path(__file__).resolve().parent
SERVER_DIR = BASE_DIR / "server"
SERVER_SCRIPT = SERVER_DIR / "server.py"

# Плоская современная палитра в духе обновлённого YouTube (тёмная тема)
ACCENT = "#ff0033"
ACCENT_HOVER = "#e6002e"
BG = "#0f0f0f"
CARD = "#1a1a1a"
CARD_ALT = "#161616"
BORDER = "#2a2a2a"
TEXT = "#f1f1f1"
TEXT_DIM = "#9a9a9a"
GREEN = "#3ddc84"
RED = "#ff4b4b"


# ───────────────────────── Автозапуск (Windows) ─────────────────────────

def _is_windows() -> bool:
    return sys.platform == "win32"


def _autostart_command() -> str:
    """Команда, которая будет прописана в реестре автозапуска."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = Path(sys.executable)
    pythonw = exe.parent / "pythonw.exe"
    python_bin = str(pythonw) if pythonw.exists() else str(exe)
    script = str(Path(__file__).resolve())
    return f'"{python_bin}" "{script}" --tray'


def is_autostart_enabled() -> bool:
    if not _is_windows():
        return False
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        ) as key:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
            return bool(val)
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enabled: bool) -> None:
    if not _is_windows():
        return
    import winreg
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_REG_NAME)
            except FileNotFoundError:
                pass


# ───────────────────────── Иконка приложения ─────────────────────────

def make_app_icon() -> QIcon:
    """Круглая красная иконка со стрелкой загрузки — без внешних файлов."""
    size = 128
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(ACCENT)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)

    pen = QPen(QColor("#ffffff"))
    pen.setWidth(10)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    cx = size / 2
    top, bottom = size * 0.28, size * 0.62
    painter.drawLine(int(cx), int(top), int(cx), int(bottom))

    left = (cx - size * 0.16, bottom - size * 0.16)
    tip = (cx, bottom)
    right = (cx + size * 0.16, bottom - size * 0.16)
    painter.drawLine(int(left[0]), int(left[1]), int(tip[0]), int(tip[1]))
    painter.drawLine(int(tip[0]), int(tip[1]), int(right[0]), int(right[1]))

    base_y = size * 0.80
    painter.drawLine(int(size * 0.30), int(base_y), int(size * 0.70), int(base_y))

    painter.end()
    return QIcon(pix)


def make_dot_pixmap(color: str, size: int = 10) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pix


# ───────────────────────── Тумблер-переключатель ─────────────────────────

class ToggleSwitch(QAbstractButton):
    """Современный анимированный переключатель (вместо стандартного чекбокса)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._knob_pos = 3.0
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool):
        self._anim.stop()
        self._anim.setStartValue(self._knob_pos)
        self._anim.setEndValue(23.0 if checked else 3.0)
        self._anim.start()

    def getKnobPos(self):
        return self._knob_pos

    def setKnobPos(self, value):
        self._knob_pos = value
        self.update()

    knobPos = Property(float, getKnobPos, setKnobPos)

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        self._knob_pos = 23.0 if checked else 3.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        track_color = QColor(ACCENT) if self.isChecked() else QColor("#3a3a3a")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        d = rect.height() - 6
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(int(self._knob_pos), 3, d, d)
        painter.end()


# ───────────────────────── Пингер сервера ─────────────────────────

class Pinger(QObject):
    status_changed = Signal(bool)

    def __init__(self, url: str, interval_ms: int = 2000, parent=None):
        super().__init__(parent)
        self._url = QUrl(url)
        self._manager = QNetworkAccessManager(self)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._check)
        self._last_status = None

    def start(self):
        self._check()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _check(self):
        req = QNetworkRequest(self._url)
        try:
            req.setTransferTimeout(1500)
        except Exception:
            pass
        reply = self._manager.get(req)
        reply.finished.connect(lambda r=reply: self._on_finished(r))

    def _on_finished(self, reply):
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        if ok != self._last_status:
            self._last_status = ok
            self.status_changed.emit(ok)


# ───────────────────────── Менеджер сервера ─────────────────────────

class ServerManager(QObject):
    log_line = Signal(str)
    state_changed = Signal(bool)  # True = запущен

    def __init__(self, script_path: Path, work_dir: Path, parent=None):
        super().__init__(parent)
        self.script_path = script_path
        self.work_dir = work_dir
        self._stopping = False  # True, пока идёт наша собственная остановка
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(work_dir))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_output)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

    def is_running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def _resolve_target(self):
        """Что запускать: если рядом лежит собранный server.exe (релизная
        сборка) — используем его напрямую. Иначе запускаем server.py текущим
        интерпретатором Python (режим разработки / запуск из исходников)."""
        exe_path = self.work_dir / "server.exe"
        if exe_path.exists():
            return str(exe_path), []
        if self.script_path.exists():
            return self._find_python(), [str(self.script_path)]
        return None, None

    def start(self):
        if self.is_running():
            return
        self._stopping = False
        program, args = self._resolve_target()
        if not program:
            self.log_line.emit(
                f"[Ошибка] Не найден ни server.exe, ни {self.script_path.name} в {self.work_dir}"
            )
            return
        self.log_line.emit(f"▶ Запуск сервера: {program} {' '.join(args)}".strip())
        self.process.setProgram(program)
        self.process.setArguments(args)
        self.process.start()
        self.state_changed.emit(True)

    def stop(self):
        if self.is_running():
            self.log_line.emit("■ Остановка сервера…")
            self._stopping = True
            # На Windows у процесса без окна/консоли terminate() обычно
            # ничего не может сделать (некому послать WM_CLOSE/Ctrl+Break),
            # поэтому почти сразу переходим к принудительному завершению —
            # это ожидаемо и не является реальной ошибкой процесса.
            self.process.terminate()
            if not self.process.waitForFinished(800):
                self.process.kill()
                self.process.waitForFinished(2000)
        self.state_changed.emit(False)

    def _find_python(self) -> str:
        if getattr(sys, "frozen", False):
            for cand in ("pythonw", "python"):
                p = shutil.which(cand)
                if p:
                    return p
            return "python"
        return sys.executable

    def _on_output(self):
        data = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        for line in data.splitlines():
            if line.strip():
                self.log_line.emit(line)

    def _on_finished(self, code, status):
        if self._stopping:
            # Это результат нашей же остановки — не пугаем пользователя кодом.
            self.log_line.emit("⏹ Сервер остановлен")
        else:
            self.log_line.emit(f"⏹ Сервер остановлен (код {code})")
        self._stopping = False
        self.state_changed.emit(False)

    def _on_error(self, error):
        # Когда мы сами останавливаем процесс через kill(), Qt всегда
        # репортит это как ProcessError.Crashed — это ожидаемое поведение
        # принудительного завершения, а не реальная ошибка сервера.
        if self._stopping and error == QProcess.ProcessError.Crashed:
            return
        self.log_line.emit(f"[Ошибка процесса] {error}")


# ───────────────────────── Стили ─────────────────────────

STYLESHEET = f"""
QMainWindow, QWidget#root {{
    background: {BG};
}}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#dim {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#appTitle {{
    color: {TEXT};
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 0.2px;
}}
QLabel#appSubtitle {{
    color: {TEXT_DIM};
    font-size: 11.5px;
}}
QLabel#sectionTitle {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 700;
}}
QLabel#settingLabel {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 500;
}}
QLabel#settingHint {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame#logCard {{
    background: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame#hline {{
    background: {BORDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}
QPushButton#powerOff {{
    background: #262626;
    color: {TEXT};
    border: 1px solid #3a3a3a;
    border-radius: 26px;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.3px;
}}
QPushButton#powerOff:hover {{ background: #303030; }}
QPushButton#powerOn {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 26px;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.3px;
}}
QPushButton#powerOn:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#ghost {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    font-size: 12px;
    font-weight: 600;
    text-align: left;
    padding: 4px 0;
}}
QPushButton#ghost:hover {{ color: {TEXT}; }}
QPushButton#saveBtn {{
    background: #2a2a2a;
    color: {TEXT_DIM};
    border: 1px solid #3a3a3a;
    border-radius: 10px;
    padding: 9px 18px;
    font-size: 12.5px;
    font-weight: 700;
}}
QPushButton#saveBtn:disabled {{
    background: #212121;
    color: #5a5a5a;
    border: 1px solid #2a2a2a;
}}
QPushButton#saveBtnDirty {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 10px;
    padding: 9px 18px;
    font-size: 12.5px;
    font-weight: 700;
}}
QPushButton#saveBtnDirty:hover {{ background: {ACCENT_HOVER}; }}
QFrame#statusChip {{
    background: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QPlainTextEdit {{
    background: #000000;
    color: #b9f6ca;
    border: none;
    border-radius: 10px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 11px;
    padding: 8px;
}}
"""


# ───────────────────────── Главное окно ─────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, app_icon: QIcon):
        super().__init__()
        self.app_icon = app_icon
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon)
        self.resize(420, 620)
        self.setMinimumSize(380, 520)
        self.setStyleSheet(STYLESHEET)

        self._notified_tray_once = False
        self._logs_visible = False

        self.settings = QSettings("YTDownloader", "Launcher")

        self.server = ServerManager(SERVER_SCRIPT, SERVER_DIR, self)
        self.server.log_line.connect(self._append_log)
        self.server.state_changed.connect(self._on_server_state)

        self.pinger = Pinger(SERVER_PING_URL, 2000, self)
        self.pinger.status_changed.connect(self._on_ping_status)

        self._build_ui()
        self._build_tray()
        self._load_settings()

        self.pinger.start()

    # ---------- UI ----------

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 20, 20, 18)
        outer.setSpacing(16)

        # ---- Header ----
        header = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(self.app_icon.pixmap(36, 36))
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Сервер загрузки видео")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addWidget(icon_label)
        header.addSpacing(10)
        header.addLayout(title_box)
        header.addStretch(1)

        self.status_chip = QFrame()
        self.status_chip.setObjectName("statusChip")
        chip_l = QHBoxLayout(self.status_chip)
        chip_l.setContentsMargins(10, 6, 12, 6)
        chip_l.setSpacing(7)
        self.dot_label = QLabel()
        self.dot_label.setPixmap(make_dot_pixmap("#888888", 9))
        self.status_text = QLabel("Проверка…")
        self.status_text.setStyleSheet("font-size: 12px; font-weight: 600;")
        chip_l.addWidget(self.dot_label)
        chip_l.addWidget(self.status_text)
        header.addWidget(self.status_chip)

        outer.addLayout(header)

        # ---- Power card ----
        power_card = QFrame()
        power_card.setObjectName("card")
        pc = QVBoxLayout(power_card)
        pc.setContentsMargins(24, 30, 24, 26)
        pc.setSpacing(14)
        pc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.power_btn = QPushButton("Run")
        self.power_btn.setObjectName("powerOff")
        self.power_btn.setFixedSize(200, 52)
        self.power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_btn.clicked.connect(self._toggle_server)

        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(0)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(255, 0, 51, 0))
        self.power_btn.setGraphicsEffect(self._glow)

        self.power_caption = QLabel("Сервер остановлен")
        self.power_caption.setObjectName("dim")
        self.power_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)

        pc.addWidget(self.power_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        pc.addWidget(self.power_caption)
        outer.addWidget(power_card)

        # ---- Settings card ----
        settings_card = QFrame()
        settings_card.setObjectName("card")
        scl = QVBoxLayout(settings_card)
        scl.setContentsMargins(18, 16, 18, 16)
        scl.setSpacing(12)

        sec_title = QLabel("Настройки")
        sec_title.setObjectName("sectionTitle")
        scl.addWidget(sec_title)

        # Row 1: запускать сервер при старте программы
        row1 = QHBoxLayout()
        row1_text = QVBoxLayout()
        row1_text.setSpacing(1)
        l1 = QLabel("Запускать сервер при старте программы")
        l1.setObjectName("settingLabel")
        l1.setWordWrap(True)
        h1 = QLabel("Сервер стартует автоматически вместе с лаунчером")
        h1.setObjectName("settingHint")
        h1.setWordWrap(True)
        row1_text.addWidget(l1)
        row1_text.addWidget(h1)
        self.toggle_server_on_launch = ToggleSwitch()
        self.toggle_server_on_launch.toggled.connect(self._mark_dirty)
        row1.addLayout(row1_text, 1)
        row1.addWidget(self.toggle_server_on_launch, 0, Qt.AlignmentFlag.AlignTop)
        scl.addLayout(row1)

        divider = QFrame()
        divider.setObjectName("hline")
        scl.addWidget(divider)

        # Row 2: автозапуск с Windows
        row2 = QHBoxLayout()
        row2_text = QVBoxLayout()
        row2_text.setSpacing(1)
        l2 = QLabel("Запускать при включении Windows")
        l2.setObjectName("settingLabel")
        l2.setWordWrap(True)
        h2 = QLabel("Программа будет тихо стартовать в трее" if _is_windows()
                     else "Доступно только в Windows")
        h2.setObjectName("settingHint")
        h2.setWordWrap(True)
        row2_text.addWidget(l2)
        row2_text.addWidget(h2)
        self.toggle_windows_autostart = ToggleSwitch()
        self.toggle_windows_autostart.toggled.connect(self._mark_dirty)
        if not _is_windows():
            self.toggle_windows_autostart.setEnabled(False)
        row2.addLayout(row2_text, 1)
        row2.addWidget(self.toggle_windows_autostart, 0, Qt.AlignmentFlag.AlignTop)
        scl.addLayout(row2)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.setEnabled(False)
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(self.save_btn)
        scl.addLayout(save_row)

        outer.addWidget(settings_card)

        # ---- Logs (collapsible) ----
        self.logs_toggle_btn = QPushButton("▸  Журнал сервера")
        self.logs_toggle_btn.setObjectName("ghost")
        self.logs_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logs_toggle_btn.clicked.connect(self._toggle_logs)
        outer.addWidget(self.logs_toggle_btn)

        self.log_card = QFrame()
        self.log_card.setObjectName("logCard")
        log_l = QVBoxLayout(self.log_card)
        log_l.setContentsMargins(8, 8, 8, 8)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_l.addWidget(self.log_view)
        self.log_card.setVisible(False)
        outer.addWidget(self.log_card, 1)

        hint = QLabel("Окно можно закрыть — программа останется в трее.")
        hint.setObjectName("dim")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(hint)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()

        self.act_show = QAction("Показать окно", self)
        self.act_show.triggered.connect(self._show_window)
        menu.addAction(self.act_show)

        menu.addSeparator()

        self.act_power = QAction("▶  Run", self)
        self.act_power.triggered.connect(self._toggle_server)
        menu.addAction(self.act_power)

        menu.addSeparator()

        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    # ---------- Настройки ----------

    def _load_settings(self):
        server_on_launch = self.settings.value("start_server_on_launch", True, type=bool)
        windows_autostart = is_autostart_enabled()

        self.toggle_server_on_launch.blockSignals(True)
        self.toggle_server_on_launch.setChecked(server_on_launch)
        self.toggle_server_on_launch.blockSignals(False)

        self.toggle_windows_autostart.blockSignals(True)
        self.toggle_windows_autostart.setChecked(windows_autostart)
        self.toggle_windows_autostart.blockSignals(False)

        self._saved_state = (server_on_launch, windows_autostart)
        self._update_save_btn()

    def _mark_dirty(self, *_):
        self._update_save_btn()

    def _current_state(self):
        return (
            self.toggle_server_on_launch.isChecked(),
            self.toggle_windows_autostart.isChecked(),
        )

    def _update_save_btn(self):
        dirty = self._current_state() != self._saved_state
        self.save_btn.setEnabled(dirty)
        self.save_btn.setObjectName("saveBtnDirty" if dirty else "saveBtn")
        self.save_btn.style().unpolish(self.save_btn)
        self.save_btn.style().polish(self.save_btn)

    def _save_settings(self):
        server_on_launch, windows_autostart = self._current_state()
        self.settings.setValue("start_server_on_launch", server_on_launch)
        set_autostart(windows_autostart)
        self._saved_state = (server_on_launch, windows_autostart)
        self._update_save_btn()

        self.save_btn.setText("Сохранено ✓")
        QTimer.singleShot(1500, lambda: self.save_btn.setText("Сохранить"))

    # ---------- Логика ----------

    def _append_log(self, text: str):
        self.log_view.appendPlainText(text)

    def _toggle_server(self):
        if self.server.is_running():
            self.server.stop()
        else:
            self.server.start()

    def _toggle_logs(self):
        self._logs_visible = not self._logs_visible
        self.log_card.setVisible(self._logs_visible)
        arrow = "▾" if self._logs_visible else "▸"
        self.logs_toggle_btn.setText(f"{arrow}  Журнал сервера")

    def _on_server_state(self, running: bool):
        if running:
            self.power_btn.setText("Stop")
            self.power_btn.setObjectName("powerOn")
            self._glow.setColor(QColor(255, 0, 51, 160))
            self._glow.setBlurRadius(40)
            self.power_caption.setText("Сервер работает — нажмите, чтобы остановить")
            self.act_power.setText("■  Stop")
        else:
            self.power_btn.setText("Run")
            self.power_btn.setObjectName("powerOff")
            self._glow.setColor(QColor(255, 0, 51, 0))
            self._glow.setBlurRadius(0)
            self.power_caption.setText("Сервер остановлен — нажмите, чтобы запустить")
            self.act_power.setText("▶  Run")
        self.power_btn.style().unpolish(self.power_btn)
        self.power_btn.style().polish(self.power_btn)

    def _on_ping_status(self, ok: bool):
        if ok:
            self.dot_label.setPixmap(make_dot_pixmap(GREEN, 9))
            self.status_text.setText("Онлайн")
        else:
            self.dot_label.setPixmap(make_dot_pixmap(RED, 9))
            self.status_text.setText("Офлайн")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self.pinger.stop()
        self.server.stop()
        self.tray.hide()
        QApplication.instance().quit()

    # ---------- Переопределения ----------

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if not self._notified_tray_once:
            self._notified_tray_once = True
            self.tray.showMessage(
                APP_NAME,
                "Программа свёрнута в трей. Сервер продолжает работать.",
                self.app_icon,
                3000,
            )


# ───────────────────────── main ─────────────────────────

def main():
    start_in_tray = "--tray" in sys.argv[1:]

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    icon = make_app_icon()
    app.setWindowIcon(icon)

    window = MainWindow(icon)

    if window.settings.value("start_server_on_launch", True, type=bool):
        window.server.start()

    if start_in_tray:
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
