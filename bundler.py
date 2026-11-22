#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Работа с архивами игр: распаковка, поиск исполняемого файла, сборка .jsdos.

Модуль ничего не знает про Flask — только файлы и конфиги.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Имя файла конфигурации внутри бандла (требование js-dos).
JSDOS_DIR = ".jsdos"
CONF_IN_BUNDLE = f"{JSDOS_DIR}/dosbox.conf"

# Расширения, которые вообще могут быть запущены в DOS.
RUNNABLE_EXT = (".exe", ".com", ".bat")

# Слова-маркеры служебных файлов: их не предлагаем запускать первыми.
SERVICE_WORDS = (
    "setup", "install", "installer", "config", "configure", "sound", "setsound",
    "uninst", "uninstall", "unwise", "isdel", "readme", "register", "serial",
    "crack", "patch", "dos4gw", "cwsdpmi", "cwsparam", "mouse", "mousectl",
    "autoexec", "choose", "select", "dxsetup", "sysinfo", "ddhelp",
)

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# Защита от zip-бомб: ограничиваем несжатый объём, число файлов и коэффициент.
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 50000
MAX_RATIO = 150

SEVEN_ZIP_CANDIDATES = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)


class ArchiveError(Exception):
    """Ошибка распаковки или работы с архивом."""


# --------------------------------------------------------------------------- #
# Распаковка
# --------------------------------------------------------------------------- #


def seven_zip() -> str | None:
    """Путь к 7z.exe, если он установлен (нужен для .rar/.7z)."""
    for candidate in SEVEN_ZIP_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("7z") or shutil.which("7za")


def _safe_target(destination: Path, name: str) -> Path:
    """Защита от zip-slip: файл обязан остаться внутри destination."""
    cleaned = name.replace("\\", "/").lstrip("/")
    target = (destination / cleaned).resolve()
    base = destination.resolve()
    if base != target and base not in target.parents:
        raise ArchiveError(f"Архив пытается записать файл вне папки игры: {name}")
    return target


