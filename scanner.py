"""Проверка загружаемых файлов: хеши, антивирусы, политика содержимого.

Модуль не привязан к конкретной ОС:
  * Linux/macOS — clamdscan (демон) или clamscan (разовый скан);
  * Windows — Microsoft Defender через MpCmdRun.exe;
  * если антивируса нет — статус "unchecked", загрузка не блокируется по вирусу,
    но политика файлов всё равно проверяется;
  * опционально VirusTotal по SHA-256 (без выгрузки файла), если задан
    RETRO_VIRUSTOTAL_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Расширения, которые не должны появляться в игре: это не DOS-игры, а способ
# доставить исполняемый код в систему пользователя.
DANGEROUS_EXT = {
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".psd1",
    ".scr", ".lnk", ".hta", ".msi", ".msp", ".mst", ".cmd", ".reg", ".cpl",
    ".jar", ".apk", ".url", ".chm",
    ".docm", ".xlsm", ".pptm", ".iso", ".img", ".vhd", ".vhdx",
}

# Заголовки исполняемых файлов: DOS (MZ), 16-бит Windows (NE/LE/LX), 32/64-бит (PE).
DOS_SIGNATURES = (b"MZ",)
WINDOWS_SIGNATURES = (b"NE", b"LE", b"LX")

ENGINE_NONE = "none"
ENGINE_CLAMAV = "clamav"
ENGINE_DEFENDER = "defender"

MAX_HASH_BYTES = 512 * 1024 * 1024


@dataclass
class ScanResult:
    status: str          # clean | infected | unchecked | error
    note: str = ""
    engine: str = ENGINE_NONE

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "note": self.note, "engine": self.engine}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Хеш дерева: имя + содержимое каждого файла, без привязки к mtime."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")[-4000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def detect_engine() -> str:
    if shutil.which("clamdscan"):
        return "clamdscan"
    if shutil.which("clamscan"):
        return "clamscan"
    if platform.system() == "Windows":
        candidate = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
        if candidate.is_file():
            return ENGINE_DEFENDER
    return ENGINE_NONE


def _scan_clamav(path: Path, binary: str) -> ScanResult:
    cmd = [binary, "--no-summary", str(path)]
    code, output = _run(cmd)
    # ClamAV: 0 — чисто, 1 — вирус найден, 2 — ошибка.
    if code == 0:
        return ScanResult("clean", "", ENGINE_CLAMAV)
    if code == 1 or "FOUND" in output:
        return ScanResult("infected", output.strip()[:500], ENGINE_CLAMAV)
    return ScanResult("error", output.strip()[:500], ENGINE_CLAMAV)


def _scan_defender(path: Path) -> ScanResult:
    exe = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
    if not exe.is_file():
        return ScanResult("unchecked", "Defender не найден", ENGINE_DEFENDER)
    code, output = _run([str(exe), "-Scan", "-ScanType", "3", "-File", str(path),
                         "-DisableRemediation", "-NoSignaturesUpdates"], timeout=600)
    # 0 — чисто, 2 — найдена угроза, иначе ошибка сканирования.
    if code == 0:
        return ScanResult("clean", "", ENGINE_DEFENDER)
    if code == 2:
        return ScanResult("infected", "Defender обнаружил угрозу", ENGINE_DEFENDER)
    return ScanResult("error", f"код {code}: {output.strip()[:400]}", ENGINE_DEFENDER)


def scan_path(path: Path) -> ScanResult:
    """Сканирует файл или каталог доступным антивирусом."""
    engine = detect_engine()
    if engine == "clamdscan":
        return _scan_clamav(path, "clamdscan")
    if engine == "clamscan":
        return _scan_clamav(path, "clamscan")
    if engine == ENGINE_DEFENDER:
        return _scan_defender(path)
    return ScanResult("unchecked", "антивирус не найден на сервере", ENGINE_NONE)


def virustotal_lookup(sha256: str) -> ScanResult | None:
    """Проверка по хешу без загрузки файла. Нужен RETRO_VIRUSTOTAL_KEY."""
    key = os.environ.get("RETRO_VIRUSTOTAL_KEY", "").strip()
    if not key or not sha256:
        return None
    url = f"https://www.virustotal.com/api/v3/files/{sha256}"
    request = urllib.request.Request(url, headers={"x-apikey": key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ScanResult("unchecked", "файла нет в VirusTotal", "virustotal")
        return ScanResult("error", f"VirusTotal: HTTP {exc.code}", "virustotal")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return ScanResult("error", f"VirusTotal: {exc}", "virustotal")

    stats = payload.get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
    malicious = int(stats.get("malicious", 0))
    suspicious = int(stats.get("suspicious", 0))
    if malicious or suspicious:
        return ScanResult("infected", f"VirusTotal: {malicious} malicious, {suspicious} suspicious",
                          "virustotal")
    return ScanResult("clean", "VirusTotal: чисто", "virustotal")


def file_kind(path: Path) -> str:
    """dos | windows | other — по первым байтам исполняемого файла."""
    try:
        with path.open("rb") as handle:
            head = handle.read(2)
            if head not in DOS_SIGNATURES:
                return "other"
            handle.seek(0x3C)
            offset_raw = handle.read(4)
            if len(offset_raw) < 4:
                return "dos"
            offset = int.from_bytes(offset_raw, "little")
            if offset <= 0 or offset > 4 * 1024 * 1024:
                return "dos"
            handle.seek(offset)
            signature = handle.read(2)
            if signature in WINDOWS_SIGNATURES:
                return "windows"
            if signature == b"PE":
                return "windows"
            return "dos"
    except OSError:
        return "other"


def windows_files(root: Path) -> list[str]:
    """Список Windows-исполняемых файлов (PE/NE/LE) внутри игры."""
    found: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".exe", ".com", ".bat"):
            if file_kind(path) == "windows":
                found.append(path.name)
    return found


def policy_violations(root: Path, allow_windows: bool = False, max_files: int = 50000) -> list[str]:
    """Жёсткие нарушения политики: опасные расширения и лимит файлов.

    Windows-бинарники не блокируются автоматически — они лишь помечаются,
    если администратор не включил строгий режим (allow_windows=False).
    """
    problems: list[str] = []
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) > max_files:
        problems.append(f"слишком много файлов в архиве: {len(files)}")
        return problems

    for path in files:
        if path.suffix.lower() in DANGEROUS_EXT:
            problems.append(f"недопустимый тип файла: {path.name}")
        if len(problems) >= 10:
            break
    return problems


def scan_game(archive: Path | None, files_dir: Path | None, allow_windows: bool = False) -> ScanResult:
    """Полная проверка загрузки: антивирус + политика. Возвращает худший статус."""
    errors: list[str] = []
    warnings: list[str] = []
    engine = ENGINE_NONE

    if archive is not None and archive.is_file():
        result = scan_path(archive)
        engine = result.engine
        if result.status == "infected":
            return ScanResult("infected", f"архив: {result.note}", engine)
        if result.status == "error":
            errors.append(f"архив: {result.note}")

    if files_dir is not None and files_dir.is_dir():
        result = scan_path(files_dir)
        if result.engine != ENGINE_NONE:
            engine = result.engine
        if result.status == "infected":
            return ScanResult("infected", f"файлы: {result.note}", engine)
        if result.status == "error":
            errors.append(f"файлы: {result.note}")

        problems = policy_violations(files_dir, allow_windows=allow_windows)
        if problems:
            return ScanResult("blocked", "; ".join(problems), engine or "policy")

        windows = windows_files(files_dir)
        if windows and not allow_windows:
            return ScanResult(
                "blocked",
                "Windows-исполняемые файлы запрещены политикой: " + ", ".join(windows[:5]),
                engine or "policy",
            )
        if windows:
            warnings.append("Windows-файлы: " + ", ".join(windows[:5]))

    if engine == ENGINE_NONE:
        note = "антивирус не найден; проверена только политика файлов"
        if warnings:
            note += "; " + "; ".join(warnings)
        return ScanResult("unchecked", note, engine)
    if errors:
        note = "; ".join(errors + warnings)
        return ScanResult("error", note, engine)
    return ScanResult("clean", "; ".join(warnings), engine)


# Расширения, которые не нужны DOS-игре и опасны на Windows:
# при включённом карантине они вырезаются из игры и не попадают в .EXE/APK.
QUARANTINE_EXT = {
    ".scr", ".msi", ".msix", ".appx", ".msp", ".mst", ".cpl", ".lnk",
    ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".hta", ".reg", ".cmd", ".dll", ".ocx",
}


def quarantine_files(files_dir: Path, quarantine_dir: Path) -> list[str]:
    """Вырезает опасные файлы из папки игры, сохраняя их в карантине."""
    moved: list[str] = []
    if not files_dir.is_dir():
        return moved
    for path in sorted(files_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in QUARANTINE_EXT:
            continue
        rel = path.relative_to(files_dir)
        target = quarantine_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        moved.append(rel.as_posix())
    return moved


def sanitize_bundle(bundle: Path, quarantine_dir: Path) -> list[str]:
    """Пересобирает .jsdos-бандл без опасных файлов."""
    import zipfile

    if not bundle.is_file():
        return []
    moved: list[str] = []
    tmp = bundle.with_suffix(".tmp")
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            name = info.filename
            if not name.endswith("/") and Path(name).suffix.lower() in QUARANTINE_EXT:
                target = quarantine_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read(name))
                moved.append(name)
                continue
            dst.writestr(info, src.read(name))
    if moved:
        tmp.replace(bundle)
    else:
        tmp.unlink(missing_ok=True)
    return moved
