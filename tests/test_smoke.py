"""Базовые smoke-тесты: приложение поднимается, страницы отвечают, вход не нужен."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["RETRO_DATA_DIR"] = tempfile.mkdtemp(prefix="retro-test-")

import app as retro  # noqa: E402


@pytest.fixture(scope="module")
def client():
    retro.setup_paths()
    retro.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return retro.app.test_client()


def test_index_works(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "RETRO" in response.get_data(as_text=True)


def test_library_open_without_login(client):
    response = client.get("/library")
    assert response.status_code == 200


def test_upload_page_open(client):
    assert client.get("/upload").status_code == 200


def test_apps_page(client):
    assert client.get("/apps").status_code == 200


def test_manifest_and_service_worker(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert "RETRO GAMES" in manifest.get_data(as_text=True)

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "Service-Worker-Allowed" in worker.headers


def test_login_and_pricing_removed(client):
    assert client.get("/login").status_code == 302
    assert client.get("/register").status_code == 302
    assert client.get("/pricing").status_code == 302


def test_unknown_game_404(client):
    assert client.get("/games/does-not-exist").status_code == 404


def test_admin_available_locally(client):
    response = client.get("/admin")
    assert response.status_code == 200
