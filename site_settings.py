#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки сайта (название, регистрация, SMTP) — data/site.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import library

THEMES = {
    "tv": "TV",
}

DEFAULTS: dict[str, Any] = {
    "title": "RETRO GAMES",
    "tagline": "DOS-игры в браузере",
    "registration_open": True,
    "allow_windows": True,
    "scan_uploads": True,
    "quarantine_uploads": True,
    "require_email": True,
    "maintenance": False,
    "theme": "tv",
    "public_url": "",
    "landing": {
        "hero_badge": "RETRO COMPUTER · ONLINE",
        "hero_title": "Ретро-игры\nпрямо в браузере",
        "hero_lead": (
            "Загрузите архив с DOS-игрой, поделитесь ссылкой с друзьями — они смогут "
            "играть онлайн без установки. А ещё — собрать автономный .EXE или APK для Android."
        ),
        "steps_title": "Как это работает",
        "steps": [
            {"title": "Загрузите игру", "text": "Архив ZIP/RAR/7Z или готовый бандл .jsdos. Система сама найдёт запускаемый файл и настроит DOSBox."},
            {"title": "Проверка безопасности", "text": "Архив сканируется антивирусом и политикой файлов, получает SHA-256 и статус проверки."},
            {"title": "Поделитесь ссылкой", "text": "У каждой игры своя страница: обложка, описание, кнопка «Поделиться»."},
            {"title": "Играйте или соберите .EXE/APK", "text": "Онлайн — сразу в браузере. Автономный .EXE или APK с DOSBox внутри — бесплатно."},
        ],
        "features_title": "Что уже есть на платформе",
        "features": [
            {"title": "Браузерный DOSBox", "text": "Игра запускается на стороне игрока, сервер отдаёт только файлы."},
            {"title": "Обмен играми", "text": "Своя библиотека, обложки, описания и ссылки для друзей."},
            {"title": "Сборка .EXE", "text": "Один файл: игра + DOSBox + launcher. Запускается двойным кликом."},
            {"title": "Проверка загрузок", "text": "ClamAV/Defender, карантин, хеши и журнал безопасности."},
            {"title": "Сборка APK для Android", "text": "Универсальный плеер или отдельное приложение под каждую игру — установка на телефон."},
            {"title": "Сохранения в браузере", "text": "Прогресс хранится локально, ничего не нужно ставить."},
        ],
        "pricing_title": "Тарифы",
        "pricing_lead": "Играть онлайн можно бесплатно. Подписка открывает свои игры и сборку .EXE.",
        "cta_title": "Готовы включить ретро?",
        "cta_text": "Библиотека открыта сразу — без регистрации, аккаунта и подписок.",
    },
    "pricing": {
        "heading": "Тарифы",
        "sub": ("Играть онлайн — бесплатно. Подписка открывает свои игры "
                "и сборку автономного .EXE и APK."),
        "note": "",
        "plans": {},
    },
    "billing": {
        "mode": "manual",
        "provider": "manual",
        "price_note": "Оплата по счёту: администратор свяжется с вами после заявки.",
        "instructions": (
            "Способы оплаты: перевод на карту или по счёту. После оплаты напишите "
            "администратору — тариф включится в течение дня."
        ),
        "requisites": "",
        "webhook_secret": "",
    },
    "smtp": {
        "host": "",
        "port": 587,
        "user": "",
        "password": "",
        "from": "",
        "tls": True,
        "ssl": False,
    },
}


def path() -> Path:
    return library.DATA_DIR / "site.json"


