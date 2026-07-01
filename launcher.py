#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
YT Downloader — Launcher (со встроенным сервером)
====================================================
Всё в одном .exe: GUI-лаунчер (PySide6) и локальный сервер (Flask + yt-dlp)
работают в одном процессе — сервер поднимается в фоновом потоке, отдельного
server.exe не требуется вообще.

Возможности:
  • Одна кнопка Run/Stop — запускает и останавливает встроенный сервер
  • Живой журнал сервера (сворачиваемый)
  • Индикатор состояния сервера (пинг http://127.0.0.1:5001/ping)
  • Сворачивание в системный трей вместо закрытия
  • Настройки с переключателями + кнопка «Сохранить»:
      - запускать сервер при старте программы
      - запускать программу вместе с Windows (в трее)
      - (опционально, для экстренных случаев) использовать внешний
        server.py / server.exe вместо встроенного — обычно не нужно,
        всё работает само

Расположение:
  yt-downloader/
  ├── launcher.py        <-- этот файл, всё остальное уже внутри него
  └── extension/

Установка зависимостей:
  pip install PySide6 flask flask-cors yt-dlp

Запуск:
  python launcher.py            — обычный запуск (окно показывается)
  python launcher.py --tray     — старт сразу в трее (используется автозапуском)

Сборка в один .exe:
  pyinstaller --noconfirm --clean --onefile --windowed ^
    --name "YT Downloader" --icon "build/icon.ico" ^
    --collect-all yt_dlp --collect-all flask_cors launcher.py
"""

import os
import re
import sys
import time
import shutil
import logging
import threading
import subprocess
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl, QObject, Signal, QProcess, QSettings,
    QPropertyAnimation, QEasingCurve, Property,
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit, QSystemTrayIcon, QMenu,
    QFrame, QAbstractButton, QGraphicsDropShadowEffect, QFileDialog,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply


# ═══════════════════════════════════════════════════════════════════════
#  ЧАСТЬ 1 — Сервер (то, что раньше было отдельным server.py)
# ═══════════════════════════════════════════════════════════════════════

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from yt_dlp import YoutubeDL
from werkzeug.serving import make_server

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5001

flask_app = Flask(__name__)
CORS(flask_app, resources={r"/*": {"origins": "*"}})

DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "YouTube")
os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

TEMP_DOWNLOAD_DIR = os.path.join(DEFAULT_DOWNLOAD_DIR, ".tmp_browser_downloads")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

# Прогресс активных загрузок: { job_id: {percent, speed, eta, status, filename} }
PROGRESS = {}


def human_size(num):
    if num is None:
        return "?"
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ПБ"


def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)[:200]


def _fmt_string(height):
    return (f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best")


def _fmt_size(f):
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    dur = f.get("duration")
    br = f.get("vbr") or f.get("abr") or f.get("tbr")
    if br and dur:
        return int(br * 1000 / 8 * dur)
    return 0


@flask_app.route("/formats", methods=["GET", "POST"])
def formats():
    url = request.args.get("url") or (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "no url"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    title = info.get("title", "video")
    duration = info.get("duration") or 0
    all_formats = info.get("formats", [])

    for f in all_formats:
        f.setdefault("duration", duration)

    heights = sorted(
        {f["height"] for f in all_formats if f.get("height") and f["height"] >= 360},
        reverse=True,
    )

    qualities = []
    for h in heights:
        total = 0
        exact = False
        try:
            sel_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                        "format": _fmt_string(h)}
            with YoutubeDL(sel_opts) as y2:
                sel = y2.process_ie_result(dict(info), download=False)
            chosen = sel.get("requested_formats")
            if chosen:
                total = sum(_fmt_size(f) for f in chosen)
                exact = all(
                    (f.get("filesize") or f.get("filesize_approx")) for f in chosen
                )
            else:
                total = _fmt_size(sel)
                exact = bool(sel.get("filesize") or sel.get("filesize_approx"))
        except Exception:
            total = 0

        qualities.append({
            "height": h,
            "label": f"{h}p",
            "bytes": int(total),
            "size_human": (("" if exact else "≈ ") + human_size(total))
            if total else "≈ ?",
            "exact": exact,
        })

    return jsonify({
        "title": title,
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "qualities": qualities,
    })


def _do_download(url, height, job_id, save_dir):
    PROGRESS[job_id] = {"status": "downloading", "percent": 0, "speed": "",
                        "eta": "", "filename": ""}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else 0
            PROGRESS[job_id].update({
                "percent": round(pct, 1),
                "speed": human_size(d.get("speed") or 0) + "/с",
                "eta": d.get("eta"),
            })
        elif d["status"] == "finished":
            PROGRESS[job_id].update({"percent": 100, "status": "processing"})

    fmt = _fmt_string(height)

    ydl_opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(save_dir, "%(title)s [%(height)sp].%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "postprocessor_args": {"ffmpeg": ["-hide_banner", "-loglevel", "error"]},
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fname = ydl.prepare_filename(info)
            if not fname.lower().endswith(".mp4"):
                fname = os.path.splitext(fname)[0] + ".mp4"
        PROGRESS[job_id].update({
            "status": "done",
            "percent": 100,
            "filename": os.path.basename(fname),
            "path": fname,
        })
    except Exception as e:
        PROGRESS[job_id].update({"status": "error", "error": str(e)})


@flask_app.route("/download", methods=["POST"])
def download():
    data = request.json or {}
    url = data.get("url")
    height = int(data.get("height", 1080))
    save_dir = data.get("save_dir") or TEMP_DOWNLOAD_DIR
    os.makedirs(save_dir, exist_ok=True)

    if not url:
        return jsonify({"error": "no url"}), 400

    job_id = str(len(PROGRESS) + 1) + "_" + str(int.from_bytes(os.urandom(2), "big"))
    t = threading.Thread(target=_do_download, args=(url, height, job_id, save_dir),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@flask_app.route("/progress")
def progress():
    job_id = request.args.get("job_id")
    return jsonify(PROGRESS.get(job_id, {"status": "unknown"}))


@flask_app.route("/file")
def get_file():
    job_id = request.args.get("job_id")

    deadline = time.time() + 30 * 60
    while time.time() < deadline:
        info = PROGRESS.get(job_id)
        if not info:
            return jsonify({"error": "unknown job"}), 404
        if info.get("status") == "error":
            return jsonify({"error": info.get("error", "download error")}), 500
        if info.get("status") == "done" and info.get("path"):
            break
        time.sleep(0.4)
    else:
        return jsonify({"error": "timeout"}), 504

    path = PROGRESS[job_id]["path"]
    if not os.path.exists(path):
        return jsonify({"error": "file missing"}), 404
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path),
                     mimetype="video/mp4")


@flask_app.route("/cleanup", methods=["POST", "GET"])
def cleanup():
    """Удаляет временный файл задания после того, как расширение подтвердило,
    что браузер полностью сохранил его в свои "Загрузки" (Ctrl+J)."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id") or request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "no job_id"}), 400

    info = PROGRESS.get(job_id)
    if not info:
        return jsonify({"ok": True, "skipped": "unknown job"})

    path = info.get("path")
    if not path:
        return jsonify({"ok": True, "skipped": "no path"})

    real_path = os.path.realpath(path)
    real_tmp_dir = os.path.realpath(TEMP_DOWNLOAD_DIR)
    if not real_path.startswith(real_tmp_dir + os.sep):
        return jsonify({"ok": True, "skipped": "not a temp file"})

    try:
        if os.path.exists(real_path):
            os.remove(real_path)
        info["path"] = None
        info["status"] = "cleaned"
        return jsonify({"ok": True, "deleted": real_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _bring_explorer_to_front(target):
    target = os.path.normpath(target)
    subprocess.Popen(f'explorer /select,"{target}"')


_LAST_OPEN = {}
_OPEN_COOLDOWN = 0.6


@flask_app.route("/open_folder", methods=["POST"])
def open_folder():
    data = request.json or {}
    job_id = data.get("job_id")
    info = PROGRESS.get(job_id) if job_id else None
    target = (info or {}).get("path")

    if not target or not os.path.exists(target):
        target = None
        done = [v.get("path") for v in PROGRESS.values()
                if v.get("status") == "done" and v.get("path") and os.path.exists(v["path"])]
        if done:
            target = max(done, key=lambda p: os.path.getmtime(p))

    folder = os.path.dirname(target) if target else TEMP_DOWNLOAD_DIR
    if not os.path.isdir(folder):
        folder = TEMP_DOWNLOAD_DIR

    key = os.path.normpath(folder)
    now = time.time()
    if now - _LAST_OPEN.get(key, 0) < _OPEN_COOLDOWN:
        return jsonify({"ok": True, "folder": folder, "target": target, "skipped": True})
    _LAST_OPEN[key] = now

    try:
        if os.name == "nt":
            if target and os.path.exists(target):
                _bring_explorer_to_front(target)
            else:
                os.startfile(os.path.normpath(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            if target and os.path.exists(target):
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return jsonify({"ok": True, "folder": folder, "target": target})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@flask_app.route("/ping")
def ping():
    return jsonify({"ok": True, "temp_dir": TEMP_DOWNLOAD_DIR})


class QtLogHandler(logging.Handler):
    """Перенаправляет строки логов Flask/werkzeug (например, лог запросов
    "127.0.0.1 - - [...] GET /ping HTTP/1.1 200 -") в журнал GUI."""

    def __init__(self, emit_func):
        super().__init__()
        self._emit_func = emit_func

    def emit(self, record):
        try:
            self._emit_func(self.format(record))
        except Exception:
            pass


class EmbeddedServer:
    """Поднимает Flask-сервер прямо в этом процессе, в отдельном потоке,
    вместо отдельного server.exe. Останавливается через werkzeug shutdown()
    — без принудительного kill(), поэтому никаких "Crashed" ошибок."""

    def __init__(self, host: str = SERVER_HOST, port: int = SERVER_PORT):
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None
        self._log_handler = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, log_func):
        if self.is_running():
            return

        self._log_handler = QtLogHandler(log_func)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        werkzeug_logger = logging.getLogger("werkzeug")
        werkzeug_logger.setLevel(logging.INFO)
        werkzeug_logger.addHandler(self._log_handler)
        werkzeug_logger.propagate = False

        self._httpd = make_server(self.host, self.port, flask_app, threaded=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        if self._httpd:
            try:
                self._httpd.server_close()
            except Exception:
                pass
        if self._log_handler:
            logging.getLogger("werkzeug").removeHandler(self._log_handler)
        self._httpd = None
        self._thread = None
        self._log_handler = None


# ═══════════════════════════════════════════════════════════════════════
#  ЧАСТЬ 2 — GUI лаунчер
# ═══════════════════════════════════════════════════════════════════════

APP_NAME = "YT Downloader"
AUTOSTART_REG_NAME = "YTDownloaderLauncher"
SERVER_PING_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/ping"

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


# ───────────────────────── Внешний сервер (опционально) ─────────────────────────

class ExternalServerProcess(QObject):
    """Запускает указанный пользователем файл (server.py или server.exe)
    отдельным процессом. Используется только в "экстренном" режиме —
    когда включён соответствующий переключатель и указан путь. По
    умолчанию не используется: сервер уже встроен в программу."""

    log_line = Signal(str)
    state_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stopping = False
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_output)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

    def is_running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self, path: str):
        if self.is_running():
            return
        p = Path(path)
        if not p.exists():
            self.log_line.emit(f"[Ошибка] Внешний сервер не найден: {path}")
            self.state_changed.emit(False)
            return

        self._stopping = False
        self.process.setWorkingDirectory(str(p.parent))
        if p.suffix.lower() == ".py":
            self.log_line.emit(f"▶ Запуск внешнего сервера: {sys.executable} {p}")
            self.process.setProgram(sys.executable)
            self.process.setArguments([str(p)])
        else:
            self.log_line.emit(f"▶ Запуск внешнего сервера: {p}")
            self.process.setProgram(str(p))
            self.process.setArguments([])
        self.process.start()
        self.state_changed.emit(True)

    def stop(self):
        if self.is_running():
            self.log_line.emit("■ Остановка внешнего сервера…")
            self._stopping = True
            self.process.terminate()
            if not self.process.waitForFinished(800):
                self.process.kill()
                self.process.waitForFinished(2000)
        self.state_changed.emit(False)

    def _on_output(self):
        data = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        for line in data.splitlines():
            if line.strip():
                self.log_line.emit(line)

    def _on_finished(self, code, status):
        if self._stopping:
            self.log_line.emit("⏹ Внешний сервер остановлен")
        else:
            self.log_line.emit(f"⏹ Внешний сервер остановлен (код {code})")
        self._stopping = False
        self.state_changed.emit(False)

    def _on_error(self, error):
        if self._stopping and error == QProcess.ProcessError.Crashed:
            return
        self.log_line.emit(f"[Ошибка процесса] {error}")


# ───────────────────────── Менеджер сервера (единая точка входа) ─────────────────────────

class ServerManager(QObject):
    """Решает, что использовать: встроенный сервер (по умолчанию) или
    внешний файл, указанный пользователем в настройках."""

    log_line = Signal(str)
    state_changed = Signal(bool)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.embedded = EmbeddedServer()
        self.external = ExternalServerProcess(self)
        self.external.log_line.connect(self.log_line)
        self.external.state_changed.connect(self.state_changed)

    def _use_external(self) -> bool:
        return bool(self.settings.value("use_external_server", False, type=bool))

    def is_running(self) -> bool:
        if self._use_external():
            return self.external.is_running()
        return self.embedded.is_running()

    def start(self):
        if self._use_external():
            path = self.settings.value("external_server_path", "", type=str)
            if not path:
                self.log_line.emit(
                    "[Внимание] Включён внешний сервер, но путь не задан — "
                    "запускаю встроенный."
                )
                self._start_embedded()
                return
            self.external.start(path)
        else:
            self._start_embedded()

    def _start_embedded(self):
        if self.embedded.is_running():
            return
        try:
            self.embedded.start(self.log_line.emit)
            self.log_line.emit(f"▶ Встроенный сервер запущен на http://{SERVER_HOST}:{SERVER_PORT}")
            self.state_changed.emit(True)
        except OSError as e:
            self.log_line.emit(f"[Ошибка] Не удалось запустить сервер: {e}")
            self.state_changed.emit(False)

    def stop(self):
        if self.external.is_running():
            self.external.stop()
        if self.embedded.is_running():
            self.log_line.emit("■ Остановка сервера…")
            self.embedded.stop()
            self.log_line.emit("⏹ Сервер остановлен")
            self.state_changed.emit(False)


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
QLabel#pathValue {{
    color: {TEXT_DIM};
    font-size: 11px;
    font-family: "Consolas", "Courier New", monospace;
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
QPushButton#miniBtn {{
    background: #2a2a2a;
    color: {TEXT};
    border: 1px solid #3a3a3a;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 11.5px;
    font-weight: 600;
}}
QPushButton#miniBtn:hover {{ background: #333333; }}
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
        self.resize(420, 680)
        self.setMinimumSize(380, 560)
        self.setStyleSheet(STYLESHEET)

        self._notified_tray_once = False
        self._logs_visible = False

        self.settings = QSettings("YTDownloader", "Launcher")

        self.server = ServerManager(self.settings, self)
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

        scl.addWidget(self._divider())

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

        scl.addWidget(self._divider())

        # Row 3: внешний сервер (экстренный режим)
        row3 = QHBoxLayout()
        row3_text = QVBoxLayout()
        row3_text.setSpacing(1)
        l3 = QLabel("Внешний сервер (опционально)")
        l3.setObjectName("settingLabel")
        l3.setWordWrap(True)
        h3 = QLabel(
            "Сервер уже встроен в программу — включай, только если хочешь "
            "запускать свой server.py / server.exe вручную."
        )
        h3.setObjectName("settingHint")
        h3.setWordWrap(True)
        row3_text.addWidget(l3)
        row3_text.addWidget(h3)
        self.toggle_external_server = ToggleSwitch()
        self.toggle_external_server.toggled.connect(self._on_external_toggled)
        self.toggle_external_server.toggled.connect(self._mark_dirty)
        row3.addLayout(row3_text, 1)
        row3.addWidget(self.toggle_external_server, 0, Qt.AlignmentFlag.AlignTop)
        scl.addLayout(row3)

        self.external_path_row = QWidget()
        ext_row_l = QHBoxLayout(self.external_path_row)
        ext_row_l.setContentsMargins(0, 4, 0, 0)
        ext_row_l.setSpacing(8)
        self.external_path_label = QLabel("Файл не выбран")
        self.external_path_label.setObjectName("pathValue")
        self.external_path_label.setWordWrap(True)
        choose_btn = QPushButton("Выбрать…")
        choose_btn.setObjectName("miniBtn")
        choose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        choose_btn.clicked.connect(self._choose_external_path)
        ext_row_l.addWidget(self.external_path_label, 1)
        ext_row_l.addWidget(choose_btn, 0)
        self.external_path_row.setVisible(False)
        scl.addWidget(self.external_path_row)

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

    def _divider(self) -> QFrame:
        divider = QFrame()
        divider.setObjectName("hline")
        return divider

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
        use_external = self.settings.value("use_external_server", False, type=bool)
        external_path = self.settings.value("external_server_path", "", type=str)

        self.toggle_server_on_launch.blockSignals(True)
        self.toggle_server_on_launch.setChecked(server_on_launch)
        self.toggle_server_on_launch.blockSignals(False)

        self.toggle_windows_autostart.blockSignals(True)
        self.toggle_windows_autostart.setChecked(windows_autostart)
        self.toggle_windows_autostart.blockSignals(False)

        self.toggle_external_server.blockSignals(True)
        self.toggle_external_server.setChecked(use_external)
        self.toggle_external_server.blockSignals(False)
        self.external_path_row.setVisible(use_external)

        self._external_path = external_path
        self._update_path_label()

        self._saved_state = (server_on_launch, windows_autostart, use_external, external_path)
        self._update_save_btn()

    def _mark_dirty(self, *_):
        self._update_save_btn()

    def _on_external_toggled(self, checked: bool):
        self.external_path_row.setVisible(checked)

    def _choose_external_path(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать внешний сервер", "",
            "Сервер (*.py *.exe);;Все файлы (*)",
        )
        if path:
            self._external_path = path
            self._update_path_label()
            self._mark_dirty()

    def _update_path_label(self):
        if self._external_path:
            self.external_path_label.setText(self._external_path)
        else:
            self.external_path_label.setText("Файл не выбран")

    def _current_state(self):
        return (
            self.toggle_server_on_launch.isChecked(),
            self.toggle_windows_autostart.isChecked(),
            self.toggle_external_server.isChecked(),
            self._external_path,
        )

    def _update_save_btn(self):
        dirty = self._current_state() != self._saved_state
        self.save_btn.setEnabled(dirty)
        self.save_btn.setObjectName("saveBtnDirty" if dirty else "saveBtn")
        self.save_btn.style().unpolish(self.save_btn)
        self.save_btn.style().polish(self.save_btn)

    def _save_settings(self):
        server_on_launch, windows_autostart, use_external, external_path = self._current_state()
        self.settings.setValue("start_server_on_launch", server_on_launch)
        self.settings.setValue("use_external_server", use_external)
        self.settings.setValue("external_server_path", external_path)
        set_autostart(windows_autostart)
        self._saved_state = (server_on_launch, windows_autostart, use_external, external_path)
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