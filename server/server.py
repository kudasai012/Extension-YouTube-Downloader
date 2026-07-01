"""
Локальный сервер для скачивания видео с YouTube.
Работает в паре с браузерным расширением (Chrome / Яндекс Браузер).

Запуск:
    pip install -r requirements.txt
    python server.py

Сервер поднимается на http://127.0.0.1:5001
Расширение обращается к нему за списком качеств и для скачивания.
"""

import os
import re
import sys
import json
import threading
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context, send_file
from flask_cors import CORS
from yt_dlp import YoutubeDL

app = Flask(__name__)
# Разрешаем запросы со страниц YouTube (content script расширения)
CORS(app, resources={r"/*": {"origins": "*"}})

# ----- Папка для сохранения -----
# По умолчанию — папка "Загрузки" пользователя
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "YouTube")
os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

# ----- Временная папка для подготовки файла перед браузерной загрузкой -----
TEMP_DOWNLOAD_DIR = os.path.join(DEFAULT_DOWNLOAD_DIR, ".tmp_browser_downloads")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

# Прогресс активных загрузок: { job_id: {percent, speed, eta, status, filename} }
PROGRESS = {}


def human_size(num):
    """Человекочитаемый размер."""
    if num is None:
        return "?"
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ПБ"


def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)[:200]


# ---------------------------------------------------------------------------
# /formats — вернуть доступные качества и примерный вес
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /formats — вернуть доступные качества и ТОЧНЫЙ вес
# ---------------------------------------------------------------------------

