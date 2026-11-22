#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retro Web — библиотека DOS-игр, которые запускаются прямо в браузере.

Запуск:  python app.py
Открыть: http://127.0.0.1:5000
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import shutil
import socket
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

import android
import applog
import auth
import builder
import bundler
import library
import probe
import server_settings
import site_settings
import scanner

BASE_DIR = Path(__file__).resolve().parent

ALLOWED_ARCHIVES = {".zip", ".rar", ".7z", ".jsdos"}
ALLOWED_COVERS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
GAME_ID_RE = re.compile(r"^[0-9a-f]{12}$")

PUBLIC_ENDPOINTS = {"index", "about_page", "apps_page", "player_apk",
                   "manifest", "service_worker", "pricing", "login", "register", "verify", "healthz", "static", "maintenance", "client_error", "billing_webhook"}
APP_BUILD = "2026-11-22.0730"

# Простой режим: без входа, тарифов и подписок —
# просто библиотека игр и сборка .EXE/.APK.
SIMPLE_MODE = True
ADMIN_PATHS = ("/admin", "/settings")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = bundler.MAX_ARCHIVE_BYTES
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.secret_key = os.environ.get("RETRO_SECRET", "retro-web-local-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RETRO_HTTPS")),
    MAX_CONTENT_LENGTH=bundler.MAX_ARCHIVE_BYTES + 8 * 1024 * 1024,
)
app.jinja_env.filters["human_size"] = bundler.human_size
# Подстраховка: csrf_token доступен шаблонам даже без context processor.
app.jinja_env.globals["csrf_token"] = auth.csrf_token


# --------------------------------------------------------------------------- #
# Инициализация
# --------------------------------------------------------------------------- #


def setup_paths() -> None:
    data_dir = os.environ.get("RETRO_DATA_DIR")
    if data_dir:
        library.configure(Path(data_dir))
    library.init()
    auth.init()
    applog.setup(library.DATA_DIR)
    upload_tmp()


def upload_tmp() -> Path:
    """Временная папка для загрузок — рядом с библиотекой, а не рядом с кодом."""
    path = library.DATA_DIR / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lan_ip() -> str:
    """Адрес в локальной сети — чтобы открыть библиотеку с другого устройства."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_is_busy(host: str, port: int) -> bool:
    """Проверяем, слушает ли уже кто-то этот порт (на Windows SO_REUSEADDR коварен)."""
    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.6)
            return probe.connect_ex((target, port)) == 0
    except OSError:
        return False


def _prompt_password(prompt: str) -> str:
    """Пароль без эха в терминале; при перенаправленном вводе читаем строку."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("нет ввода для пароля")
    print(prompt + "(ввод из stdin)")
    return line.rstrip("\r\n")


def games_folder() -> Path:
    """Папка с архивами, откуда можно импортировать игры (по умолчанию ./games)."""
    override = os.environ.get("RETRO_GAMES_DIR")
    return Path(override).resolve() if override else (BASE_DIR / "games").resolve()


SCAN_BLOCKING = ("infected", "blocked")


def _apply_scan(game_id: str, archive: Path | None, uploader: str | None,
                allow_windows: bool, scan_enabled: bool = True) -> "scanner.ScanResult":
    """Сканирует файлы игры и записывает результат в карточку."""
    files_dir = library.files_dir(game_id)
    if scan_enabled:
        result = scanner.scan_game(
            archive,
            files_dir if files_dir.is_dir() else None,
            allow_windows=allow_windows,
        )
    else:
        result = scanner.ScanResult("unchecked", "проверка отключена администратором")
    digest = scanner.sha256_file(archive) if archive and archive.is_file() else ""
    if files_dir.is_dir():
        digest = scanner.sha256_tree(files_dir)
    library.update(
        game_id,
        sha256=digest or None,
        scan_status=result.status,
        scan_note=result.note[:1000] if result.note else None,
        scanned_at=datetime.now().isoformat(timespec="seconds"),
        uploaded_by=uploader,
    )
    applog.log(
        f"SCAN game={game_id} status={result.status} engine={result.engine} "
        f"user={uploader or '-'} note={result.note[:200]!r}",
        logging.WARNING if result.status in SCAN_BLOCKING else logging.INFO,
    )
    return result


def _import_archive(archive: Path, title: str, suffix: str, source: str | None = None,
                    uploader: str | None = None, allow_windows: bool = False,
                    scan_enabled: bool = True, quarantine: bool = True) -> str:
    """Распаковывает архив, проверяет безопасность и возвращает id игры."""
    game_id = library.create_draft(title)
    try:
        if suffix == ".jsdos":
            shutil.copy2(archive, library.bundle_path(game_id))
            excluded = (scanner.sanitize_bundle(
                library.bundle_path(game_id),
                library.game_dir(game_id) / "quarantine",
            ) if quarantine else [])
            library.update(
                game_id,
                status="ready",
                bundle_size=library.bundle_path(game_id).stat().st_size,
                source=source,
            )
            if scan_enabled:
                result = scanner.scan_path(library.bundle_path(game_id))
            else:
                result = scanner.ScanResult("unchecked", "проверка отключена администратором")
            library.update(
                game_id,
                sha256=scanner.sha256_file(library.bundle_path(game_id)),
                scan_status=result.status,
                scan_note=result.note[:1000] or None,
                scanned_at=datetime.now().isoformat(timespec="seconds"),
                uploaded_by=uploader,
            )
            if result.status in SCAN_BLOCKING:
                raise bundler.ArchiveError(
                    f"Загрузка заблокирована проверкой безопасности: {result.note}"
                )
            if excluded:
                library.update(
                    game_id,
                    excluded_files=", ".join(excluded)[:500],
                    quarantine_files="quarantine",
                )
            library.write_meta(game_id)
            return game_id

        bundler.extract_archive(archive, library.files_dir(game_id))
        excluded = (scanner.quarantine_files(
            library.files_dir(game_id),
            library.game_dir(game_id) / "quarantine",
        ) if quarantine else [])
    except bundler.ArchiveError:
        library.delete(game_id)
        raise

    result = bundler.import_game_dir(library.files_dir(game_id))
    if not result.candidates:
        library.delete(game_id)
        raise bundler.ArchiveError(
            "В архиве не нашлось ни одного .EXE/.COM/.BAT — похоже, это не папка с игрой."
        )

    scan = _apply_scan(game_id, archive, uploader, allow_windows, scan_enabled)
    if scan.status in SCAN_BLOCKING:
        library.delete(game_id)
        raise bundler.ArchiveError(
            f"Загрузка отклонена проверкой безопасности: {scan.note}"
        )

    library.update(
        game_id,
        total_size=result.total_size,
        exe=bundler.default_candidate(result.candidates),
        source=source,
        excluded_files=(", ".join(excluded)[:500] if excluded else None),
        quarantine_files=("quarantine" if excluded else None),
    )
    return game_id


# --------------------------------------------------------------------------- #
# Доступ: вход, CSRF, обслуживание
# --------------------------------------------------------------------------- #


@app.before_request
def load_context():
    g.site = site_settings.load()
    g.user = auth.current_user()
    if SIMPLE_MODE:
        # без авторизации: локально — админ, снаружи — гость
        local = request.remote_addr in ("127.0.0.1", "::1")
        g.user = {
            "id": 0,
            "username": "local" if local else "guest",
            "email": "",
            "role": auth.ROLE_ADMIN if local else "user",
            "plan": "year",
            "plan_until": None,
            "must_change": 0,
        }
        if request.endpoint in {"login", "register", "verify", "logout", "profile",
                                "pricing", "billing_checkout", "billing_webhook"}:
            return redirect(url_for("index"))
    g.plan = auth.plan_of(g.user)
    g.plan_title = auth.PLANS.get(g.plan, {}).get("title", g.plan)
    g.theme_title = site_settings.THEMES.get(g.site.get("theme") or "classic", "Famicom")
    g.subscribed = True if SIMPLE_MODE else auth.subscription_active(g.user)

    # Телеметрия и счётчик запусков приходят без формы и без токена.
    if request.method == "POST" and request.endpoint not in ("client_error", "api_play", "billing_webhook"):
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not auth.valid_csrf(token):
            if request.endpoint in PUBLIC_ENDPOINTS:
                flash("Сессия устарела — обновите страницу и попробуйте снова.", "error")
                return redirect(request.referrer or url_for("login"))
            abort(400, "Неверный CSRF-токен")

    if g.site.get("maintenance") and (g.user is None or g.user["role"] != auth.ROLE_ADMIN):
        if request.endpoint not in ("login", "static", "healthz", "maintenance"):
            return render_template("maintenance.html", site=g.site), 503


