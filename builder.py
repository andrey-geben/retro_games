#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка автономного exe для игры: DOSBox + игра + лаунчер в одном файле.

Использует PyInstaller (должен быть установлен в том же Python, что и сервер)
и шаблон лаунчера из папки builder/.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BUILDER_DIR = Path(__file__).resolve().parent / "builder"
DOSBOX_DIR = BUILDER_DIR / "dosbox"
LAUNCHER_TEMPLATE = BUILDER_DIR / "launcher_template.py"
ICON_FILE = BUILDER_DIR / "icon.ico"

# Транслитерация для имён файлов: PyInstaller спокойнее относится к ASCII.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class BuildError(Exception):
    """Сборка не удалась; текст показывается пользователю."""


def pyinstaller_available() -> bool:
    return importlib.util.find_spec("PyInstaller") is not None


def slugify(text: str, fallback: str = "dos-game") -> str:
    """Имя exe-файла: латиница, цифры, дефисы."""
    result = []
    for char in text.lower():
        if char in _TRANSLIT:
            result.append(_TRANSLIT[char])
        elif char.isascii() and (char.isalnum() or char in " -_"):
            result.append(char)
        else:
            result.append("-")
    slug = "".join(result).strip()
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.replace(" ", "-").strip("-")
    # убираем повторные дефисы после замен
    parts = [part for part in slug.split("-") if part]
    slug = "-".join(parts)[:40].strip("-")
    return slug or fallback


def app_id_for(title: str, game_id: str) -> str:
    """Имя папки данных в %LOCALAPPDATA% — латиница + короткий ID."""
    return f"{slugify(title, 'dos-game')}-{game_id[:6]}"


def check_prerequisites() -> None:
    if not pyinstaller_available():
        raise BuildError(
            "PyInstaller не установлен в Python, под которым запущен сервер. "
            "Установите: python -m pip install pyinstaller"
        )
    for path in (DOSBOX_DIR, LAUNCHER_TEMPLATE):
        if not path.exists():
            raise BuildError(f"Нет файлов сборщика: {path}")
    if not (DOSBOX_DIR / "dosbox.exe").is_file():
        raise BuildError("В builder/dosbox нет dosbox.exe — проверьте комплект сборщика.")


def _write_launcher(target: Path, app_name: str, app_id: str) -> None:
    template = LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    code = template.replace("@@APP_NAME@@", app_name).replace("@@APP_ID@@", app_id)
    target.write_text(code, encoding="utf-8")


def build_exe(
    title: str,
    game_id: str,
    files_dir: Path,
    conf_text: str,
    work_dir: Path,
) -> Path:
    """Собирает exe и возвращает путь к готовому файлу (внутри work_dir/dist)."""
    check_prerequisites()

    slug = slugify(title)
    app_id = app_id_for(title, game_id)

    work_dir.mkdir(parents=True, exist_ok=True)
    with (work_dir / "build.log").open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"build start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log_file.flush()

        shutil.copytree(DOSBOX_DIR, work_dir / "dosbox")
        shutil.copytree(files_dir, work_dir / "game")
        (work_dir / "default.conf").write_text(conf_text, encoding="utf-8")
        _write_launcher(work_dir / "launcher.py", title, app_id)

        command = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", slug,
            "--distpath", str(work_dir / "dist"),
            "--workpath", str(work_dir / "pyi-build"),
            "--specpath", str(work_dir / "pyi-build"),
            "--log-level", "WARN",
            "--add-data", f"{work_dir / 'dosbox'}{os.pathsep}dosbox",
            "--add-data", f"{work_dir / 'game'}{os.pathsep}game",
            "--add-data", f"{work_dir / 'default.conf'}{os.pathsep}.",
        ]
        if ICON_FILE.is_file():
            command += ["--icon", str(ICON_FILE)]
        command.append(str(work_dir / "launcher.py"))

        log_file.write(" ".join(command) + "\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYINSTALLER_CONFIG_DIR"] = str(work_dir / ".pyi-cache")
        result = subprocess.run(
            command,
            cwd=str(work_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )

        exe_path = work_dir / "dist" / f"{slug}.exe"
        if result.returncode != 0 or not exe_path.is_file():
            tail = _tail(work_dir / "build.log")
            raise BuildError("PyInstaller завершился с ошибкой.\n" + tail)

        return exe_path


def _tail(path: Path, limit: int = 1500) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]