def load() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULTS))  # глубокая копия
    try:
        stored = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key == "smtp" and isinstance(value, dict):
                    data["smtp"].update(value)
                else:
                    data[key] = value
    except (OSError, ValueError):
        pass

    data["title"] = str(data.get("title") or DEFAULTS["title"]).strip()[:60] or DEFAULTS["title"]
    data["tagline"] = str(data.get("tagline") or "").strip()[:120]
    data["registration_open"] = bool(data.get("registration_open", True))
    data["require_email"] = bool(data.get("require_email", True))
    data["allow_windows"] = bool(data.get("allow_windows", True))
    data["scan_uploads"] = bool(data.get("scan_uploads", True))
    data["maintenance"] = bool(data.get("maintenance", False))
    data["theme"] = "tv"
    data["public_url"] = str(data.get("public_url") or "").strip()[:200]

    landing = data.get("landing") if isinstance(data.get("landing"), dict) else {}
    base_landing = json.loads(json.dumps(DEFAULTS["landing"]))
    for key, value in landing.items():
        if key in ("steps", "features") and isinstance(value, list):
            items = []
            for index, item in enumerate(value[:len(base_landing[key])]):
                if isinstance(item, dict):
                    items.append({
                        "title": str(item.get("title") or "")[:80],
                        "text": str(item.get("text") or "")[:400],
                    })
                else:
                    items.append(base_landing[key][index])
            while len(items) < len(base_landing[key]):
                items.append(base_landing[key][len(items)])
            base_landing[key] = items
        elif isinstance(value, str):
            base_landing[key] = value[:1200]
    for item in base_landing.get("features", []):
        if item.get("title") == "Админ-панель":
            item["title"] = "Сборка APK для Android"
            item["text"] = ("Универсальный плеер или отдельное приложение "
                            "под каждую игру — установка на телефон.")
    data["landing"] = base_landing

    billing = data.get("billing") if isinstance(data.get("billing"), dict) else {}
    base_billing = json.loads(json.dumps(DEFAULTS["billing"]))
    for key in ("mode", "provider", "price_note", "instructions", "requisites", "webhook_secret"):
        if key in billing:
            base_billing[key] = str(billing.get(key) or "")[:2000]
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    base_pricing = json.loads(json.dumps(DEFAULTS["pricing"]))
    for key in ("heading", "sub", "note"):
        value = pricing.get(key)
        if isinstance(value, str) and value.strip():
            base_pricing[key] = value.strip()[:2000]
    base_pricing["plans"] = {}
    raw_plans = pricing.get("plans") if isinstance(pricing.get("plans"), dict) else {}
    for key, patch in raw_plans.items():
        if key not in ("free", "pro", "year") or not isinstance(patch, dict):
            continue
        clean: dict[str, Any] = {}
        for field in ("title", "period", "tagline"):
            value = patch.get(field)
            if isinstance(value, str) and value.strip():
                clean[field] = value.strip()[:300]
        if isinstance(patch.get("price"), (int, float)):
            clean["price"] = max(0, int(patch["price"]))
        features = patch.get("features")
        if isinstance(features, list):
            clean["features"] = [str(x).strip()[:300] for x in features if str(x).strip()][:20]
        base_pricing["plans"][key] = clean
    data["pricing"] = base_pricing

    data["billing"] = base_billing

    smtp = data["smtp"]
    try:
        smtp["port"] = int(smtp.get("port") or 587)
    except (TypeError, ValueError):
        smtp["port"] = 587
    smtp["tls"] = bool(smtp.get("tls", True))
    smtp["ssl"] = bool(smtp.get("ssl", False))
    return data


def save(data: dict[str, Any]) -> dict[str, Any]:
    current = load()
    current.update(
        {
            "title": str(data.get("title") or current["title"]).strip(),
            "tagline": str(data.get("tagline") or "").strip(),
            "registration_open": bool(data.get("registration_open")),
            "require_email": bool(data.get("require_email")),
        "allow_windows": bool(data.get("allow_windows", True)),
        "scan_uploads": bool(data.get("scan_uploads")),
            "maintenance": bool(data.get("maintenance")),
            "theme": "tv",
            "public_url": str(data.get("public_url") or "").strip()[:200],
        }
    )
    smtp = dict(current["smtp"])
    for key in ("host", "user", "from"):
        smtp[key] = str(data.get(f"smtp_{key}", smtp.get(key, "")) or "").strip()
    if data.get("smtp_password"):
        smtp["password"] = str(data["smtp_password"])
    if data.get("smtp_port"):
        try:
            smtp["port"] = int(data["smtp_port"])
        except (TypeError, ValueError):
            pass
    smtp["tls"] = bool(data.get("smtp_tls"))
    smtp["ssl"] = bool(data.get("smtp_ssl"))
    current["smtp"] = smtp

    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def smtp_configured(data: dict[str, Any] | None = None) -> bool:
    smtp = (data or load())["smtp"]
    return bool(smtp.get("host"))