@app.before_request
def guard_access():
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if g.user is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "требуется вход"}), 401
        return redirect(url_for("login", next=request.path))

    # Пароль, выданный системой, обязательно меняем при первом входе.
    if g.user.get("must_change") and request.endpoint not in (
        "profile", "logout", "static", "maintenance"
    ):
        flash("Смените выданный системой пароль, чтобы продолжить.", "error")
        return redirect(url_for("profile"))

    if any(request.path.startswith(prefix) for prefix in ADMIN_PATHS):
        if g.user["role"] != auth.ROLE_ADMIN:
            abort(403)
    return None


def rebuild_bundle(game_id: str, keep_config: bool = True) -> bool:
    """Пересобирает бандл игры из её файлов, сохраняя старый dosbox.conf."""
    game = library.get(game_id)
    if not game:
        return False
    files = library.files_dir(game_id)
    if not files.is_dir():
        return False
    target = library.bundle_path(game_id)
    config_text = ""
    if keep_config and target.is_file():
        try:
            with zipfile.ZipFile(target) as zf:
                config_text = zf.read(".jsdos/dosbox.conf").decode("utf-8", "replace")
        except (KeyError, OSError, zipfile.BadZipFile):
            config_text = ""
    if not config_text.strip():
        exe = game.get("exe") or ""
        if not exe:
            return False
        config_text = bundler.build_config(
            game.get("title") or "DOS game",
            Path(exe).as_posix(),
            game.get("machine") or "svga_s3",
            str(game.get("cycles") or "3000"),
            game.get("sbtype") or "sb16",
            args=game.get("args") or "",
        )
    size = bundler.make_bundle(files, target, config_text)
    library.update(game_id, bundle_size=size)
    applog.log(f"BUNDLE rebuilt game={game_id} bytes={size}", logging.WARNING)
    return True


def bundle_url_for(game: dict) -> str:
    """Ссылка на бандл с версией в пути (js-dos кэширует по URL)."""
    path = library.bundle_path(game["id"])
    stamp = int(path.stat().st_mtime) if path.exists() else 0
    build_tag = "".join(ch for ch in APP_BUILD if ch.isdigit())[-6:]
    version = f"{stamp}-{build_tag}-{secrets.token_hex(2)}"
    return url_for("bundle_file", game_id=game["id"], version=version)


@app.context_processor
def inject_globals():
    current = g.get("user")
    return {
        "site": g.get("site") or site_settings.load(),
        "user": current,
        "csrf_token": auth.csrf_token,
        "is_admin": bool(current and current["role"] == auth.ROLE_ADMIN),
        "bundle_url": bundle_url_for,
        "build": APP_BUILD,
        "simple_mode": SIMPLE_MODE,
        "year": datetime.now().year,
    }


# --------------------------------------------------------------------------- #
# Вход, регистрация, подтверждение
# --------------------------------------------------------------------------- #


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    next_url = request.args.get("next") or request.form.get("next") or ""

    if request.method == "POST":
        login_value = (request.form.get("login") or "").strip()
        password = request.form.get("password") or ""
        if not auth.allow_attempt(auth.client_key("login"), limit=10, window_seconds=600):
            flash("Слишком много попыток входа. Подождите несколько минут.", "error")
            return render_template("login.html", next_url=next_url), 429

        user = auth.authenticate(login_value, password)
        if not user:
            applog.log(f"LOGIN FAIL login={login_value!r} ip={request.remote_addr}", logging.WARNING)
            flash("Неверное имя или пароль.", "error")
            return render_template("login.html", next_url=next_url), 401

        if g.site.get("require_email") and not user["verified"]:
            applog.log(f"LOGIN unverified user={user['username']}", logging.WARNING)
            flash("Почта не подтверждена — проверьте письмо или попросите администратора.", "error")
            return render_template("login.html", next_url=next_url), 403

        auth.login_user(user)
        applog.log(f"LOGIN ok user={user['username']} role={user['role']} ip={request.remote_addr}")
        if user.get("must_change"):
            flash("Пароль выдан системой — смените его при первом входе.", "error")
            return redirect(url_for("profile"))
        target = next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("index")
        return redirect(target)

    return render_template("login.html", next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))

    if not g.site["registration_open"]:
        return render_template("register.html", closed=True), 403

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        errors = auth.validate_registration(username, email, password, password2)
        if not auth.allow_attempt(auth.client_key("register"), limit=6, window_seconds=900):
            errors.append("Слишком много попыток регистрации. Попробуйте позже.")
        if auth.get_by_login(username) or auth.get_by_login(email):
            errors.append("Пользователь с таким именем или e-mail уже существует.")

        if errors:
            return render_template("register.html", errors=errors, form=request.form), 400

        user_id = auth.create_user(
            username,
            email,
            password,
            role=auth.ROLE_USER,
            verified=not g.site["require_email"],
        )
        applog.log(f"REGISTER user={username} email={email}")

        if not g.site["require_email"]:
            flash("Регистрация завершена, можно входить.", "ok")
            return redirect(url_for("login"))

        token = auth.issue_token(user_id)
        link = url_for("verify", token=token, _external=True)
        ok, error = auth.send_email(
            g.site["smtp"],
            email,
            f"{g.site['title']}: подтверждение регистрации",
            f"Привет, {username}!\n\nПодтвердите регистрацию по ссылке:\n{link}\n\n"
            "Если вы не регистрировались — просто проигнорируйте письмо.",
        )
        if ok:
            applog.log(f"REGISTER mail sent to {email}")
            return render_template("verify_pending.html", link=None, email=email)
        applog.log(f"REGISTER mail failed to {email}: {error}", logging.WARNING)
        return render_template("verify_pending.html", link=link, email=email, error=error)

    return render_template("register.html")


@app.get("/verify/<token>")
def verify(token: str):
    user_id = auth.consume_token(token)
    if not user_id:
        flash("Ссылка подтверждения недействительна или устарела.", "error")
        return redirect(url_for("login"))
    auth.update_user(user_id, verified=1)
    auth.clear_token(user_id)
    user = auth.get_user(user_id)
    applog.log(f"VERIFY user={user['username'] if user else user_id}")
    flash("Почта подтверждена — можно входить.", "ok")
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        current = request.form.get("current") or ""
        new = request.form.get("password") or ""
        new2 = request.form.get("password2") or ""
        errors: list[str] = []
        if not auth.verify_password(g.user, current):
            errors.append("Текущий пароль неверен.")
        errors.extend(auth.validate_password(new, g.user["username"], g.user["email"]))
        if new != new2:
            errors.append("Новые пароли не совпадают.")
        if errors:
            return render_template("profile.html", errors=errors), 400

        auth.set_password(g.user["id"], new, must_change=False)
        applog.log(f"PASSWORD changed user={g.user['username']}")
        flash("Пароль изменён. Теперь доступны все разделы.", "ok")
        return redirect(url_for("index"))

    return render_template("profile.html")


@app.post("/logout")
def logout():
    login_name = g.user["username"] if g.user else "?"
    auth.logout_user()
    applog.log(f"LOGOUT user={login_name}")
    flash("Вы вышли из системы.", "ok")
    return redirect(url_for("login"))


@app.get("/maintenance")
def maintenance():
    return render_template("maintenance.html", site=g.site)


# --------------------------------------------------------------------------- #
# Админка
# --------------------------------------------------------------------------- #


@app.get("/admin")
def admin_dashboard():
    games = library.list_games()
    builds = sum(len(library.list_builds(game["id"])) for game in games)
    total_bytes = 0
    for game in games:
        total_bytes += game.get("bundle_size") or 0
        builds_dir = library.builds_dir(game["id"])
        if builds_dir.is_dir():
            total_bytes += sum(f.stat().st_size for f in builds_dir.glob("*.exe") if f.is_file())

    stats = {
        "users": len(auth.list_users()),
        "admins": auth.count_admins(),
        "games": len(games),
        "drafts": len(library.list_drafts()),
        "builds": builds,
        "storage": bundler.human_size(total_bytes),
        "log_size": applog.size_human(),
    }
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        smtp_configured=site_settings.smtp_configured(),
    )


