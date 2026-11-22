#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки сервера (адрес и порт), которые переживают перезапуск."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import library

DEFAULTS = {"host": "0.0.0.0", "port": 5000}


def path() -> Path:
    return library.DATA_DIR / "settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        stored = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update(stored)
    except (OSError, ValueError):
        pass

    host = str(data.get("host") or DEFAULTS["host"]).strip()
    data["host"] = host or DEFAULTS["host"]
    try:
        port = int(data.get("port", DEFAULTS["port"]))
    except (TypeError, ValueError):
        port = DEFAULTS["port"]
    data["port"] = port if 1 <= port <= 65535 else DEFAULTS["port"]
    return data


def save(host: str, port: int) -> dict:
    settings = {"host": (host or DEFAULTS["host"]).strip(), "port": int(port)}
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def host_choices() -> list[tuple[str, str]]:
    """Варианты привязки для формы: все интерфейсы, localhost и адреса этой машины."""
    choices: list[tuple[str, str]] = [
        ("0.0.0.0", "Все интерфейсы — доступ из домашней сети"),
        ("127.0.0.1", "Только этот компьютер"),
    ]
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addresses = []
    for address in addresses:
        if address.startswith("127."):
            continue
        choices.append((address, f"Только этот адрес — {address}"))
    return choices
