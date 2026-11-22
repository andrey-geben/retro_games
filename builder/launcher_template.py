#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обёртка «одна игра — один exe» (шаблон Retro Web).

В этот файл при сборке подставляются @@APP_NAME@@ и @@APP_ID@@.
Рядом (внутри exe) лежат: dosbox/, game/ и default.conf.

Запускает игру под DOSBox; сохранения — в %LOCALAPPDATA%\\<APP_ID>\\.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "@@APP_NAME@@"
APP_ID = "@@APP_ID@@"
DATA_DIR_ENV = "RETRO_GAME_DATA_DIR"

GAME_DIR_NAME = "game"
DOSBOX_DIR_NAME = "dosbox"
DOSBOX_EXE_NAME = "dosbox.exe"
CONFIG_NAME = "dosbox.conf"
DEFAULT_CONFIG_NAME = "default.conf"

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

_mutex_handle = None
_log_path: Path | None = None


# --------------------------------------------------------------------------- #
# Пути
# --------------------------------------------------------------------------- #


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_root() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


# --------------------------------------------------------------------------- #
# Логи и ошибки
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    if _log_path is None:
        return
    try:
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        if _log_path.exists() and _log_path.stat().st_size > 1_000_000:
            _log_path.replace(_log_path.with_suffix(".log.1"))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def show_error(message: str, details: str = "") -> None:
    log("ОШИБКА: " + message)
    text = f"{APP_NAME}\n\n{message}"
    if details:
        text += f"\n\n{details}"
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Первая справка
# --------------------------------------------------------------------------- #

HELP_FLAG = "help_shown.flag"
README_NAME = "КАК_ИГРАТЬ.txt"

HELP_TEXT = (
    f"{APP_NAME}\n\n"
    "ПЕРЕД НАЧАЛОМ — КОРОТКО ОБ УПРАВЛЕНИИ\n"
    "\n"
    "1. Мышь и клавиатура\n"
    "   DOSBox захватывает мышь, когда игра это требует (шутеры, стратегии).\n"
    "   Освободить мышь и клавиатуру: Ctrl + F10 (или средняя кнопка мыши).\n"
    "   Снова захватить: кликните по окну игры.\n"
    "   Чувствительность мыши: ползунок в меню DOSBox (иконка мыши слева).\n"
    "\n"
    "2. Экран\n"
    "   Полный экран / окно: Alt + Enter.\n"
    "   Масштаб и сглаживание: меню DOSBox слева (иконка шестерёнки).\n"
    "\n"
    "3. Как выйти из игры\n"
    "   Esc — меню игры, затем пункт Quit / Exit.\n"
    "   Если игра не отпускает: Ctrl + F10, затем закройте окно DOSBox.\n"
    "\n"
    "4. Как закрыть приложение\n"
    "   Alt + F4 или крестик окна.\n"
    "   Если окно зависло: Ctrl + Shift + Esc → «Диспетчер задач» →\n"
    "   завершить процесс dosbox.exe.\n"
    "\n"
    "5. Сохранения\n"
    "   Лежат в папке данных приложения (откроется кнопкой ниже).\n"
    "\n"
    "Эта инструкция сохранена рядом с сохранениями: " + README_NAME
)


def show_first_run_help(data_dir: Path) -> None:
    """Показывает справку один раз (и кладёт файл с инструкцией рядом)."""
    if not _first_time(data_dir):
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / README_NAME).write_text(HELP_TEXT, encoding="utf-8")
    except OSError:
        pass
    if os.name == "nt":
        try:
            # 0x40 — иконка информации; окно модальное, закрывается кнопкой OK.
            ctypes.windll.user32.MessageBoxW(
                None, HELP_TEXT, f"{APP_NAME} — как играть", 0x40
            )
        except Exception:
            print(HELP_TEXT)
    else:
        print(HELP_TEXT)
    try:
        (data_dir / HELP_FLAG).write_text("1", encoding="utf-8")
    except OSError:
        pass


def _first_time(data_dir: Path) -> bool:
    try:
        return not (data_dir / HELP_FLAG).exists()
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# Подготовка данных
# --------------------------------------------------------------------------- #


def copy_missing(source: Path, target: Path) -> int:
    """Копируем только отсутствующие файлы: сохранения не трогаем."""
    copied = 0
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        destination = target / item.relative_to(source)
        if destination.exists():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            copied += 1
        except OSError as exc:
            log(f"Не удалось скопировать {item.name}: {exc}")
    return copied