@app.get("/admin/users")
def admin_users():
    return render_template("admin/users.html", users=auth.list_users(), plans=auth.PLANS)


@app.post("/admin/users/create")
def admin_user_create():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = (request.form.get("password") or "").strip() or auth.generate_password(14)
    role = request.form.get("role") or auth.ROLE_USER

    errors = auth.validate_registration(username, email, password, password)
    if auth.get_by_login(username) or auth.get_by_login(email):
        errors.append("Такое имя или e-mail уже заняты.")
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("admin_users"))

    auth.create_user(username, email, password, role=role, verified=True)
    applog.log(f"ADMIN created user={username} role={role} by={g.user['username']}")
    flash(f"Пользователь {username} создан, пароль: {password}", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/delete")
def admin_user_delete(user_id: str):
    target = auth.get_user(user_id)
    if not target:
        abort(404)
    if target["role"] == auth.ROLE_ADMIN and auth.count_admins(except_id=user_id) == 0:
        flash("Нельзя удалить последнего администратора.", "error")
        return redirect(url_for("admin_users"))
    if target["id"] == g.user["id"]:
        flash("Нельзя удалить самого себя.", "error")
        return redirect(url_for("admin_users"))
    auth.delete_user(user_id)
    applog.log(f"ADMIN deleted user={target['username']} by={g.user['username']}", logging.WARNING)
    flash(f"Пользователь {target['username']} удалён.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/role")
def admin_user_role(user_id: str):
    target = auth.get_user(user_id)
    if not target:
        abort(404)
    role = request.form.get("role") or auth.ROLE_USER
    if role not in auth.ROLES:
        role = auth.ROLE_USER
    if target["role"] == auth.ROLE_ADMIN and role != auth.ROLE_ADMIN and auth.count_admins(except_id=user_id) == 0:
        flash("Нельзя снять роль с последнего администратора.", "error")
        return redirect(url_for("admin_users"))
    auth.update_user(user_id, role=role)
    applog.log(f"ADMIN role user={target['username']} -> {role} by={g.user['username']}")
    flash("Роль обновлена.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/active")
def admin_user_active(user_id: str):
    target = auth.get_user(user_id)
    if not target:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("Нельзя заблокировать самого себя.", "error")
        return redirect(url_for("admin_users"))
    active = 0 if target["active"] else 1
    if not active and target["role"] == auth.ROLE_ADMIN and auth.count_admins(except_id=user_id) == 0:
        flash("Нельзя заблокировать последнего администратора.", "error")
        return redirect(url_for("admin_users"))
    auth.update_user(user_id, active=active)
    applog.log(f"ADMIN active user={target['username']} -> {active} by={g.user['username']}")
    flash("Доступ обновлён.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/verify")
def admin_user_verify(user_id: str):
    target = auth.get_user(user_id)
    if not target:
        abort(404)
    auth.update_user(user_id, verified=1)
    auth.clear_token(user_id)
    applog.log(f"ADMIN verified user={target['username']} by={g.user['username']}")
    flash("Почта отмечена как подтверждённая.", "ok")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<user_id>/password")
def admin_user_password(user_id: str):
    target = auth.get_user(user_id)
    if not target:
        abort(404)
    password = (request.form.get("password") or "").strip() or auth.generate_password(14)
    errors = auth.validate_password(password, target["username"], target["email"])
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("admin_users"))
    auth.set_password(user_id, password, must_change=True)
    applog.log(f"ADMIN reset password user={target['username']} by={g.user['username']}", logging.WARNING)
    flash(f"Новый пароль для {target['username']}: {password} (сменится при первом входе)", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/site", methods=["GET", "POST"])
def admin_site():
    if request.method == "POST":
        site_settings.save(request.form)
        applog.log(f"ADMIN site settings by={g.user['username']}")
        flash("Настройки сайта сохранены.", "ok")
        return redirect(url_for("admin_site"))
    return render_template(
        "admin/site.html",
        settings=site_settings.load(),
        themes=site_settings.THEMES,
        smtp_configured=site_settings.smtp_configured(),
    )


@app.get("/admin/logs")
def admin_logs():
    filter_text = (request.args.get("q") or "").strip()
    lines = applog.tail(600)
    if filter_text:
        lines = [line for line in lines if filter_text.lower() in line.lower()]
    return render_template("admin/logs.html", lines=lines, query=filter_text, log_size=applog.size_human())


# --------------------------------------------------------------------------- #
# Библиотека
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return render_template(
        "index.html",
        plans=_plans(),
        plan=g.plan,
        games_count=len(library.list_games()),
        users_count=auth.count_users(),
    )


@app.get("/library")
def library_page():
    games = library.list_games()
    admin = bool(g.user and g.user["role"] == auth.ROLE_ADMIN)
    if not admin:
        # игры без вердикта проверки не публикуем
        games = [g for g in games if (g.get("play_status") or "ok") != "pending"]
        popular = [g for g in library.popular_games(8)
                   if (g.get("play_status") or "ok") != "pending"]
    else:
        popular = library.popular_games(8)
    return render_template(
        "library.html",
        games=games,
        popular=popular,
        cartridges=games[:6],
        drafts=library.list_drafts(),
    )


@app.get("/about")
def about_page():
    return render_template("about.html", plans=_plans(), plan=g.plan)


def _public_base() -> str:
    value = (g.site.get("public_url") or "").strip()
    return value.rstrip("/") if value else request.url_root.rstrip("/")


@app.get("/manifest.webmanifest")
def manifest():
    response = send_from_directory(app.static_folder, "manifest.webmanifest",
                                   mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js",
                                   mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


def _apk_cache_path(name: str, url: str) -> Path:
    digest = hashlib.sha256((url + APP_BUILD).encode("utf-8")).hexdigest()[:12]
    folder = library.DATA_DIR / "android" / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}-{digest}.apk"


@app.get("/apps")
def apps_page():
    return render_template(
        "apps.html",
        android_ready=android.available(),
        base_url=_public_base(),
        public_url=(g.site.get("public_url") or "").strip(),
    )


@app.get("/apps/player.apk")
def player_apk():
    if not android.available():
        abort(503, "Сборка APK недоступна: на сервере нет Android SDK.")
    target = _apk_cache_path("player", _public_base() + "/library")
    if not target.is_file():
        android.build(_public_base() + "/library", target)
    applog.log(f"APK player downloaded ip={request.remote_addr}")
    return send_file(target, as_attachment=True, download_name="RetroGames-Player.apk",
                     mimetype="application/vnd.android.package-archive")


@app.post("/games/<game_id>/rebuild-bundle")
@auth.admin_required
def rebuild_bundle_route(game_id: str):
    ok = rebuild_bundle(game_id)
    flash("Бандл пересобран." if ok else "Не удалось пересобрать: нет файлов игры.",
          "ok" if ok else "error")
    return redirect(url_for("game_page", game_id=game_id))


@app.get("/games/<game_id>/apk")
def game_apk(game_id: str):
    game = _load(game_id)
    _ensure_playable(game)
    if not android.available():
        abort(503, "Сборка APK недоступна: на сервере нет Android SDK.")
    url = _public_base() + url_for("play", game_id=game_id)
    target = _apk_cache_path(f"game-{game_id}", url)
    if not target.is_file():
        android.build(url, target)
    applog.log(f"APK game={game_id} user={g.user['username']}")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", game.get("title") or game_id).strip("-")[:40] or game_id
    return send_file(target, as_attachment=True, download_name=f"{safe}.apk",
                     mimetype="application/vnd.android.package-archive")


def _plans() -> dict:
    """Тарифы: значения по умолчанию + правки из админки."""
    plans = {key: dict(value) for key, value in auth.PLANS.items()}
    pricing = g.site.get("pricing") or {}
    overrides = pricing.get("plans") or {}
    for key, patch in overrides.items():
        if key not in plans or not isinstance(patch, dict):
            continue
        target = plans[key]
        if patch.get("title"):
            target["title"] = patch["title"]
        if patch.get("period"):
            target["period"] = patch["period"]
        if patch.get("tagline"):
            target["tagline"] = patch["tagline"]
        if isinstance(patch.get("price"), (int, float)):
            target["price"] = max(0, int(patch["price"]))
        if patch.get("features"):
            target["features"] = list(patch["features"])
    return plans


@app.get("/pricing")
def pricing():
    billing = g.site["billing"]
    return render_template(
        "pricing.html",
        plans=_plans(),
        page=g.site.get("pricing") or {},
        plan=g.plan,
        payments=auth.list_payments(g.user["username"]) if g.user else [],
        demo=billing["mode"] == "demo",
        billing=billing,
    )


@app.post("/billing/checkout")
def billing_checkout():
    if not g.user:
        return redirect(url_for("login", next=url_for("pricing")))
    plan = (request.form.get("plan") or "").strip()
    plans = _plans()
    if plan not in plans or plan == "free":
        flash("Выберите тариф.", "error")
        return redirect(url_for("pricing"))

    info = plans[plan]
    billing = g.site["billing"]
    demo = billing["mode"] == "demo"
    auth.record_payment(g.user["username"], plan, int(info["price"]),
                        provider="demo" if demo else (billing.get("provider") or "manual"),
                        status="paid" if demo else "pending")
    if demo:
        auth.set_plan(g.user["username"], plan, int(info["days"]))
        applog.log(f"BILLING demo activate user={g.user['username']} plan={plan}", logging.WARNING)
        flash(f"Тариф «{info['title']}» активирован (демо-режим).", "ok")
        return redirect(url_for("pricing"))

    applog.log(f"BILLING order user={g.user['username']} plan={plan} amount={info['price']}", logging.WARNING)
    flash(billing.get("price_note") or "Заявка на оплату создана, администратор свяжется с вами.", "ok")
    return redirect(url_for("pricing"))


@app.post("/billing/webhook")
def billing_webhook():
    """Приём подтверждения оплаты от провайдера (ЮKassa/CloudPayments и др.)."""
    secret = (g.site["billing"].get("webhook_secret") or os.environ.get("RETRO_BILLING_SECRET", ""))
    if not secret or request.headers.get("X-Billing-Secret") != secret:
        abort(403)
    payload = request.get_json(silent=True) or {}
    login = (payload.get("login") or payload.get("username") or "").strip()
    plan = (payload.get("plan") or "pro").strip()
    amount = int(payload.get("amount") or 0)
    if plan not in auth.PLANS or plan == "free" or not login:
        abort(400)
    user = auth.get_by_login(login)
    if not user:
        abort(404)
    days = int(payload.get("days") or auth.PLANS[plan]["days"] or 0)
    auth.set_plan(user["username"], plan, days)
    auth.record_payment(user["username"], plan, amount,
                        provider=str(payload.get("provider") or "webhook"),
                        external_id=str(payload.get("external_id") or ""))
    applog.log(f"BILLING paid user={user['username']} plan={plan} amount={amount}", logging.WARNING)
    return {"ok": True}


@app.get("/admin/landing")
@auth.admin_required
def admin_landing():
    return render_template("admin/landing.html", landing=g.site["landing"])


@app.post("/admin/landing")
@auth.admin_required
def admin_landing_save():
    site_settings.save_landing(request.form)
    applog.log(f"ADMIN landing updated by={g.user['username']}", logging.WARNING)
    flash("Тексты главной сохранены.", "ok")
    return redirect(url_for("admin_landing"))


@app.get("/admin/pricing")
@auth.admin_required
def admin_pricing():
    return render_template("admin/pricing.html", page=g.site.get("pricing") or {},
                           plans=_plans())


@app.post("/admin/pricing")
@auth.admin_required
def admin_pricing_save():
    site_settings.save_pricing(request.form)
    flash("Страница тарифов сохранена.", "ok")
    return redirect(url_for("admin_pricing"))


@app.get("/admin/billing")
@auth.admin_required
def admin_billing():
    return render_template("admin/billing.html", billing=g.site["billing"],
                           plans=auth.PLANS)


@app.post("/admin/billing")
@auth.admin_required
def admin_billing_save():
    site_settings.save_billing(request.form)
    applog.log(f"ADMIN billing updated by={g.user['username']}", logging.WARNING)
    flash("Настройки оплаты сохранены.", "ok")
    return redirect(url_for("admin_billing"))


@app.post("/admin/users/<username>/plan")
@auth.admin_required
def admin_set_plan(username: str):
    plan = request.form.get("plan") or "free"
    if plan not in auth.PLANS:
        plan = "free"
    days = int(auth.PLANS[plan]["days"] or 0)
    until = auth.set_plan(username, plan, days)
    applog.log(f"ADMIN plan user={username} plan={plan} until={until} by={g.user['username']}", logging.WARNING)
    flash(f"Тариф пользователя {username}: {auth.PLANS[plan]['title']}.", "ok")
    return redirect(url_for("admin_users"))


@app.get("/upload")
def upload_page():
    return render_template("upload.html", max_mb=bundler.MAX_ARCHIVE_BYTES // (1024 * 1024))


@app.post("/upload")
def upload():
    archive = request.files.get("archive")
    if archive is None or not archive.filename:
        flash("Выберите файл архива с игрой.", "error")
        return redirect(url_for("upload_page"))

    suffix = Path(archive.filename).suffix.lower()
    if suffix not in ALLOWED_ARCHIVES:
        flash("Поддерживаются архивы ZIP, RAR, 7Z и готовые бандлы .jsdos.", "error")
        return redirect(url_for("upload_page"))

    title = (request.form.get("title") or "").strip()
    if not title:
        title = Path(archive.filename).stem.replace("_", " ").replace("-", " ").strip() or "Без названия"

    is_admin = g.user["role"] == auth.ROLE_ADMIN
    if not is_admin and not auth.allow_attempt(auth.client_key("upload"), limit=12, window_seconds=3600):
        flash("Слишком много загрузок за час — попробуйте позже.", "error")
        return redirect(url_for("upload_page"))

    if not is_admin:
        quota_gb = float(os.environ.get("RETRO_QUOTA_GB", "10") or 10)
        used = library.storage_used(g.user["username"])
        if used > quota_gb * 1024 ** 3:
            flash(f"Квота {quota_gb:g} ГБ исчерпана: удалите старые игры.", "error")
            return redirect(url_for("upload_page"))

    tmp_path = upload_tmp() / f"{secrets.token_hex(8)}{suffix}"
    archive.save(tmp_path)
    try:
        game_id = _import_archive(
            tmp_path, title, suffix,
            uploader=g.user["username"],
            allow_windows=bool(g.site.get("allow_windows")),
            scan_enabled=bool(g.site.get("scan_uploads", True)),
            quarantine=bool(g.site.get("quarantine_uploads", True)),
        )
    except bundler.ArchiveError as exc:
        applog.log(f"UPLOAD fail file={archive.filename!r} user={g.user['username']}: {exc}", logging.WARNING)
        flash(str(exc), "error")
        return redirect(url_for("upload_page"))
    finally:
        tmp_path.unlink(missing_ok=True)

    applog.log(f"UPLOAD file={archive.filename!r} game={game_id} user={g.user['username']}")

    game = library.get(game_id) or {}
    scan_status = game.get("scan_status")
    if scan_status == "clean":
        flash("Проверка безопасности пройдена: угроз не найдено.", "ok")
    elif scan_status == "unchecked":
        flash("Антивирус на сервере не найден — проверена только политика файлов.", "error")
    elif scan_status == "error":
        flash(f"Антивирус не смог проверить архив: {game.get('scan_note') or 'ошибка'}", "error")
    if game.get("excluded_files"):
        flash("В карантин убраны опасные файлы: " + game["excluded_files"], "error")
    if game.get("status") == "ready":
        return redirect(url_for("play", game_id=game_id))
    return redirect(url_for("setup_game", game_id=game_id))


# --------------------------------------------------------------------------- #
# Импорт из папки games/
# --------------------------------------------------------------------------- #


def _scan_games_folder() -> list[dict]:
    folder = games_folder()
    imported = library.sources()
    items: list[dict] = []
    if not folder.is_dir():
        return items

    for path in folder.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ALLOWED_ARCHIVES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "size_human": bundler.human_size(stat.st_size),
                "suffix": suffix,
                "imported": path.name in imported,
                "big": stat.st_size > 200 * 1024 * 1024,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
            }
        )

    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/import")
