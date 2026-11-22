"""E2E-проверка авторизации, ролей и админки."""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:5081"
CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


class Client:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect(),
        )

    def get(self, path: str):
        request = urllib.request.Request(BASE + path, headers={"User-Agent": "test"})
        try:
            response = self.opener.open(request, timeout=20)
            return response.status, response.headers, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode("utf-8", "replace")

    def post(self, path: str, data: dict, csrf: str | None = None):
        if csrf:
            data = {**data, "_csrf": csrf}
        body = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(
            BASE + path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "test"},
        )
        try:
            response = self.opener.open(request, timeout=20)
            return response.status, response.headers, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode("utf-8", "replace")

    @staticmethod
    def csrf(html: str) -> str:
        match = CSRF_RE.search(html)
        return match.group(1) if match else ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(name: str, ok: bool, extra: str = "") -> bool:
    print(("OK   " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    return ok


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    results: list[bool] = []
    admin = Client()

    status, headers, _ = admin.get("/")
    results.append(check("анонимный / -> редирект на вход", status == 302 and "/login" in headers.get("Location", "")))

    status, _, html = admin.get("/login")
    token = Client.csrf(html)
    results.append(check("страница входа отдаётся", status == 200 and bool(token)))

    people = {"username": "dandy", "email": "dandy@example.com", "password": "retro12345", "password2": "retro12345"}
    status, headers, html = admin.post("/register", people, token)
    results.append(check("регистрация первого = админ", status == 302 and "/login" in headers.get("Location", "")))

    status, _, html = admin.get("/login")
    token = Client.csrf(html)
    status, headers, html = admin.post("/login", {"login": "dandy", "password": "retro12345"}, token)
    results.append(check("вход админа", status == 302 and headers.get("Location", "").endswith("/")))

    status, _, html = admin.get("/")
    results.append(check("библиотека доступна после входа", status == 200 and "Библиотека" in html))

    status, _, html = admin.get("/admin")
    results.append(check("админка открывается", status == 200 and "ПАНЕЛЬ АДМИНИСТРАТОРА" in html))

    # создание пользователя админом
    token = Client.csrf(admin.get("/admin/users")[2])
    status, _, _ = admin.post("/admin/users/create", {
        "username": "bob", "email": "bob@example.com", "role": "user", "password": "bobpass123",
    }, token)
    results.append(check("админ создаёт пользователя", status == 302))

    # сам регистрируется второй пользователь (без SMTP -> ссылка на экране)
    guest = Client()
    token = Client.csrf(guest.get("/register")[2])
    status, headers, html = guest.post("/register", {
        "username": "carol", "email": "carol@example.com",
        "password": "carolpass1", "password2": "carolpass1",
    }, token)
    link = re.search(r'href="(http[^"]+/verify/[^"]+)"', html)
    results.append(check("регистрация показывает ссылку подтверждения", status == 200 and bool(link)))

    if link:
        path = urllib.parse.urlparse(link.group(1)).path
        status, headers, _ = guest.get(path)
        results.append(check("подтверждение почты", status == 302 and "/login" in headers.get("Location", "")))

    token = Client.csrf(guest.get("/login")[2])
    status, headers, _ = guest.post("/login", {"login": "carol", "password": "carolpass1"}, token)
    results.append(check("вход пользователя", status == 302))

    status, _, _ = guest.get("/admin")
    results.append(check("пользователю админка запрещена (403)", status == 403))

    status, _, _ = guest.post("/logout", {}, csrf=None)
    results.append(check("POST без CSRF отклоняется", status == 400))

    # настройки сайта
    token = Client.csrf(admin.get("/admin/site")[2])
    status, _, _ = admin.post("/admin/site", {
        "title": "DANDY ZONE", "tagline": "16 бит навсегда", "registration_open": "on",
        "require_email": "on", "smtp_host": "", "smtp_port": "587",
        "smtp_user": "", "smtp_password": "", "smtp_from": "", "smtp_tls": "on",
    }, token)
    results.append(check("настройки сайта сохраняются", status == 302))
    status, _, html = admin.get("/")
    results.append(check("новое название на странице", "DANDY ZONE" in html))

    status, _, html = admin.get("/admin/logs")
    results.append(check("журнал содержит события", status == 200 and "LOGIN ok" in html))

    print()
    print(f"итог: {sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