def ensure_config(conf: Path, reset: bool) -> None:
    if reset and conf.exists():
        conf.unlink()
        log("Конфиг удалён (--reset)")
    if conf.exists():
        return
    default = resource_root() / DEFAULT_CONFIG_NAME
    if not default.is_file():
        raise FileNotFoundError(f"В сборке нет {DEFAULT_CONFIG_NAME}")
    conf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(default, conf)
    log(f"Создан конфиг: {conf}")


def acquire_single_instance() -> bool:
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        _mutex_handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}_launcher")
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


def run_game(dosbox_exe: Path, conf: Path, data_dir: Path, fullscreen: bool | None) -> int:
    args = [str(dosbox_exe), "--noprimaryconf", "--conf", str(conf)]
    if fullscreen is True:
        args += ["--set", "fullscreen=on"]
    elif fullscreen is False:
        args += ["--set", "fullscreen=off"]

    log("Команда: " + " ".join(args))
    log(f"Рабочая папка: {data_dir}")

    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        with (data_dir / "dosbox.log").open("w", encoding="utf-8", errors="replace") as fh:
            completed = subprocess.run(
                args,
                cwd=str(data_dir),
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
    except FileNotFoundError:
        show_error("Не найден dosbox.exe в сборке.")
        return 1
    except OSError as exc:
        show_error("Не удалось запустить DOSBox.", str(exc))
        return 1

    log(f"DOSBox завершился с кодом {completed.returncode}")
    return completed.returncode


def parse_args(argv: list[str]) -> dict:
    options = {"fullscreen": None, "reset": False, "help": False}
    for arg in argv[1:]:
        value = arg.lower()
        if value in ("--fullscreen", "-f"):
            options["fullscreen"] = True
        elif value in ("--windowed", "-w"):
            options["fullscreen"] = False
        elif value in ("--reset", "--reset-config"):
            options["reset"] = True
        elif value in ("--help", "-h", "/?"):
            options["help"] = True
    return options


def main(argv: list[str] | None = None) -> int:
    global _log_path

    argv = list(sys.argv if argv is None else argv)
    options = parse_args(argv)

    root = resource_root()
    data_dir = data_root()
    _log_path = data_dir / "launcher.log"

    if options["help"]:
        message = (
            f"{APP_NAME}\n\n"
            "Как играть:\n"
            "  Ctrl+F10 — освободить мышь и клавиатуру\n"
            "  Alt+Enter — полный экран / окно\n"
            "  Alt+F4 — закрыть приложение\n"
            "  Esc — меню игры, затем Quit\n\n"
            "Ключи запуска:\n"
            "  --fullscreen, -f   на весь экран\n"
            "  --windowed, -w     в окне\n"
            "  --reset            пересоздать dosbox.conf\n\n"
            f"Данные и сохранения: {data_dir}"
        )
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
        else:
            print(message)
        return 0

    log("=" * 60)
    log(f"Запуск {APP_NAME}")
    log(f"Ресурсы: {root}")
    log(f"Данные:  {data_dir}")

    show_first_run_help(data_dir)

    if not acquire_single_instance():
        show_error("Игра уже запущена.", "Закройте окно DOSBox и попробуйте снова.")
        return 0

    dosbox_exe = root / DOSBOX_DIR_NAME / DOSBOX_EXE_NAME
    game_source = root / GAME_DIR_NAME
    if not dosbox_exe.is_file():
        show_error("Не найдено ядро DOSBox в сборке.", str(dosbox_exe))
        return 1
    if not game_source.is_dir():
        show_error("Не найдены файлы игры в сборке.", str(game_source))
        return 1

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        game_dir = data_dir / GAME_DIR_NAME
        copied = copy_missing(game_source, game_dir)
        if copied:
            log(f"Скопировано файлов игры: {copied}")

        conf = data_dir / CONFIG_NAME
        ensure_config(conf, options["reset"])
        run_game(dosbox_exe, conf, data_dir, options["fullscreen"])
    except OSError as exc:
        show_error("Ошибка подготовки к запуску.", str(exc))
        return 1
    except Exception as exc:  # pragma: no cover
        log(f"Непредвиденная ошибка: {exc!r}")
        show_error("Непредвиденная ошибка.", repr(exc))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