def import_page():
    return render_template(
        "import.html",
        folder=games_folder(),
        files=_scan_games_folder(),
        max_mb=bundler.MAX_ARCHIVE_BYTES // (1024 * 1024),
    )


@app.post("/import/<path:name>")
def import_one(name: str):
    folder = games_folder()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        abort(404)
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_ARCHIVES:
        flash("Этот файл не похож на архив игры.", "error")
        return redirect(url_for("import_page"))

    title = path.stem.replace("_", " ").replace("-", " ").strip() or path.stem
    try:
        game_id = _import_archive(
        path, title, suffix, source=path.name,
        uploader=g.user["username"],
        allow_windows=bool(g.site.get("allow_windows")),
        scan_enabled=bool(g.site.get("scan_uploads", True)),
    )
    except bundler.ArchiveError as exc:
        applog.log(f"IMPORT fail file={path.name!r} user={g.user['username']}: {exc}", logging.WARNING)
        flash(f"{path.name}: {exc}", "error")
        return redirect(url_for("import_page"))

    applog.log(f"IMPORT file={path.name!r} game={game_id} user={g.user['username']}")

    game = library.get(game_id) or {}
    if game.get("status") == "ready":
        return redirect(url_for("play", game_id=game_id))
    return redirect(url_for("setup_game", game_id=game_id))


