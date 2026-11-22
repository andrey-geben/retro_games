#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Хранилище библиотеки игр: SQLite + папки на диске."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
GAMES_DIR = DATA_DIR / "games"
DB_PATH = DATA_DIR / "library.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    exe         TEXT,
    machine     TEXT,
    cycles      TEXT,
    sbtype      TEXT,
    status      TEXT NOT NULL DEFAULT 'draft',
    cover       TEXT,
    bundle_size INTEGER NOT NULL DEFAULT 0,
    total_size  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    id         TEXT PRIMARY KEY,
    game_id    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'running',
    message    TEXT,
    created_at TEXT NOT NULL
);
"""


def configure(data_dir: Path) -> None:
    """Позволяет увести данные в другую папку (тесты, портативный режим)."""
    global DATA_DIR, GAMES_DIR, DB_PATH
    DATA_DIR = Path(data_dir).resolve()
    GAMES_DIR = DATA_DIR / "games"
    DB_PATH = DATA_DIR / "library.sqlite3"


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GAMES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        _migrate(db)


def _migrate(db: sqlite3.Connection) -> None:
    """Добавляем недостающие колонки в старые базы."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(games)")}
    if "source" not in columns:
        db.execute("ALTER TABLE games ADD COLUMN source TEXT")
    if "plays" not in columns:
        db.execute("ALTER TABLE games ADD COLUMN plays INTEGER NOT NULL DEFAULT 0")
    if "description" not in columns:
        db.execute("ALTER TABLE games ADD COLUMN description TEXT")
    for name, ddl in (
        ("sha256", "TEXT"),
        ("scan_status", "TEXT"),
        ("scan_note", "TEXT"),
        ("scanned_at", "TEXT"),
        ("uploaded_by", "TEXT"),
        ("args", "TEXT"),
        ("play_status", "TEXT"),
        ("play_note", "TEXT"),
        ("checked_at", "TEXT"),
        ("excluded_files", "TEXT"),
        ("quarantine_files", "TEXT"),
        ("backend", "TEXT"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE games ADD COLUMN {name} {ddl}")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def game_dir(game_id: str) -> Path:
    return GAMES_DIR / game_id


def files_dir(game_id: str) -> Path:
    return game_dir(game_id) / "files"


def bundle_path(game_id: str) -> Path:
    return game_dir(game_id) / "bundle.jsdos"


def cover_path(game_id: str) -> Path:
    return game_dir(game_id) / "cover.png"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_draft(title: str, total_size: int = 0) -> str:
    game_id = new_id()
    files_dir(game_id).mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO games (id, title, status, created_at, total_size, play_status) "
            "VALUES (?, ?, 'draft', ?, ?, 'pending')",
            (game_id, title, now, total_size),
        )
    return game_id


def update(game_id: str, **fields: Any) -> None:
    allowed = {"title", "exe", "machine", "cycles", "sbtype", "status", "cover",
               "bundle_size", "total_size", "source", "description",
               "sha256", "scan_status", "scan_note", "scanned_at", "uploaded_by", "args",
               "backend", "play_status", "play_note", "checked_at",
               "excluded_files", "quarantine_files"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    assignments = ", ".join(f"{key} = ?" for key in data)
    with connect() as db:
        db.execute(f"UPDATE games SET {assignments} WHERE id = ?", (*data.values(), game_id))


def get(game_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return dict(row) if row else None


def list_games() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM games WHERE status = 'ready' ORDER BY lower(title)"
        ).fetchall()
    return [dict(row) for row in rows]


def popular_games(limit: int = 8) -> list[dict[str, Any]]:
    """Игры для витрины: сначала часто запускаемые, потом новые."""
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM games WHERE status = 'ready' "
            "ORDER BY plays DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def storage_used(username: str) -> int:
    """Сколько байт занял пользователь (архивы + бандлы + сборки)."""
    with connect() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(total_size + bundle_size), 0) FROM games WHERE uploaded_by = ?",
            (username,),
        ).fetchone()
    return int(row[0] or 0)


def record_play(game_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE games SET plays = plays + 1 WHERE id = ?", (game_id,))


def prune_drafts(max_age_hours: int = 48) -> int:
    """Удаляет незаконченные загрузки старше max_age_hours (0 — отключено)."""
    if max_age_hours <= 0:
        return 0
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    removed = 0
    for game in list_drafts():
        try:
            created = datetime.fromisoformat(game["created_at"])
        except (TypeError, ValueError):
            continue
        if created <= cutoff:
            delete(game["id"])
            removed += 1
    return removed


def list_drafts() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM games WHERE status = 'draft' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def sources() -> set[str]:
    """Имена архивов, которые уже импортированы из папки (любые записи)."""
    with connect() as db:
        rows = db.execute("SELECT source FROM games WHERE source IS NOT NULL").fetchall()
    return {row["source"] for row in rows if row["source"]}


def delete(game_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    shutil.rmtree(game_dir(game_id), ignore_errors=True)


def write_meta(game_id: str) -> None:
    """Дублируем метаданные рядом с игрой — удобно для бэкапа и отладки."""
    row = get(game_id)
    if not row:
        return
    (game_dir(game_id) / "meta.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def rebuild_ready(game_id: str) -> None:
    update(game_id, status="ready")
    write_meta(game_id)


# --------------------------------------------------------------------------- #
# Собранные exe
# --------------------------------------------------------------------------- #


def builds_dir(game_id: str) -> Path:
    return game_dir(game_id) / "builds"


def build_file(build_id: str) -> Path | None:
    row = get_build(build_id)
    if not row or row["status"] != "done":
        return None
    path = builds_dir(row["game_id"]) / row["filename"]
    return path if path.is_file() else None


def create_build(game_id: str) -> str:
    build_id = new_id()
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO builds (id, game_id, filename, status, created_at) VALUES (?, ?, '', 'running', ?)",
            (build_id, game_id, now),
        )
    return build_id


def update_build(build_id: str, **fields: Any) -> None:
    allowed = {"filename", "size", "status", "message"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    assignments = ", ".join(f"{key} = ?" for key in data)
    with connect() as db:
        db.execute(f"UPDATE builds SET {assignments} WHERE id = ?", (*data.values(), build_id))


def get_build(build_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
    return dict(row) if row else None


def list_builds(game_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM builds WHERE game_id = ? ORDER BY created_at DESC", (game_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def running_build(game_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM builds WHERE game_id = ? AND status = 'running' ORDER BY created_at DESC LIMIT 1",
            (game_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_build(build_id: str) -> None:
    row = get_build(build_id)
    if row and row["filename"]:
        (builds_dir(row["game_id"]) / row["filename"]).unlink(missing_ok=True)
    with connect() as db:
        db.execute("DELETE FROM builds WHERE id = ?", (build_id,))


def prune_builds(game_id: str, keep: int = 3) -> None:
    """Оставляем только последние N сборок, чтобы не копить по 19 МБ."""
    done = [b for b in list_builds(game_id) if b["status"] == "done"]
    for old in done[keep:]:
        delete_build(old["id"])