# Строка выбора формата — ДОЛЖНА совпадать с той, что в _do_download,
# чтобы оценка веса соответствовала реально скачиваемым потокам.
def _fmt_string(height):
    # Без привязки к ext=mp4: иначе для 4K (часто VP9/AV1 webm) yt-dlp брал бы
    # "лучший mp4" = 1080p. Берём лучшее видео нужной высоты в любом кодеке
    # и склеиваем с лучшим аудио в mp4.
    return (f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best")


def _fmt_size(f):
    """Точный размер формата в байтах. Приоритет: filesize -> filesize_approx
    -> оценка по точному битрейту (vbr/abr/tbr) и длительности."""
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    dur = f.get("duration")
    # битрейт именно этого потока (кбит/с)
    br = f.get("vbr") or f.get("abr") or f.get("tbr")
    if br and dur:
        return int(br * 1000 / 8 * dur)
    return 0


@app.route("/formats", methods=["GET", "POST"])
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

    # длительность каждого формата (если нет — берём общую) — для оценки по битрейту
    for f in all_formats:
        f.setdefault("duration", duration)

    # Доступные высоты видеопотоков (>= 360p)
    heights = sorted(
        {f["height"] for f in all_formats if f.get("height") and f["height"] >= 360},
        reverse=True,
    )

    qualities = []
    for h in heights:
        # Просим yt-dlp выбрать ИМЕННО те потоки, что скачаются для этой высоты —
        # тогда размер точный, а не "самый тяжёлый из всех".
        total = 0
        exact = False
        try:
            sel_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                        "format": _fmt_string(h)}
            with YoutubeDL(sel_opts) as y2:
                sel = y2.process_ie_result(dict(info), download=False)
            chosen = sel.get("requested_formats")
            if chosen:  # раздельные видео+аудио
                total = sum(_fmt_size(f) for f in chosen)
                exact = all(
                    (f.get("filesize") or f.get("filesize_approx")) for f in chosen
                )
            else:  # один объединённый поток
                total = _fmt_size(sel)
                exact = bool(sel.get("filesize") or sel.get("filesize_approx"))
        except Exception:
            total = 0

        qualities.append({
            "height": h,
            "label": f"{h}p",
            "bytes": int(total),
            # точный размер — без "≈", оценка — с "≈"
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


# ---------------------------------------------------------------------------
# /download — скачать выбранное качество
# ---------------------------------------------------------------------------
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

    # Формат: лучшее видео <= выбранной высоты (любой кодек) + лучшее аудио,
    # склейка в mp4. Без ext=mp4-приоритета, иначе 4K (VP9/AV1) терялся.
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


@app.route("/download", methods=["POST"])
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


@app.route("/progress")
def progress():
    job_id = request.args.get("job_id")
    return jsonify(PROGRESS.get(job_id, {"status": "unknown"}))


@app.route("/file")
def get_file():
    """Отдаёт готовый mp4 браузеру, чтобы загрузка появилась в списке загрузок
    браузера (Ctrl+J) с уведомлением. Если файл ещё качается — ждём готовности
    (long-poll), поэтому расширение может запустить загрузку браузера сразу."""
    import time
    job_id = request.args.get("job_id")

    # Ждём, пока yt-dlp скачает и склеит файл (макс. 30 минут)
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


def _bring_explorer_to_front(target):
    """Windows: открыть папку с файлом, выделив его.
    Простой надёжный способ — explorer /select,"путь". Explorer сам откроет
    нужную папку и подсветит файл (и переиспользует окно, если оно открыто)."""
    target = os.path.normpath(target)
    # /select требует, чтобы путь и запятая шли слитно одним аргументом
    subprocess.Popen(f'explorer /select,"{target}"')


# Анти-дубликат: гасим только "вспышки" из двойного клика (доли секунды),
# осознанные повторные клики должны срабатывать (поднимать окно вперёд).
_LAST_OPEN = {}
_OPEN_COOLDOWN = 0.6


@app.route("/open_folder", methods=["POST"])
def open_folder():
    """Открывает папку с загрузками в проводнике И выводит её на передний план."""
    import time
    data = request.json or {}
    job_id = data.get("job_id")
    info = PROGRESS.get(job_id) if job_id else None
    target = (info or {}).get("path")

    # Подстраховка: если путь к файлу неизвестен/не существует — берём папку
    # последней успешной загрузки, иначе папку по умолчанию.
    if not target or not os.path.exists(target):
        target = None
        # ищем самый свежий готовый файл
        done = [v.get("path") for v in PROGRESS.values()
                if v.get("status") == "done" and v.get("path") and os.path.exists(v["path"])]
        if done:
            target = max(done, key=lambda p: os.path.getmtime(p))

    folder = os.path.dirname(target) if target else TEMP_DOWNLOAD_DIR
    if not os.path.isdir(folder):
        folder = TEMP_DOWNLOAD_DIR

    print(f"[open_folder] job={job_id} target={target!r} folder={folder!r}")

    # гасим только "вспышки" двойного клика
    key = os.path.normpath(folder)
    now = time.time()
    if now - _LAST_OPEN.get(key, 0) < _OPEN_COOLDOWN:
        return jsonify({"ok": True, "folder": folder, "target": target, "skipped": True})
    _LAST_OPEN[key] = now

    try:
        if os.name == "nt":
            if target and os.path.exists(target):
                _bring_explorer_to_front(target)   # открыть папку + выделить файл
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


@app.route("/cleanup", methods=["POST", "GET"])
def cleanup():
    """Удаляет временный файл задания после того, как расширение подтвердило,
    что браузер полностью сохранил его в свои "Загрузки" (Ctrl+J).
    Из соображений безопасности удаляем только файлы, лежащие внутри
    TEMP_DOWNLOAD_DIR — то есть только те, что были скачаны специально для
    последующей отправки в браузер, а не файлы из пользовательской папки
    сохранения (save_dir), если она была указана отдельно."""
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
        # Файл сохранён не во временную папку — не трогаем его.
        return jsonify({"ok": True, "skipped": "not a temp file"})

    try:
        if os.path.exists(real_path):
            os.remove(real_path)
        info["path"] = None
        info["status"] = "cleaned"
        return jsonify({"ok": True, "deleted": real_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/ping")
def ping():
    return jsonify({"ok": True, "temp_dir": TEMP_DOWNLOAD_DIR})


if __name__ == "__main__":
    print("=" * 50)
    print(" YouTube Downloader — локальный сервер")
    print(" Адрес:        http://127.0.0.1:5001")
    print(" Временная папка подготовки:", TEMP_DOWNLOAD_DIR)
    print("=" * 50)
    app.run(host="127.0.0.1", port=5001, threaded=True)