def _archive_in_folder(name: str) -> Path | None:
    """Файл архива строго внутри папки games (без путей и обхода)."""
    name = (name or "").strip()
    if not name or Path(name).name != name:
        return None
    folder = games_folder().resolve()
    target = (folder / name).resolve()
    if target.parent != folder or not target.is_file():
        return None
    if target.suffix.lower() not in ALLOWED_ARCHIVES:
        return None
    return target


@app.post("/import/delete")
@auth.admin_required
def import_delete():
    name = (request.form.get("name") or "").strip()
    target = _archive_in_folder(name)
    if target is None:
        flash("Архив не найден в папке импорта.", "error")
        return redirect(url_for("import_page"))
    try:
        target.unlink()
    except OSError as exc:
        flash(f"Не удалось удалить архив: {exc}", "error")
        return redirect(url_for("import_page"))
    applog.log(f"IMPORT delete file={name!r} by={g.user['username']}", logging.WARNING)
    flash(f"Архив {name} удалён.", "ok")
    return redirect(url_for("import_page"))


@app.post("/import/delete-imported")
@auth.admin_required
def import_delete_imported():
    imported = set(library.sources())
    removed = 0
    for source in imported:
        target = _archive_in_folder(source)
        if target is None:
            continue
        try:
            target.unlink()
            removed += 1
        except OSError:
            continue
    applog.log(f"IMPORT delete-imported count={removed} by={g.user['username']}", logging.WARNING)
    flash(f"Удалено архивов с диска: {removed}", "ok")
    return redirect(url_for("import_page"))


@app.post("/import-all")
def import_all():
    folder = games_folder()
    imported = library.sources()
    done = 0
    skipped: list[str] = []
    errors: list[str] = []

    for path in sorted(folder.iterdir() if folder.is_dir() else []):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_ARCHIVES:
            continue
        if path.name in imported:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 200 * 1024 * 1024:
            skipped.append(path.name)
            continue

        title = path.stem.replace("_", " ").replace("-", " ").strip() or path.stem
        try:
            _import_archive(
        path, title, path.suffix.lower(), source=path.name,
        uploader=g.user["username"],
        allow_windows=bool(g.site.get("allow_windows")),
        scan_enabled=bool(g.site.get("scan_uploads", True)),
    )
            done += 1
        except bundler.ArchiveError as exc:
            errors.append(f"{path.name}: {exc}")

    if done:
        applog.log(f"IMPORT-ALL imported={done} skipped={len(skipped)} user={g.user['username']}")
        flash(f"Импортировано игр: {done}. Осталось настроить файл запуска.", "ok")
    if skipped:
        flash("Пропущены большие архивы (импортируйте вручную): " + ", ".join(skipped), "error")
    for item in errors:
        flash(item, "error")

    return redirect(url_for("index"))


@app.get("/games/<game_id>")
def game_page(game_id: str):
    """Публичная страница игры: обложка, описание, запуск."""
    game = _load(game_id)
    if game["status"] != "ready":
        return redirect(url_for("setup_game", game_id=game_id))
    return render_template("game_page.html", game=game)


@app.route("/games/<game_id>/edit", methods=["GET", "POST"])
@auth.admin_required
def edit_game(game_id: str):
    """Карточка игры: название, описание, обложка и настройки DOSBox."""
    game = _load(game_id)
    files = library.files_dir(game_id)
    result = bundler.import_game_dir(files) if files.is_dir() else None
    candidates = result.candidates if result else []

    if request.method == "POST":
        title = (request.form.get("title") or game["title"]).strip()[:120] or game["title"]
        description = (request.form.get("description") or "").strip()[:2000]
        machine = request.form.get("machine") or game.get("machine") or "svga_s3"
        cycles = request.form.get("cycles") or game.get("cycles") or "3000"
        sbtype = request.form.get("sbtype") or game.get("sbtype") or "sb16"
        exe_rel = (request.form.get("exe") or game.get("exe") or "").strip()
        args = (request.form.get("args") or game.get("args") or "").strip()[:120]
        backend = (request.form.get("backend") or game.get("backend") or "dosbox")
        if backend not in ("dosbox", "dosboxX"):
            backend = "dosbox"
        rebuild = request.form.get("rebuild") == "1"

        library.update(
            game_id,
            title=title,
            description=description,
            machine=machine,
            cycles=cycles,
            sbtype=sbtype,
            exe=exe_rel or None,
            args=args,
            backend=backend,
        )

        image = request.files.get("cover")
        if image is not None and image.filename:
            suffix = Path(image.filename).suffix.lower()
            if suffix in ALLOWED_COVERS:
                name = f"cover{suffix}"
                target = library.game_dir(game_id) / name
                image.save(target)
                if not cover_bytes_ok(target):
                    target.unlink(missing_ok=True)
                    flash("Файл не похож на картинку — обложка отклонена.", "error")
                else:
                    if game.get("cover") and game["cover"] != name:
                        (library.game_dir(game_id) / game["cover"]).unlink(missing_ok=True)
                    library.update(game_id, cover=name)
            else:
                flash("Обложка должна быть PNG, JPG, WEBP или GIF.", "error")

        if rebuild and exe_rel and files.is_dir():
            config = bundler.build_config(title, exe_rel, machine, cycles, sbtype, args=args)
            try:
                size = bundler.make_bundle(files, library.bundle_path(game_id), config)
                library.update(game_id, status="ready", bundle_size=size)
                flash("Настройки сохранены, игра пересобрана.", "ok")
            except OSError as exc:
                flash(f"Настройки сохранены, но пересборка не удалась: {exc}", "error")
        else:
            flash("Карточка игры сохранена.", "ok")

        library.write_meta(game_id)
        applog.log(f"EDIT game={title!r} rebuild={rebuild} user={g.user['username']}")
        return redirect(url_for("edit_game", game_id=game_id))

    return render_template(
        "game_edit.html",
        game=game,
        candidates=candidates,
        machines=bundler.MACHINES,
        sounds=bundler.SOUNDS,
        cycles=bundler.CYCLE_PRESETS,
        selected_exe=game.get("exe"),
    )


@app.get("/games/<game_id>/setup")
def setup_game(game_id: str):
    game = _load(game_id)
    if game["status"] == "ready":
        return redirect(url_for("play", game_id=game_id))

    result = bundler.import_game_dir(library.files_dir(game_id))
    return render_template(
        "setup.html",
        game=game,
        candidates=result.candidates,
        no_dos=not bundler.has_usable_dos(result.candidates),
        total_size_human=bundler.human_size(result.total_size),
        machines=bundler.MACHINES,
        sounds=bundler.SOUNDS,
        cycles=bundler.CYCLE_PRESETS,
        selected_exe=game.get("exe") or bundler.default_candidate(result.candidates),
    )