def _form_text(form, name: str, limit: int = 2000) -> str:
    return str((form.get(name) if hasattr(form, "get") else "") or "").strip()[:limit]


def save_landing(form) -> dict[str, Any]:
    current = load()
    landing = current["landing"]
    landing["hero_badge"] = _form_text(form, "hero_badge", 80) or landing["hero_badge"]
    landing["hero_title"] = _form_text(form, "hero_title", 200) or landing["hero_title"]
    landing["hero_lead"] = _form_text(form, "hero_lead", 1200) or landing["hero_lead"]
    landing["steps_title"] = _form_text(form, "steps_title", 80) or landing["steps_title"]
    landing["features_title"] = _form_text(form, "features_title", 80) or landing["features_title"]
    landing["pricing_title"] = _form_text(form, "pricing_title", 80) or landing["pricing_title"]
    landing["pricing_lead"] = _form_text(form, "pricing_lead", 600)
    landing["cta_title"] = _form_text(form, "cta_title", 80) or landing["cta_title"]
    landing["cta_text"] = _form_text(form, "cta_text", 400)
    for index, item in enumerate(landing["steps"]):
        item["title"] = _form_text(form, f"step_title_{index}", 80) or item["title"]
        item["text"] = _form_text(form, f"step_text_{index}", 400) or item["text"]
    for index, item in enumerate(landing["features"]):
        item["title"] = _form_text(form, f"feature_title_{index}", 80) or item["title"]
        item["text"] = _form_text(form, f"feature_text_{index}", 400) or item["text"]
    write_section("landing", landing)
    return landing


def save_billing(form) -> dict[str, Any]:
    current = load()
    billing = current["billing"]
    mode = _form_text(form, "mode", 20)
    billing["mode"] = mode if mode in ("manual", "demo", "provider") else "manual"
    billing["provider"] = _form_text(form, "provider", 40)
    billing["price_note"] = _form_text(form, "price_note", 400)
    billing["instructions"] = _form_text(form, "instructions", 2000)
    billing["requisites"] = _form_text(form, "requisites", 2000)
    secret = _form_text(form, "webhook_secret", 200)
    if secret:
        billing["webhook_secret"] = secret
    elif form.get("clear_secret") == "1":
        billing["webhook_secret"] = ""
    write_section("billing", billing)
    return billing


def write_section(name: str, value: Any) -> None:
    """Аккуратно обновляет один раздел site.json, не затирая остальные."""
    current: dict[str, Any] = {}
    try:
        stored = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            current.update(stored)
    except (OSError, ValueError):
        pass
    current[name] = value
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def save_pricing(form) -> dict[str, Any]:
    """Сохраняет страницу тарифов: заголовок, подзаголовок и карточки."""
    current = load()
    pricing = json.loads(json.dumps(current.get("pricing") or DEFAULTS["pricing"]))
    pricing["heading"] = _form_text(form, "heading", 200) or "Тарифы"
    pricing["sub"] = _form_text(form, "sub", 1000)
    pricing["note"] = _form_text(form, "note", 2000)
    plans: dict[str, Any] = {}
    for key in ("free", "pro", "year"):
        patch: dict[str, Any] = {}
        title = _form_text(form, f"{key}_title", 100)
        if title:
            patch["title"] = title
        period = _form_text(form, f"{key}_period", 50)
        if period:
            patch["period"] = period
        tagline = _form_text(form, f"{key}_tagline", 300)
        if tagline:
            patch["tagline"] = tagline
        raw_price = _form_text(form, f"{key}_price", 20).replace(" ", "")
        if raw_price.isdigit():
            patch["price"] = int(raw_price)
        features = _form_text(form, f"{key}_features", 4000)
        if features:
            patch["features"] = [line.strip() for line in features.splitlines() if line.strip()][:20]
        plans[key] = patch
    pricing["plans"] = plans
    write_section("pricing", pricing)
    return pricing