def extract_archive(archive: Path, destination: Path) -> None:
    """Распаковывает zip/rar/7z в destination. Бросает ArchiveError."""
    destination.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix.lower()

    if suffix in (".zip", ".jsdos"):
        try:
            with zipfile.ZipFile(archive) as zf:
                declared = 0
                count = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    count += 1
                    declared += info.file_size
                    if count > MAX_FILES or declared > MAX_UNCOMPRESSED_BYTES:
                        raise ArchiveError("Архив слишком большой после распаковки.")
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    target = _safe_target(destination, info.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            _check_limits(archive, destination)
            return
        except zipfile.BadZipFile as exc:
            raise ArchiveError("Файл не похож на ZIP-архив.") from exc

    if suffix in (".rar", ".7z"):
        tool = seven_zip()
        if not tool:
            raise ArchiveError(
                "Для .rar/.7z нужен 7-Zip: установите его или загрузите игру в ZIP."
            )
        result = subprocess.run(
            [tool, "x", str(archive), f"-o{destination}", "-y", "-bd", "-bso0", "-bsp0"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise ArchiveError("7-Zip не смог распаковать архив: " + (detail[-1] if detail else "ошибка"))
        _check_extracted(destination)
        _check_limits(archive, destination)
        return

    raise ArchiveError(f"Неподдерживаемый формат архива: {suffix or 'без расширения'}")


def _check_limits(archive: Path, root: Path) -> None:
    """Проверка после распаковки: объём, число файлов, коэффициент сжатия."""
    files = 0
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        files += 1
        total += path.stat().st_size
        if files > MAX_FILES:
            raise ArchiveError(f"В архиве больше {MAX_FILES} файлов — отклонено.")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ArchiveError("Распакованный архив превышает допустимый объём.")
    archive_size = max(archive.stat().st_size, 1)
    if total > archive_size * MAX_RATIO and total > 200 * 1024 * 1024:
        raise ArchiveError("Подозрительный коэффициент сжатия — возможна zip-бомба.")


def _check_extracted(root: Path) -> None:
    """После внешнего распаковщика проверяем, что ничего не вылезло наружу."""
    base = root.resolve()
    for path in base.rglob("*"):
        resolved = path.resolve()
        if base not in resolved.parents and resolved != base:
            raise ArchiveError("Архив содержит небезопасные пути, распаковка отменена.")


# --------------------------------------------------------------------------- #
# Поиск исполняемого файла
# --------------------------------------------------------------------------- #


@dataclass
class Candidate:
    rel_path: str          # путь относительно корня игры, в DOS-виде
    size: int
    score: int
    reason: str = ""
    kind: str = "dos"      # dos | windows | unknown

    @property
    def label(self) -> str:
        return self.rel_path.replace("/", "\\")

    @property
    def is_windows(self) -> bool:
        return self.kind == "windows"

    @property
    def is_service(self) -> bool:
        return self.reason.startswith("служебный")


def exe_kind(path: Path) -> str:
    """DOS-программа или Windows? У Doom95/Quake .exe имеет заголовок PE/NE."""
    try:
        with path.open("rb") as fh:
            head = fh.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return "dos"
            offset = int.from_bytes(head[0x3C:0x40], "little")
            if offset <= 0 or offset > 0x10000000:
                return "dos"
            fh.seek(offset)
            signature = fh.read(4)
            # PE — Win32/64, NE — Win16, LE/LX — Win32s/OS2.
            if signature.startswith(b"PE\0\0") or signature[:2] in (b"NE", b"LE", b"LX"):
                return "windows"
            return "dos"
    except OSError:
        return "unknown"


def _name_score(name: str, size: int) -> tuple[int, str]:
    """Чем выше балл, тем вероятнее это главный файл игры."""
    lower = name.lower()
    score = 0
    reason = ""

    for word in SERVICE_WORDS:
        if word in lower:
            score -= 60
            reason = f"служебный файл ({word})"
            break

    # Игры той эпохи: WOLF3D.EXE, DOOM.EXE, PRINCE.EXE — обычно крупные.
    score += min(size // 8192, 40)

    # .bat почти всегда обёртка, .com — маленькие утилиты.
    if lower.endswith(".bat"):
        score -= 40
    elif lower.endswith(".com"):
        score -= 10

    # «Главные» имена.
    if re.search(r"(game|play|start|run|main)", lower):
        score += 12
    if lower in ("game.exe", "play.exe", "start.exe"):
        score += 10

    # Меню выбора режимов (MNU.EXE, MENU.EXE) — штатная точка входа в старых играх:
    # оно спрашивает видео/звук и само запускает движок.
    if re.match(r"^(mnu|menu|launch|launcher)(\.|$)", lower):
        score += 22
        reason = reason or "меню запуска"
    # Загрузчик движка обычно требует параметров и сам по себе не игра.
    if re.match(r"^(load|loader|engine|driver)(\.|$)", lower):
        score -= 15

    return score, reason


def find_executables(root: Path, max_depth: int = 3) -> list[Candidate]:
    """Ищет кандидатов на запуск, сортирует по убыванию вероятности.

    Windows-программы (PE) помечаются отдельно: в DOSBox они не запустятся.
    """
    found: list[Candidate] = []
    root = root.resolve()

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in RUNNABLE_EXT:
            continue
        rel = path.relative_to(root)
        if len(rel.parts) - 1 > max_depth:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        kind = exe_kind(path) if path.suffix.lower() == ".exe" else "dos"
        score, reason = _name_score(path.name, size)

        lower_name = path.stem.lower()
        if lower_name.startswith("set"):
            score -= 25
            reason = reason or "похоже на настройщик"
        # Главный файл игры часто называется так же, как папка: DUKE3D\DUKE3D.EXE.
        if len(rel.parts) > 1 and lower_name == rel.parts[-2].lower():
            score += 25
            reason = ""

        if kind == "windows":
            score -= 200
            reason = "Windows-программа — в DOSBox не запустится"
        found.append(
            Candidate(rel_path=rel.as_posix(), size=size, score=score, reason=reason, kind=kind)
        )

    # Сначала «обычные» DOS-программы, потом служебные, в конце Windows.
    found.sort(
        key=lambda c: (c.kind == "windows", c.is_service, -c.score, -c.size, c.rel_path.lower())
    )
    return found


def default_candidate(candidates: list["Candidate"]) -> str:
    """Что предложить первым: не Windows и не служебный файл, если такое есть."""
    for candidate in candidates:
        if not candidate.is_windows and not candidate.is_service:
            return candidate.rel_path
    return candidates[0].rel_path if candidates else ""


def has_usable_dos(candidates: list["Candidate"]) -> bool:
    """Есть ли шанс запустить игру в DOSBox."""
    return any(not c.is_windows and not c.is_service for c in candidates)


# --------------------------------------------------------------------------- #
# dosbox.conf
# --------------------------------------------------------------------------- #

MACHINES = ("svga_s3", "vgaonly", "ega", "cga", "tandy", "hercules")
SOUNDS = ("sb16", "sbpro2", "adlib", "none")
CYCLE_PRESETS = (("300", "8086/PC — самые старые игры"), ("1000", "286 — Wolf3D-эра"),
                 ("3000", "386 — 1990–1992"), ("10000", "386DX-33"),
                 ("30000", "486 — Doom и рядом"), ("60000", "Pentium — быстрые 3D-игры"),
                 ("max", "максимум (может сломать старые игры)"))

CONF_TEMPLATE = """\
# Сгенерировано Retro Web для игры «{title}»
# Конфигурация DOSBox (классические ключи, совместимые с js-dos)

[sdl]
autolock = false
show_mouse = false

[dosbox]
machine = {machine}
memsize = 16

[cpu]
core = auto
cputype = auto
cycles = {cycles}

[mixer]
nosound = {nosound}
rate = 44100
blocksize = 1024
prebuffer = 20

[midi]
mididevice = none

[render]
aspect = false
scaler = none

[sblaster]
sbtype = {sbtype}
sbbase = 220
irq = 7
dma = 1
hdma = 5
oplmode = auto

[speaker]
pcspeaker = true

[dos]
# Кириллица в DOS-играх: без этого текст в CP866 рисуется символами CP437.
codepage = 866
keyboardlayout = ru

[autoexec]
@echo off
mount c {mount_path}
c:
{cd_line}
{game}{args_line}
{exit_line}
"""


def build_config(title: str, exe_rel: str, machine: str, cycles: str,
                 sbtype: str, mount_path: str = ".", exit_after: bool = False,
                 args: str = "") -> str:
    """Собирает dosbox.conf.

    exe_rel     — путь к запускаемому файлу внутри смонтированного диска;
    mount_path  — что монтировать как C: ('.' для бандла js-dos, 'game' для exe);
    exit_after  — закрыть DOSBox после выхода из игры (нужно для exe-обёртки);
    args        — аргументы командной строки (например "/u EGA 0").
    """
    if machine not in MACHINES:
        machine = "svga_s3"
    if sbtype not in SOUNDS:
        sbtype = "sb16"
    if not re.fullmatch(r"\d+|max", str(cycles)):
        cycles = "3000"

    parts = Path(exe_rel).parts
    directory = parts[:-1]
    cd_line = ""
    if directory:
        dos_dir = "\\".join(directory)
        cd_line = f"cd {dos_dir}"

    return CONF_TEMPLATE.format(
        title=title.replace("\n", " ").strip() or "DOS game",
        machine=machine,
        cycles=cycles,
        sbtype=sbtype,
        nosound="true" if sbtype == "none" else "false",
        mount_path=mount_path,
        cd_line=cd_line,
        game=parts[-1].upper(),
        args_line=(" " + args.strip()) if args and args.strip() else "",
        exit_line="exit" if exit_after else "",
    ).replace("\n\n\n", "\n\n").rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Сборка бандла
# --------------------------------------------------------------------------- #


def make_bundle(files_dir: Path, bundle_path: Path, config_text: str,
                skip_dirs: tuple[str, ...] = (JSDOS_DIR,)) -> int:
    """Пакует файлы игры + .jsdos/dosbox.conf в zip. Возвращает размер в байтах."""
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    files_dir = files_dir.resolve()

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(CONF_IN_BUNDLE, config_text.replace("\r\n", "\n"))

        for path in sorted(files_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(files_dir)
            if rel.parts and rel.parts[0] in skip_dirs:
                continue
            zf.write(path, rel.as_posix())

    return bundle_path.stat().st_size


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def human_size(num: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ГБ"


@dataclass
class ImportResult:
    files_dir: Path
    candidates: list[Candidate] = field(default_factory=list)
    total_size: int = 0


def import_game_dir(files_dir: Path) -> ImportResult:
    """Готовит данные для страницы настройки: список exe и общий размер."""
    total = sum(p.stat().st_size for p in files_dir.rglob("*") if p.is_file())
    return ImportResult(files_dir=files_dir, candidates=find_executables(files_dir), total_size=total)
