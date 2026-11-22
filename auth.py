#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пользователи, вход, подтверждение почты, CSRF и защита маршрутов."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import uuid
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from typing import Any, Callable

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import library

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_USER, ROLE_ADMIN)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    verified      INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    must_change   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login    TEXT,
    token         TEXT,
    token_expires TEXT,
    plan          TEXT NOT NULL DEFAULT 'free',
    plan_until    TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key        TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    plan        TEXT NOT NULL,
    amount      INTEGER NOT NULL DEFAULT 0,
    provider    TEXT NOT NULL DEFAULT 'manual',
    external_id TEXT,
    status      TEXT NOT NULL DEFAULT 'paid',
    created_at  TEXT NOT NULL
);
"""

# Слабые пароли, которые не пропускаем при регистрации и смене.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty", "qwerty123", "admin", "admin123", "administrator", "letmein",
    "welcome", "welcome1", "iloveyou", "11111111", "00000000", "retro1234",
    "qazwsx", "1q2w3e4r", "abc12345", "12345qwert",
}

_token_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #


def init() -> None:
    with library.connect() as db:
        db.executescript(SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "must_change" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
        if "plan" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        if "plan_until" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN plan_until TEXT")
        db.execute(
            "UPDATE users SET plan = 'pro', plan_until = ? WHERE plan = 'lifetime'",
            ((datetime.now() + timedelta(days=30)).isoformat(timespec="seconds"),),
        )


# --------------------------------------------------------------------------- #
# Подписка
# --------------------------------------------------------------------------- #

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "title": "Гость",
        "price": 0,
        "days": 0,
        "tagline": "Играть онлайн со всеми",
        "features": [
            "Все игры библиотеки онлайн",
            "Свои сохранения в браузере",
            "Без сборки .EXE и .APK",
        ],
    },
    "pro": {
        "title": "Месяц",
        "price": 299,
        "days": 30,
        "period": "месяц",
        "tagline": "Свои игры, .EXE и .APK",
        "features": [
            "Всё из «Гостя»",
            "Загрузка своих игр и обмен ими",
            "Сборка .EXE и .APK для Android",
            "Приоритетная поддержка",
        ],
    },
    "year": {
        "title": "Год",
        "price": 1990,
        "days": 365,
        "period": "год",
        "tagline": "166 ₽ в месяц — выгоднее вдвое",
        "features": [
            "Всё из «Месяца», включая .EXE и .APK",
            "Цена 166 ₽/мес вместо 299 ₽",
            "Подходит, если хостинг оплачивается на год",
        ],
    },
}


def plan_of(user: dict[str, Any] | None) -> str:
    if not user:
        return "free"
    plan = str(user.get("plan") or "free")
    if plan not in PLANS:
        return "free"
    until = user.get("plan_until")
    if plan != "free" and until:
        try:
            expires = datetime.fromisoformat(str(until))
        except ValueError:
            expires = None
        if expires is not None and expires < datetime.now():
            return "free"
    return plan


def subscription_active(user: dict[str, Any] | None) -> bool:
    return plan_of(user) != "free"


def set_plan(username: str, plan: str, days: int = 0) -> str | None:
    """Выдаёт план пользователю. days=0 — бессрочно (кроме free)."""
    plan = plan if plan in PLANS else "free"
    until = None
    if plan != "free" and days <= 0:
        days = int(PLANS[plan]["days"] or 30)
    if plan != "free" and days > 0:
        until = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    with library.connect() as db:
        row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE users SET plan = ?, plan_until = ? WHERE id = ?",
            (plan, until, row["id"]),
        )
    return until


def record_payment(username: str, plan: str, amount: int, provider: str = "manual",
                   external_id: str = "", status: str = "paid") -> None:
    with library.connect() as db:
        db.execute(
            "INSERT INTO payments (id, username, plan, amount, provider, external_id, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:16], username, plan, amount, provider,
             external_id or None, status, datetime.now().isoformat(timespec="seconds")),
        )


def list_payments(username: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with library.connect() as db:
        if username:
            rows = db.execute(
                "SELECT * FROM payments WHERE username = ? ORDER BY created_at DESC LIMIT ?",
                (username, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def count_users() -> int:
    with library.connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def has_admins() -> bool:
    with library.connect() as db:
        row = db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()
    return bool(row and row[0])


def get_user(user_id: str) -> dict[str, Any] | None:
    with library.connect() as db:
        return _row(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_by_login(login: str) -> dict[str, Any] | None:
    value = login.strip().lower()
    with library.connect() as db:
        return _row(
            db.execute(
                "SELECT * FROM users WHERE lower(username) = ? OR lower(email) = ?",
                (value, value),
            ).fetchone()
        )


def list_users() -> list[dict[str, Any]]:
    with library.connect() as db:
        rows = db.execute(
            "SELECT id, username, email, role, verified, active, must_change, created_at, last_login, plan, plan_until "
            "FROM users ORDER BY role DESC, lower(username)"
        ).fetchall()
    return [dict(row) for row in rows]


def create_user(
    username: str,
    email: str,
    password: str,
    role: str = ROLE_USER,
    verified: bool = False,
    must_change: bool = False,
) -> str:
    user_id = secrets.token_hex(8)
    now = datetime.now().isoformat(timespec="seconds")
    with library.connect() as db:
        db.execute(
            "INSERT INTO users (id, username, email, password_hash, role, verified, active, must_change, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                user_id,
                username.strip(),
                email.strip().lower(),
                generate_password_hash(password),
                role if role in ROLES else ROLE_USER,
                1 if verified else 0,
                1 if must_change else 0,
                now,
            ),
        )
    return user_id


def update_user(user_id: str, **fields: Any) -> None:
    allowed = {"username", "email", "role", "verified", "active", "password_hash",
               "last_login", "must_change"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    assignments = ", ".join(f"{key} = ?" for key in data)
    with library.connect() as db:
        db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*data.values(), user_id))


def set_password(user_id: str, password: str, must_change: bool = False) -> None:
    update_user(
        user_id,
        password_hash=generate_password_hash(password),
        must_change=1 if must_change else 0,
    )


def delete_user(user_id: str) -> None:
    with library.connect() as db:
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins(except_id: str | None = None) -> int:
    with library.connect() as db:
        if except_id:
            row = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (except_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# Пароли и вход
# --------------------------------------------------------------------------- #


def verify_password(user: dict[str, Any], password: str) -> bool:
    return check_password_hash(user["password_hash"], password)


def authenticate(login: str, password: str) -> dict[str, Any] | None:
    user = get_by_login(login)
    if not user or not user["active"]:
        return None
    if not verify_password(user, password):
        return None
    return user


# --------------------------------------------------------------------------- #
# Токены подтверждения
# --------------------------------------------------------------------------- #


def issue_token(user_id: str, ttl_minutes: int = 60 * 24) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
    with library.connect() as db:
        db.execute(
            "UPDATE users SET token = ?, token_expires = ? WHERE id = ?",
            (token, expires, user_id),
        )
    return token


def consume_token(token: str) -> str | None:
    """Возвращает id пользователя, если токен валиден и не истёк."""
    if not token:
        return None
    with library.connect() as db:
        row = db.execute("SELECT id, token_expires FROM users WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["token_expires"])
    except (TypeError, ValueError):
        return None
    if expires < datetime.now():
        return None
    return row["id"]


def clear_token(user_id: str) -> None:
    with library.connect() as db:
        db.execute("UPDATE users SET token = NULL, token_expires = NULL WHERE id = ?", (user_id,))


# --------------------------------------------------------------------------- #
# Почта
# --------------------------------------------------------------------------- #


def send_email(smtp: dict[str, Any], to: str, subject: str, body: str) -> tuple[bool, str]:
    """Отправка письма. Возвращает (успех, текст ошибки)."""
    host = (smtp.get("host") or "").strip()
    if not host:
        return False, "SMTP не настроен"

    sender = (smtp.get("from") or smtp.get("user") or "").strip() or f"retro-web@{host}"
    try:
        port = int(smtp.get("port") or 587)
    except (TypeError, ValueError):
        port = 587

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if smtp.get("ssl"):
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if smtp.get("tls") and not smtp.get("ssl"):
                server.starttls()
            user = (smtp.get("user") or "").strip()
            if user:
                server.login(user, smtp.get("password") or "")
            server.send_message(message)
        return True, ""
    except Exception as exc:  # SMTP-ошибки разнородны
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Ограничение попыток
# --------------------------------------------------------------------------- #


def allow_attempt(key: str, limit: int = 8, window_seconds: int = 600) -> bool:
    """Не больше limit попыток за window_seconds для ключа (IP + действие)."""
    now = time.time()
    with _token_lock:
        with library.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE created_at < ?", (now - window_seconds,))
            count = db.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE key = ?", (key,)
            ).fetchone()[0]
            if count >= limit:
                return False
            db.execute("INSERT INTO login_attempts (key, created_at) VALUES (?, ?)", (key, now))
    return True


def client_key(action: str) -> str:
    address = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
    return f"{action}:{address.split(',')[0].strip()}"


# --------------------------------------------------------------------------- #
# Сессия и CSRF
# --------------------------------------------------------------------------- #


def login_user(user: dict[str, Any]) -> None:
    session.clear()
    session["uid"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(24)
    session.permanent = True
    update_user(user["id"], last_login=datetime.now().isoformat(timespec="seconds"))


def logout_user() -> None:
    session.clear()


def current_user() -> dict[str, Any] | None:
    if "user" in g:
        return g.user
    user_id = session.get("uid")
    user = get_user(user_id) if user_id else None
    if user and not user["active"]:
        user = None
    g.user = user
    return user


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def valid_csrf(submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


# --------------------------------------------------------------------------- #
# Декораторы
# --------------------------------------------------------------------------- #


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            flash("Сначала войдите в систему.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = getattr(g, "user", None) or current_user()
        if user is None:
            flash("Сначала войдите в систему.", "error")
            return redirect(url_for("login", next=request.path))
        if user["role"] != ROLE_ADMIN:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #


def validate_password(password: str, username: str = "", email: str = "") -> list[str]:
    """Политика паролей: длина, состав, отсев типовых слабых значений."""
    errors: list[str] = []
    value = password or ""
    if len(value) < 10:
        errors.append("Пароль должен быть не короче 10 символов.")
    if value.lower() in COMMON_PASSWORDS:
        errors.append("Слишком простой пароль — выберите другой.")
    if username and value.lower() == username.lower():
        errors.append("Пароль не должен совпадать с именем пользователя.")
    if email and value.lower() == email.lower():
        errors.append("Пароль не должен совпадать с e-mail.")
    if value and not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        errors.append("В пароле нужны и буквы, и цифры.")
    return errors


def validate_registration(
    username: str, email: str, password: str, password2: str
) -> list[str]:
    errors: list[str] = []
    if not USERNAME_RE.match(username or ""):
        errors.append("Имя: 3–24 символа, латиница/цифры, точка, дефис или подчёркивание.")
    if not EMAIL_RE.match(email or ""):
        errors.append("Введите корректный e-mail.")
    errors.extend(validate_password(password, username, email))
    if password != password2:
        errors.append("Пароли не совпадают.")
    return errors


# --------------------------------------------------------------------------- #
# Гарантированный доступ администратора
# --------------------------------------------------------------------------- #

def generate_password(length: int = 16) -> str:
    """Случайный пароль, который заведомо проходит политику (буквы + цифры)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    base = "".join(secrets.choice(alphabet) for _ in range(max(length, 12)))
    return base + str(secrets.randbelow(90) + 10)


