#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал действий: пишем в data/logs/app.log и показываем в админке."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "retro"
LOG_FILE: Path | None = None
_configured = False


def setup(data_dir: Path) -> None:
    global LOG_FILE, _configured
    if _configured:
        return

    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = logs_dir / "app.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    _configured = True


def log(message: str, level: int = logging.INFO) -> None:
    logging.getLogger(LOGGER_NAME).log(level, message)


def tail(lines: int = 400) -> list[str]:
    if LOG_FILE is None or not LOG_FILE.is_file():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-lines:]
    except OSError:
        return []


def size_human() -> str:
    if LOG_FILE is None or not LOG_FILE.is_file():
        return "0 Б"
    size = LOG_FILE.stat().st_size
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024 or unit == "МБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} МБ"
