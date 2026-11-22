"""Проверка игры «запускается ли в браузере» перед публикацией.

Запускает настоящий js-dos в отдельном окне Edge на локальной странице,
ждёт загрузку эмулятора и смотрит, появилось ли изображение на экране.
Если экран остаётся чёрным — игра помечается «только .EXE».
"""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import library

ROOT = Path(__file__).resolve().parent
JSDOS_DIR = ROOT / "static" / "vendor" / "js-dos"
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

PROBE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>PROBE-{token}</title>
<link rel="stylesheet" href="jsdos/js-dos.css">
<style>html,body{{margin:0;height:100%;background:#000}}#dos{{width:100vw;height:100vh}}</style>
</head><body><div id="dos"></div>
<script src="jsdos/js-dos.js"></script>
<script>
setTimeout(function(){{
  Dos(document.getElementById('dos'), {{
    url: 'game.jsdos', pathPrefix: 'jsdos/emulators/',
    theme: 'dark', lang: 'ru', backend: '{backend}', backendLocked: true,
    workerThread: false, autoStart: true, autoSave: false,
    mouseCapture: false, imageRendering: 'pixelated', renderAspect: '4/3'
  }});
}}, 300);
</script></body></html>
"""


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def browser_available() -> bool:
    return any(p.is_file() for p in EDGE_CANDIDATES)


def _edge() -> Path:
    for path in EDGE_CANDIDATES:
        if path.is_file():
            return path
    raise RuntimeError("Microsoft Edge не найден")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    user32 = ctypes.windll.user32
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _find_window(token: str) -> int | None:
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if token in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def _bright_ratio(image) -> float:
    """Доля светлых пикселей в центральной части кадра."""
    width, height = image.size
    left, top = int(width * 0.12), int(height * 0.05)
    right, bottom = int(width * 0.88), int(height * 0.95)
    crop = image.crop((left, top, right, bottom)).convert("L")
    hist = crop.histogram()
    bright = sum(hist[28:])
    total = max(1, crop.width * crop.height)
    return bright / total


def run_browser_probe(game: dict, timeout: float = 45.0) -> tuple[str, str, float]:
    """Возвращает (play_status, note, ratio)."""
    if not browser_available():
        return "unknown", "Edge не найден — проверка недоступна", 0.0

    bundle = library.bundle_path(game["id"])
    if not bundle.is_file():
        return "broken", "Бандл не найден", 0.0

    token = f"PROBE-{game['id']}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix="probe-", dir=str(library.DATA_DIR / "tmp")))
    server = None
    browser = None
    profile = work / "profile"
    try:
        shutil.copytree(JSDOS_DIR, work / "jsdos")
        os.link(bundle, work / "game.jsdos")
        backend = game.get("backend") or "dosbox"
        (work / "index.html").write_text(
            PROBE_HTML.format(token=token, backend=backend), encoding="utf-8")

        port = _free_port()
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=str(work), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        browser = subprocess.Popen(
            [str(_edge()), f"--user-data-dir={profile}", "--no-first-run",
             "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
             "--window-size=1200,800", "--window-position=20,20",
             f"--app=http://127.0.0.1:{port}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        hwnd = None
        deadline = time.time() + 20
        while time.time() < deadline and hwnd is None:
            hwnd = _find_window(token)
            time.sleep(0.4)
        if hwnd is None:
            return "unknown", "Окно проверки не открылось", 0.0

        best = 0.0
        started = time.time()
        while time.time() - started < timeout:
            time.sleep(3.0)
            rect = _window_rect(hwnd)
            if not rect:
                continue
            left, top, right, bottom = rect
            if right - left < 50 or bottom - top < 50:
                continue
            try:
                from PIL import ImageGrab

                shot = ImageGrab.grab(bbox=(left, top, right, bottom))
            except Exception:
                continue
            best = max(best, _bright_ratio(shot))

        if best >= 0.01:
            return "ok", f"Изображение появилось (светлых пикселей {best * 100:.1f}%)", best
        return "exe_only", "Чёрный экран: в браузере не запускается", best
    finally:
        if browser is not None:
            subprocess.run(["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(work, ignore_errors=True)