ADMIN_EMAIL_DOMAIN = "retro.local"


def ensure_admin(
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Гарантирует встроенного администратора.

    - пользователей нет → создаёт админа; если пароль не задан, генерирует
      случайный и требует смену при первом входе;
    - админ с таким именем есть и передан пароль → сбрасывает пароль и права;
    - иначе ничего не делает.

    Возвращает {"username", "password", "created", "reset", "generated"}.
    """
    username = (username or os.environ.get("RETRO_ADMIN_USER") or "admin").strip()
    explicit = password or os.environ.get("RETRO_ADMIN_PASSWORD")
    info: dict[str, Any] = {
        "username": username,
        "password": None,
        "created": False,
        "reset": False,
        "generated": False,
    }

    existing = get_by_login(username)
    if existing:
        if explicit:
            set_password(existing["id"], explicit, must_change=False)
            update_user(existing["id"], role=ROLE_ADMIN, verified=1, active=1)
            info.update(password=explicit, reset=True)
        return info

    if count_users() > 0:
        return info

    generated = not explicit
    password = explicit or generate_password(20)
    create_user(
        username,
        f"{username}@{ADMIN_EMAIL_DOMAIN}",
        password,
        role=ROLE_ADMIN,
        verified=True,
        must_change=generated,
    )
    info.update(password=password, created=True, generated=generated)
    return info