@app.post("/games/<game_id>/build")
def build_game(game_id: str):
    game = _load(game_id)
    files_dir = library.files_dir(game_id)

    exe_rel = (request.form.get("exe") or "").strip()
    exe_path = (files_dir / exe_rel).resolve()
    if not exe_rel or not exe_path.is_file() or files_dir.resolve() not in exe_path.parents:
        flash("Выберите файл, который нужно запускать.", "error")
        return redirect(url_for("setup_game", game_id=game_id))

    title = (request.form.get("title") or game["title"]).strip() or game["title"]
    machine = request.form.get("machine") or "svga_s3"
    cycles = request.form.get("cycles") or "3000"
    sbtype = request.form.get("sbtype") or "sb16"
    args = (request.form.get("args") or "").strip()[:120]
    backend = request.form.get("backend") if request.form.get("backend") in ("dosbox", "dosboxX") else "dosbox"

    config = bundler.build_config(title, exe_rel, machine, cycles, sbtype, args=args)
    try:
        size = bundler.make_bundle(files_dir, library.bundle_path(game_id), config)
    except OSError as exc:
        flash(f"Не удалось собрать бандл: {exc}", "error")
        return redirect(url_for("setup_game", game_id=game_id))

    library.update(
        game_id,
        title=title,
        exe=exe_rel,
        machine=machine,
        cycles=cycles,
        sbtype=sbtype,
        args=args,
        backend=backend,
        status="ready",
        bundle_size=size,
    )
    library.write_meta(game_id)
    applog.log(f"BUNDLE game={title!r} exe={exe_rel!r} size={size} user={g.user['username']}")
    return redirect(url_for("play", game_id=game_id))


# --------------------------------------------------------------------------- #
# Игра
# --------------------------------------------------------------------------- #


@app.get("/selftest")
def selftest():
    """Диагностика: эталонный бандл Digger, без конфига игры."""
    return render_template("selftest.html")

@app.post("/games/<game_id>/probe")
@auth.admin_required
def probe_game(game_id: str):
    game = _load(game_id)
    status, note, ratio = probe.run_browser_probe(game)
    library.update(
        game_id,
        play_status=status,
        play_note=note,
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    applog.log(f"PROBE game={game_id} status={status} ratio={ratio:.3f}")
    flash(f"Проверка: {note}", "ok" if status == "ok" else "error")
    return redirect(url_for("game_page", game_id=game_id))


@app.post("/games/<game_id>/play-status")
@auth.admin_required
def set_play_status(game_id: str):
    game = _load(game_id)
    value = (request.form.get("value") or "").strip()
    if value not in {"ok", "exe_only", "pending"}:
        abort(400)
    note = {
        "ok": "Запускается в браузере (подтверждено вручную)",
        "exe_only": "В браузере не запускается — только .EXE (вручную)",
        "pending": "Ожидает проверки",
    }[value]
    library.update(
        game_id,
        play_status=value,
        play_note=note,
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    flash(f"«{game['title']}»: {note}", "ok")
    return redirect(url_for("game_page", game_id=game_id))


@app.get("/play/<game_id>")
def play(game_id: str):
    game = _load(game_id)
    if game["status"] != "ready":
        return redirect(url_for("setup_game", game_id=game_id))
    _ensure_playable(game)
    # js-dos кэширует бандлы в браузере, поэтому после пересборки добавляем версию.
    bundle = library.bundle_path(game_id)
    stamp = int(bundle.stat().st_mtime) if bundle.exists() else 0
    build_tag = "".join(ch for ch in APP_BUILD if ch.isdigit())[-6:]
    # случайный хвост: каждый запуск — новый URL, кэш js-dos не может «залипнуть»
    nonce = secrets.token_hex(3)
    version = f"{stamp}-{build_tag}-{nonce}"
    library.record_play(game_id)
    return render_template("play.html", game=game, bundle_version=version)


def _bundle_valid(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return ".jsdos/dosbox.conf" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _repair_bundle(game: dict) -> None:
    """Пересобирает бандл, если в нём нет .jsdos/dosbox.conf."""
    files = library.files_dir(game["id"])
    target = library.bundle_path(game["id"])
    if not files.is_dir():
        return
    exe = game.get("exe") or ""
    if not exe:
        return
    config = bundler.build_config(
        game.get("title") or "DOS game",
        Path(exe).as_posix(),
        game.get("machine") or "svga_s3",
        str(game.get("cycles") or "3000"),
        game.get("sbtype") or "sb16",
        args=game.get("args") or "",
    )
    bundler.make_bundle(files, target, config)
    library.update(game["id"], bundle_size=target.stat().st_size)
    applog.log(f"BUNDLE repaired game={game['id']} bytes={target.stat().st_size}",
               logging.WARNING)


@app.get("/games/<game_id>/<version>/bundle.jsdos")
def bundle_file(game_id: str, version: str | None = None):
    game = _load(game_id)
    _ensure_playable(game)
    path = library.bundle_path(game_id)
    if not path.is_file():
        abort(404)
    if not _bundle_valid(path):
        _repair_bundle(game)
    if not _bundle_valid(path):
        abort(500, "Бандл игры повреждён: нет .jsdos/dosbox.conf")
    response = send_file(path, mimetype="application/zip", conditional=True)
    # Версия в пути (а не в ?query) — js-dos определяет бандл по расширению.
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Game-Title"] = game["title"]
    applog.log(f"BUNDLE served game={game['title']!r} bytes={path.stat().st_size} ip={request.remote_addr}")
    return response


@app.get("/games/<game_id>/cover")
def cover_file(game_id: str):
    game = _load(game_id)
    name = game.get("cover")
    if not name:
        abort(404)
    path = library.game_dir(game_id) / name
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


COVER_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def cover_bytes_ok(path: Path) -> bool:
    """Проверяет сигнатуру картинки, а не только расширение."""
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    if head.startswith(COVER_MAGIC):
        return True
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


@app.post("/games/<game_id>/cover")
def cover_upload(game_id: str):
    game = _load(game_id)
    image = request.files.get("cover")
    if image is None or not image.filename:
        flash("Выберите картинку для обложки.", "error")
        return redirect(request.referrer or url_for("index"))

    suffix = Path(image.filename).suffix.lower()
    if suffix not in ALLOWED_COVERS:
        flash("Обложка должна быть PNG, JPG, WEBP или GIF.", "error")
        return redirect(request.referrer or url_for("index"))

    name = f"cover{suffix}"
    target = library.game_dir(game_id) / name
    image.save(target)
    if not cover_bytes_ok(target):
        target.unlink(missing_ok=True)
        flash("Файл не похож на картинку — обложка отклонена.", "error")
        return redirect(request.referrer or url_for("index"))
    if game.get("cover") and game["cover"] != name:
        (library.game_dir(game_id) / game["cover"]).unlink(missing_ok=True)
    library.update(game_id, cover=name)
    library.write_meta(game_id)
    return redirect(request.referrer or url_for("play", game_id=game_id))


@app.post("/drafts/clear")
@auth.admin_required
def clear_drafts():
    drafts = library.list_drafts()
    for game in drafts:
        library.delete(game["id"])
    applog.log(f"DRAFTS cleared count={len(drafts)} by={g.user['username']}", logging.WARNING)
    flash(f"Удалено незаконченных загрузок: {len(drafts)}", "ok")
    return redirect(url_for("index"))


@app.post("/games/<game_id>/delete")
@auth.admin_required
def delete_game(game_id: str):
    game = library.get(game_id)
    library.delete(game_id)
    applog.log(f"DELETE game={game['title'] if game else game_id!r} user={g.user['username']}", logging.WARNING)
    flash("Игра удалена.", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Сборка автономного exe
# --------------------------------------------------------------------------- #


@app.get("/games/<game_id>/exe")
def exe_page(game_id: str):
    game = _load(game_id)
    if game["status"] != "ready":
        return redirect(url_for("setup_game", game_id=game_id))
    _ensure_playable(game)
    builds = library.list_builds(game_id)
    running = next((item for item in builds if item["status"] == "running"), None)
    return render_template(
        "exe.html",
        subscribed=True if SIMPLE_MODE else auth.subscription_active(g.user),
        game=game,
        builds=builds,
        running=running,
        pyinstaller_ok=builder.pyinstaller_available(),
        data_folder=builder.app_id_for(game["title"], game_id),
    )


@app.post("/games/<game_id>/build-exe")
def build_exe(game_id: str):
    game = _load(game_id)
    _ensure_playable(game)
    if game["status"] != "ready":
        return redirect(url_for("setup_game", game_id=game_id))
    if not builder.pyinstaller_available():
        flash(
            "PyInstaller не установлен в Python, под которым запущен сервер. "
            "Установите: python -m pip install pyinstaller",
            "error",
        )
        return redirect(url_for("exe_page", game_id=game_id))
    if library.running_build(game_id):
        flash("Сборка уже идёт — дождитесь окончания.", "error")
        return redirect(url_for("exe_page", game_id=game_id))

    build_id = library.create_build(game_id)
    threading.Thread(
        target=_run_build, args=(build_id, game, game_id), daemon=True
    ).start()
    return redirect(url_for("exe_page", game_id=game_id))


def _run_build(build_id: str, game: dict, game_id: str) -> None:
    """Фоновая сборка: PyInstaller + DOSBox + файлы игры."""
    work_root = library.builds_dir(game_id) / ".work" / build_id
    try:
        conf = bundler.build_config(
            game["title"],
            game["exe"],
            game.get("machine") or "svga_s3",
            str(game.get("cycles") or "3000"),
            game.get("sbtype") or "sb16",
            mount_path="game",
            exit_after=True,
        )
        exe_path = builder.build_exe(
            title=game["title"],
            game_id=game_id,
            files_dir=library.files_dir(game_id),
            conf_text=conf,
            work_dir=work_root,
        )
        target_dir = library.builds_dir(game_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{builder.slugify(game['title'])}-{build_id[:6]}.exe"
        target = target_dir / filename
        shutil.move(str(exe_path), target)
        library.update_build(
            build_id, status="done", filename=filename, size=target.stat().st_size
        )
        library.prune_builds(game_id)
        applog.log(f"BUILD exe game={game['title']!r} size={target.stat().st_size} build={build_id}")
    except builder.BuildError as exc:
        applog.log(f"BUILD exe failed game={game['title']!r}: {exc}", logging.ERROR)
        library.update_build(build_id, status="error", message=str(exc)[-1500:])
    except Exception as exc:  # pragma: no cover - защита от неожиданностей
        applog.log(f"BUILD exe crashed game={game['title']!r}: {exc!r}", logging.ERROR)
        library.update_build(build_id, status="error", message=repr(exc)[:1500])
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


@app.get("/builds/<build_id>/download")
def download_build(build_id: str):
    row = library.get_build(build_id)
    if row:
        game = library.get(row["game_id"]) or {}
        _ensure_playable(game)
    path = library.build_file(build_id)
    if not path:
        abort(404)
    return send_file(
        path, as_attachment=True, download_name=path.name,
        mimetype="application/octet-stream",
    )


@app.post("/builds/<build_id>/delete")
def delete_build(build_id: str):
    row = library.get_build(build_id)
    if not row:
        abort(404)
    library.delete_build(build_id)
    flash("Сборка удалена.", "ok")
    return redirect(url_for("exe_page", game_id=row["game_id"]))


@app.get("/api/games/<game_id>/builds")
def api_builds(game_id: str):
    _load(game_id)
    return jsonify(library.list_builds(game_id))


# --------------------------------------------------------------------------- #
# Настройки сервера
# --------------------------------------------------------------------------- #


@app.get("/settings")
def settings_page():
    return render_template(
        "settings.html",
        settings=server_settings.load(),
        hosts=server_settings.host_choices(),
        current_host=app.config.get("RETRO_HOST", "0.0.0.0"),
        current_port=app.config.get("RETRO_PORT", 5000),
        lan=lan_ip(),
    )


@app.post("/settings")
def settings_save():
    host = (request.form.get("host") or "").strip()
    raw_port = (request.form.get("port") or "").strip()
    if not host:
        host = "0.0.0.0"
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        flash("Порт должен быть целым числом от 1 до 65535.", "error")
        return redirect(url_for("settings_page"))

    server_settings.save(host, port)
    applog.log(f"SERVER settings host={host}:{port} by={g.user['username']}")
    flash(f"Сохранено: {host}:{port}. Перезапустите сервер, чтобы применить.", "ok")
    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------- #
# Служебное
# --------------------------------------------------------------------------- #


@app.get("/api/games")
def api_games():
    return jsonify(library.list_games())


CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "media-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), camera=(), microphone=(), usb=()"
    )
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


@app.after_request
def no_store_html(response):
    """HTML не кэшируем: иначе браузер держит старую разметку после обновлений."""
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.post("/api/client-error")
def client_error():
    """Ошибки плеера из браузера — в журнал, чтобы не просить консоль руками."""
    if not auth.allow_attempt(auth.client_key("clienterr"), limit=40, window_seconds=60):
        return "", 429
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "")[:600]
    where = str(payload.get("where") or "")[:200]
    user_agent = (request.headers.get("User-Agent") or "")[:180]
    applog.log(f"CLIENT ERROR where={where!r} msg={message!r} ua={user_agent!r}", logging.WARNING)
    return "", 204


@app.post("/api/play/<game_id>")
def api_play(game_id: str):
    """Отметить запуск игры — вызывается из модального плеера."""
    _load(game_id)
    library.record_play(game_id)
    return "", 204


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.errorhandler(400)
def bad_request(_error):
    return render_template("error.html", code=400, message="Некорректный запрос"), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="Доступ запрещён"), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="Страница не найдена"), 404


@app.errorhandler(500)
def server_error(error):
    applog.log(f"HTTP 500: {error!r}", logging.ERROR)
    return (
        render_template(
            "error.html",
            code=500,
            message="Внутренняя ошибка. Если вы только что обновили код — перезапустите сервер.",
        ),
        500,
    )


@app.errorhandler(413)
def too_large(_error):
    flash(
        f"Архив больше {bundler.MAX_ARCHIVE_BYTES // (1024 * 1024)} МБ. "
        "Для больших игр поднимите лимит в bundler.MAX_ARCHIVE_BYTES.",
        "error",
    )
    return redirect(url_for("upload_page")), 303


def _ensure_playable(game: dict) -> None:
    """Блокирует запуск и скачивание игр, не прошедших проверку безопасности."""
    if (game.get("scan_status") or "") in ("infected", "blocked"):
        applog.log(
            f"SECURITY blocked game={game.get('id')} status={game.get('scan_status')} "
            f"ip={request.remote_addr}",
            logging.WARNING,
        )
        abort(403, "Игра заблокирована проверкой безопасности.")


def _load(game_id: str) -> dict:
    if not GAME_ID_RE.match(game_id):
        abort(404)
    game = library.get(game_id)
    if not game:
        abort(404)
    return game


def main() -> int:
    # Консоль Windows бывает в cp1251 — не даём печати уронить запуск.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="Retro Web — DOS-игры в браузере")
    parser.add_argument("--host", help="адрес, на котором слушать (например 0.0.0.0 или 127.0.0.1)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию 5000)")
    parser.add_argument("--data", help="папка библиотеки (по умолчанию ./data)")
    parser.add_argument("--admin-user", help="имя администратора (по умолчанию admin)")
    parser.add_argument(
        "--admin-password",
        help="пароль администратора: создаст его или сбросит существующий",
    )
    parser.add_argument(
        "--check-admin",
        action="store_true",
        help="проверить, что сохранённый пароль администратора совпадает с --admin-password, и выйти",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="показать пользователей и выйти",
    )
    parser.add_argument(
        "--reset-admin",
        action="store_true",
        help="интерактивно задать новый пароль администратора и выйти",
    )
    parser.add_argument(
        "--prune-drafts",
        nargs="?",
        type=int,
        const=24,
        default=None,
        metavar="ЧАСЫ",
        help="удалить незаконченные загрузки старше N часов (по умолчанию 24) и выйти",
    )
    parser.add_argument(
        "--rebuild-bundles",
        nargs="?",
        const="all",
        default=None,
        metavar="ID",
        help="пересобрать бандлы игр из файлов (ID или all) и выйти",
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="перепроверить все игры антивирусом и обновить статусы",
    )
    parser.add_argument(
        "--fix-bundles",
        action="store_true",
        help="добавить русскую кодовую страницу в уже собранные бандлы игр",
    )
    args = parser.parse_args()

    if args.data:
        os.environ["RETRO_DATA_DIR"] = args.data
    setup_paths()

    if args.reset_admin:
        username = (args.admin_user or "admin").strip() or "admin"
        user = auth.get_by_login(username)
        renamed_from = None
        if user is None and username != "admin":
            legacy = auth.get_by_login("admin")
            if legacy is not None:
                if legacy["role"] != auth.ROLE_ADMIN:
                    print("Имя «admin» занято обычным пользователем — выберите другой логин.")
                    return 1
                auth.update_user(legacy["id"], username=username)
                user = auth.get_user(legacy["id"])
                renamed_from = "admin"
        if user is not None and user["role"] != auth.ROLE_ADMIN:
            print(f"Пользователь «{username}» уже существует и не является администратором.")
            print("Выберите другое имя или смените роль через панель управления.")
            return 1

        print(f"Сброс администратора «{username}»")
        if renamed_from:
            print(f"(логин «{renamed_from}» будет переименован в «{username}»)")
        if sys.stdin.isatty():
            print("Пароль вводится без эха и не попадает в историю команд.\n")
        while True:
            first = _prompt_password("Новый пароль: ")
            second = _prompt_password("Повторите: ")
            if first != second:
                print("Пароли не совпадают, попробуйте снова.")
                continue
            errors = auth.validate_password(first, username, "")
            if errors:
                for error in errors:
                    print(" ", error)
                continue
            break

        if user:
            auth.set_password(user["id"], first, must_change=False)
            auth.update_user(user["id"], role=auth.ROLE_ADMIN, verified=1, active=1)
        else:
            try:
                auth.create_user(
                    username,
                    f"{username}@{auth.ADMIN_EMAIL_DOMAIN}",
                    first,
                    role=auth.ROLE_ADMIN,
                    verified=True,
                )
            except sqlite3.IntegrityError:
                # почта прежнего админа уже занята — берём свободную
                suffix = secrets.token_hex(2)
                auth.create_user(
                    username,
                    f"{username}.{suffix}@{auth.ADMIN_EMAIL_DOMAIN}",
                    first,
                    role=auth.ROLE_ADMIN,
                    verified=True,
                )
        print(f"\nГотово: логин «{username}», пароль обновлён.")
        if renamed_from:
            print(f"Старый логин «{renamed_from}» больше не действует.")
        if any(ord(ch) > 127 for ch in first):
            print("Внимание: в пароле есть не-ASCII символы — при входе проверьте раскладку.")
        applog.log(f"CLI admin reset user={username} renamed_from={renamed_from}", logging.WARNING)
        return 0

    if args.fix_bundles:
        import zipfile

        fixed = 0
        for game in library.list_games():
            path = library.bundle_path(game["id"])
            if not path.is_file():
                continue
            with zipfile.ZipFile(path) as src:
                if ".jsdos/dosbox.conf" not in src.namelist():
                    continue
                conf = src.read(".jsdos/dosbox.conf").decode("utf-8", "replace")
                if "codepage" in conf.lower():
                    continue
                if "[autoexec]" in conf:
                    conf = conf.replace(
                        "[autoexec]",
                        "[dos]\ncodepage = 866\nkeyboardlayout = ru\n\n[autoexec]",
                        1,
                    )
                else:
                    conf += "\n[dos]\ncodepage = 866\nkeyboardlayout = ru\n"
                tmp = path.with_name(path.name + ".tmp")
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
                    for name in src.namelist():
                        data = conf.encode("utf-8") if name == ".jsdos/dosbox.conf" else src.read(name)
                        dst.writestr(name, data)
            tmp.replace(path)
            fixed += 1
            print(f"  обновлён: {game['title']}")
        print(f"Бандлов обновлено: {fixed}")
        return 0

    if args.prune_drafts is not None:
        removed = library.prune_drafts(args.prune_drafts)
        print(f"Удалено незаконченных загрузок: {removed}")
        return 0

    if args.rebuild_bundles is not None:
        if args.rebuild_bundles == "all":
            games = library.list_games()
        else:
            game = library.get(args.rebuild_bundles)
            games = [game] if game else []
        done = 0
        for game in games:
            if rebuild_bundle(game["id"]):
                done += 1
                print(f"  пересобран: {game['title']}")
            else:
                print(f"  пропущен (нет файлов): {game['title']}")
        print(f"Готово: {done}")
        return 0

    if args.scan_all:
        games = library.list_games() + library.list_drafts()
        print(f"Проверяю игр: {len(games)}")
        for game in games:
            bundle = library.bundle_path(game["id"])
            files_dir = library.files_dir(game["id"])
            result = scanner.scan_game(
                bundle if bundle.is_file() else None,
                files_dir if files_dir.is_dir() else None,
                allow_windows=bool(site_settings.load().get("allow_windows")),
            )
            library.update(
                game["id"],
                scan_status=result.status,
                scan_note=result.note[:1000] or None,
                scanned_at=datetime.now().isoformat(timespec="seconds"),
                sha256=(scanner.sha256_file(bundle) if bundle.is_file()
                        else scanner.sha256_tree(files_dir) if files_dir.is_dir() else None),
            )
            print(f"  {game['title']:<30} {result.status:<10} {result.note[:60]}")
        return 0

    if args.list_users:
        users = auth.list_users()
        if not users:
            print(f"Пользователей нет в {library.DATA_DIR}")
            return 1
        for user in users:
            print(
                f"{user['username']:<20} role={user['role']:<6} verified={user['verified']} "
                f"active={user['active']} must_change={user['must_change']} last_login={user['last_login'] or '-'}"
            )
        return 0

    if args.check_admin:
        user = auth.get_by_login(args.admin_user or "admin")
        if not user:
            print("Администратор не найден — запустите с --admin-password, чтобы создать.")
            return 1
        matches = bool(args.admin_password) and auth.verify_password(user, args.admin_password)
        print(f"пользователь: {user['username']} ({user['role']})")
        print(f"активен: {bool(user['active'])}, почта подтверждена: {bool(user['verified'])}, "
              f"требуется смена пароля: {bool(user['must_change'])}")
        print("пароль: " + ("верный" if matches else "НЕ совпадает"))
        return 0 if matches else 2

    stored = server_settings.load()
    host = args.host or os.environ.get("RETRO_HOST") or stored["host"]
    port = args.port or int(os.environ.get("RETRO_PORT") or stored["port"])

    # Порт проверяем ДО любых изменений: иначе второй запуск молча менял пароль,
    # а сайт продолжал обслуживать старый процесс.
    if port_is_busy(host, port):
        print()
        print(f"[ОШИБКА] порт {port} уже занят — сервер уже запущен другим окном.")
        print("Сайт сейчас обслуживает СТАРЫЙ процесс: он не знает о правках кода.")
        print()
        print("Что делать:")
        print("  1) найдите и закройте старое окно с сервером (Ctrl+C);")
        print(f"  2) проверьте: netstat -ano | findstr :{port}")
        print("  3) запустите снова — в консоли появится строка «сборка ...».")
        return 1

    app.config["RETRO_HOST"] = host
    app.config["RETRO_PORT"] = port

    ttl_hours = int(os.environ.get("RETRO_DRAFT_TTL_HOURS", "48") or 48)
    pruned = library.prune_drafts(ttl_hours)
    if pruned:
        print(f"  очищено незаконченных загрузок: {pruned}")
        applog.log(f"DRAFTS pruned count={pruned} ttl_hours={ttl_hours}")

    admin_info = auth.ensure_admin(args.admin_user, args.admin_password)

    print("Retro Web запущен:")
    print(f"  сборка:   {APP_BUILD}")
    print(f"  адрес:    {host}:{port}")
    print(f"  локально: http://127.0.0.1:{port}")
    if host in ("0.0.0.0", ""):
        print(f"  в сети:   http://{lan_ip()}:{port}")
    print(f"  данные:   {library.DATA_DIR}")
    applog.log(f"SERVER start build={APP_BUILD} host={host} port={port}")

    if admin_info["created"]:
        print("  --- доступ администратора ---")
        print(f"    логин:  {admin_info['username']}")
        print(f"    пароль: {admin_info['password']}")
        if admin_info.get("generated"):
            print("    пароль показан один раз, в файл не пишется;")
            print("    при первом входе система потребует его сменить")
        else:
            print("    пароль задан ключом --admin-password")
        applog.log(f"BOOTSTRAP admin created user={admin_info['username']}", logging.WARNING)
    elif admin_info["reset"]:
        print(f"  логин:  {admin_info['username']}")
        print(f"  пароль: {admin_info['password']}   (установлен только что)")
        if any(ord(ch) > 127 for ch in (admin_info["password"] or "")):
            print("  внимание: в пароле есть не-ASCII символы — при входе проверьте раскладку")
        applog.log(f"BOOTSTRAP admin password reset user={admin_info['username']}", logging.WARNING)

    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    except OSError as exc:
        print()
        print(f"[ОШИБКА] не удалось занять {host}:{port} — {exc}")
        print("Укажите другой порт: python app.py --port 5001")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

