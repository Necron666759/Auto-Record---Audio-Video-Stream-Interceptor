#!/usr/bin/env python3
"""
auto_record_suno.py (без selenium)

Полностью автоматизированная запись собственной композиции с Suno —
"запустил и всё". В этой версии вместо пакета selenium используется
прямое общение с geckodriver по HTTP (протокол WebDriver Classic) —
никаких pip-зависимостей, только стандартная библиотека Python.

  1. Сам находит ваш default-профиль Firefox (через profiles.ini).
  2. Определяет, каким именно бинарником запускать второй браузер — по
     умолчанию берёт РЕАЛЬНЫЙ путь исполняемого файла (/proc/<pid>/exe) уже
     запущенного у вас процесса firefox-bin. Если такой процесс ещё не
     запущен — скрипт спокойно ждёт его появления (--firefox-wait-timeout,
     по умолчанию 60с), можно в это время открыть свой обычный Firefox.
     Так второй браузер гарантированно запускается тем же firefox-bin, а не
     каким-нибудь firefox-esr из PATH. Путь также можно задать вручную через
     --firefox-binary. Найденный процесс firefox-bin скрипт аккуратно
     закрывает сам (SIGTERM, с запасным SIGKILL) — ПОСЛЕ того, как скопирует
     его профиль во временную папку — и запускает вместо него свой
     собственный, с этим временным профилем; основной профиль пользователя
     при этом не меняется. После старта geckodriver скрипт дополнительно
     проверяет, что реально поднялся процесс firefox-bin для нужного
     профиля — если вместо него оказался firefox-esr, сразу выдаётся
     понятная ошибка, а не тихая подмена браузера.
  3. Открывает вашу библиотеку Suno (или указанный URL) и САМ определяет,
     какой трек сейчас играет — url конкретного трека вводить не нужно.
     От вас нужно только нажать play на нужном треке.
  4. Запускает запись системного звука (monitor-source) в момент, когда
     видит, что плеер реально заиграл (paused: false).
  4а. Пока трек играет, скрипт непрерывно следит за плеером: как только
      он ставится на паузу — запись в wav физически останавливается (ни
      секунды тишины/паузы в файл не попадает), а как только
      воспроизведение реально продолжается — запись физически
      возобновляется с этого же места, без склейки постфактум.
  5. Останавливает запись по событию 'ended' от плеера.
  6. По умолчанию запись идёт на обычной скорости (--rate 1.0) — без
     какого-либо ускорения и, соответственно, без потерь от последующего
     time-stretch. При желании можно включить ускоренную запись через
     --rate (например, --rate 2.0), но тогда обратное замедление делается
     через ffmpeg atempo, что вносит собственные искажения — используйте
     только если скорость записи важнее качества звука. Итоговый файл
     сохраняется в указанную папку (-d/--destination); имя берётся из
     названия трека, если не задано явно.

Требования:
  ffmpeg, pactl (PulseAudio/PipeWire) в системе
  Python 3 (без дополнительных pip-пакетов)

  geckodriver отдельно ставить НЕ нужно — при первом запуске скрипт сам
  скачает нужную версию с GitHub Releases в ~/.cache/auto_record_suno и
  будет переиспользовать её в дальнейшем. Если geckodriver уже стоит у
  вас в PATH — будет использован он, без повторной загрузки.

Использование (файл уже с правами на выполнение — python3 указывать не нужно):
  ./auto_record_suno.py
  ./auto_record_suno.py -d ~/Music
  ./auto_record_suno.py "https://suno.com/song/КОНКРЕТНЫЙ_ID" -d ~/Music
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import ctypes.util
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from format_options import (
    RAW_CAPTURE_RATE, RAW_CAPTURE_BIT_DEPTH, RAW_CAPTURE_WAV_CODEC, RAW_CAPTURE_FRAME_SIZE,
)
from encode_helpers import (
    parec_raw_capture_args,
    ffmpeg_raw_wav_args,
    encode_final_audio,
    output_suffix_for_container,
)


GECKODRIVER_CACHE_DIR = Path.home() / ".cache" / "auto_record_suno" / "geckodriver"
FFMPEG_CACHE_DIR = Path.home() / ".cache" / "auto_record_suno" / "ffmpeg"

# --------------------------------------------------------------------------
# Постоянный кэш профиля Firefox и пути к firefox-bin между запусками
# --------------------------------------------------------------------------
#
# Раньше на КАЖДЫЙ запуск требовалось: (а) чтобы был запущен основной
# firefox-bin пользователя (по нему определялись профиль и бинарник) и
# (б) свежее копирование его профиля во временную папку. Это неудобно для
# GUI, где по кнопке "Запустить запись" хочется, чтобы браузер поднимался
# сам, без предварительно открытого Firefox.
#
# Начиная с этой версии, после первого успешного запуска (когда основной
# firefox-bin действительно был найден) сохраняются:
#   - постоянная копия профиля -> FIREFOX_PROFILE_CACHE_DIR (переиспользуется
#     напрямую, БЕЗ повторного копирования на каждый следующий запуск);
#   - путь к использованному бинарнику firefox-bin -> FIREFOX_BINARY_CACHE_FILE.
#
# На всех последующих запусках, если кэш валиден, скрипт использует его
# напрямую и вообще не ждёт/не ищет уже запущенный процесс firefox-bin —
# свой собственный firefox-bin поднимается автоматически. См.
# resolve_firefox_launch_plan().
FIREFOX_PROFILE_CACHE_DIR = Path.home() / ".cache" / "auto_record_suno" / "firefox_profile"
FIREFOX_BINARY_CACHE_FILE = Path.home() / ".cache" / "auto_record_suno" / "firefox_binary_path.txt"


# --------------------------------------------------------------------------
# Автозагрузка geckodriver (чтобы не нужно было скачивать вручную)
# --------------------------------------------------------------------------

def _geckodriver_asset_name() -> str:
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Linux":
        arch = "linux64" if machine in ("x86_64", "amd64") else "linux-aarch64"
        return f"geckodriver-{{version}}-{arch}.tar.gz"
    if system == "Darwin":
        arch = "macos-aarch64" if machine in ("arm64", "aarch64") else "macos"
        return f"geckodriver-{{version}}-{arch}.tar.gz"
    if system == "Windows":
        arch = "win64" if machine in ("amd64", "x86_64") else "win32"
        return f"geckodriver-{{version}}-{arch}.zip"
    raise RuntimeError(f"Неизвестная платформа для автозагрузки geckodriver: {system}/{machine}")


def _latest_geckodriver_release() -> tuple[str, str]:
    """Возвращает (version, asset_download_url) последнего релиза с GitHub."""
    api_url = "https://api.github.com/repos/mozilla/geckodriver/releases/latest"
    req = urllib.request.Request(api_url, headers={"User-Agent": "auto_record_suno"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    version = data["tag_name"]
    name_template = _geckodriver_asset_name()
    asset_name = name_template.format(version=version)

    for asset in data.get("assets", []):
        if asset["name"] == asset_name:
            return version, asset["browser_download_url"]

    raise RuntimeError(f"Не найден подходящий файл релиза geckodriver: {asset_name}")


def ensure_geckodriver() -> str:
    """Возвращает путь к рабочему geckodriver, скачивая его при необходимости."""
    existing = shutil.which("geckodriver")
    if existing:
        return existing

    cached_binary = GECKODRIVER_CACHE_DIR / ("geckodriver.exe" if platform.system() == "Windows" else "geckodriver")
    if cached_binary.exists():
        return str(cached_binary)

    print("geckodriver не найден — скачиваю автоматически с GitHub Releases...")
    GECKODRIVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    version, url = _latest_geckodriver_release()
    print(f"Версия: {version}, источник: {url}")

    archive_path = GECKODRIVER_CACHE_DIR / Path(url).name
    urllib.request.urlretrieve(url, archive_path)

    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(GECKODRIVER_CACHE_DIR)
    else:
        with tarfile.open(archive_path) as tf:
            try:
                tf.extractall(GECKODRIVER_CACHE_DIR, filter="data")
            except TypeError:
                tf.extractall(GECKODRIVER_CACHE_DIR)  # Python < 3.12 без параметра filter

    archive_path.unlink(missing_ok=True)

    if not cached_binary.exists():
        raise RuntimeError(f"После распаковки не найден бинарник по ожидаемому пути: {cached_binary}")

    cached_binary.chmod(cached_binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"geckodriver готов: {cached_binary}")
    return str(cached_binary)


# --------------------------------------------------------------------------
# Поиск профиля Firefox
# --------------------------------------------------------------------------

def find_running_firefox_profile() -> str | None:
    """
    Смотрит на уже запущенный процесс firefox/firefox-bin через /proc и
    определяет, какой профиль он реально использует — просто чтение
    argv и списка открытых файлов процесса, без какого-либо подключения
    к самому браузеру.

    Возвращает путь к профилю или None, если распознать не удалось
    (например, Firefox не запущен, или это не Linux).
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None  # не Linux — пропускаем, дальше отработает profiles.ini

    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
        if not parts:
            continue
        exe_name = _process_binary_name(int(pid_dir.name), parts[0])
        if exe_name not in ("firefox", "firefox-bin", "firefox-esr"):
            continue
        if _is_firefox_child_process(parts[1:]):
            continue  # дочерний content/RDD/... процесс — у него профиль всегда родительского процесса, доверять его собственным argv нельзя

        # 1) Явный -profile <path> в аргументах запуска.
        for i, arg in enumerate(parts):
            if arg == "-profile" and i + 1 < len(parts):
                candidate = parts[i + 1]
                if Path(candidate).is_dir():
                    return candidate

        # 2) -P <имя> — ищем путь по имени в profiles.ini.
        for i, arg in enumerate(parts):
            if arg in ("-P", "--profile") and i + 1 < len(parts):
                name = parts[i + 1]
                resolved = _resolve_profile_by_name(name)
                if resolved:
                    return resolved

        # 3) Явного аргумента нет (обычный запуск без -profile/-P) — определяем
        # по фактически заблокированному профилю: у активного профиля Firefox
        # держит открытым файл lock/.parentlock, это видно через /proc/<pid>/fd.
        locked = _profile_from_open_fds(pid_dir)
        if locked:
            return locked

    return None


def _resolve_profile_by_name(name: str) -> str | None:
    base = Path.home() / ".mozilla" / "firefox"
    ini_path = base / "profiles.ini"
    if not ini_path.exists():
        return None
    config = configparser.ConfigParser()
    config.read(ini_path)
    for section in config.sections():
        if section.startswith("Profile") and config.get(section, "Name", fallback=None) == name:
            return str(base / config.get(section, "Path"))
    return None


def _profile_from_open_fds(pid_dir: Path) -> str | None:
    fd_dir = pid_dir / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError):
        return None
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.endswith((".parentlock", "/lock")) and "/firefox/" in target:
            candidate = Path(target).parent
            # ВАЖНО: readlink() отдаёт путь даже для уже недоступного файла
            # (например, если он был отмонтирован/удалён, а процесс всё ещё
            # держит его открытым по старому fd) — на некоторых нестандартных
            # сборках Firefox это встречается на практике. Без этой проверки
            # дальше make_profile_copy() упадёт с FileNotFoundError на
            # копировании несуществующей папки.
            if candidate.is_dir():
                return str(candidate)
    return None


def find_default_firefox_profile() -> str:
    """Читает ~/.mozilla/firefox/profiles.ini и возвращает путь к default профилю."""
    base = Path.home() / ".mozilla" / "firefox"
    ini_path = base / "profiles.ini"
    if not ini_path.exists():
        raise RuntimeError(f"Не найден {ini_path} — Firefox профиль не обнаружен автоматически.")

    config = configparser.ConfigParser()
    config.read(ini_path)

    candidates = []
    for section in config.sections():
        if not section.startswith("Install") and not section.startswith("Profile"):
            continue
        if config.has_option(section, "Default"):
            candidates.append((True, config.get(section, "Default")))
        elif section.startswith("Profile") and config.has_option(section, "Path"):
            is_default = config.has_option(section, "Default") and config.getboolean(section, "Default", fallback=False)
            candidates.append((is_default, config.get(section, "Path")))

    if not candidates:
        raise RuntimeError("В profiles.ini не найдено ни одного профиля.")

    for is_default, path in candidates:
        if is_default:
            return str(base / path)
    for _, path in candidates:
        if "default-release" in path:
            return str(base / path)
    return str(base / candidates[0][1])


# --------------------------------------------------------------------------
# Явный выбор бинарника firefox-bin (а не firefox-esr)
# --------------------------------------------------------------------------
#
# На некоторых системах (например Debian/Ubuntu) "firefox" в PATH — это
# symlink/обёртка, которая на самом деле указывает на firefox-esr. Если не
# сказать geckodriver'у явно, каким бинарником пользоваться, он берёт
# "firefox" из PATH и в итоге поднимает второй firefox-esr вместо желаемого
# firefox-bin. Ниже — поиск конкретно firefox-bin и проверка уже после
# запуска, что поднялся именно нужный процесс.

def _read_process_exe(pid: int) -> str | None:
    """Возвращает реальный путь к исполняемому файлу процесса (/proc/<pid>/exe)."""
    try:
        return str(Path(f"/proc/{pid}/exe").resolve())
    except OSError:
        return None


def _process_binary_name(pid: int, argv0: str | None = None) -> str | None:
    """
    Возвращает имя РЕАЛЬНОГО исполняемого файла процесса — basename
    /proc/<pid>/exe, а не argv[0] из /proc/<pid>/cmdline.

    Это принципиально: argv[0] — это то, что процесс сам заявляет о себе
    при запуске, и его может выставить в произвольную строку сам лаунчер
    (например, обёрточный скрипт вида `exec -a firefox /opt/.../firefox-bin
    "$@"`, или установка через кастомный лаунчер/AppImage/сторонний
    инсталлятор). На практике это встречается: главный (родительский)
    процесс Firefox может иметь argv[0], НЕ равный "firefox-bin", хотя
    физически запущен именно этим бинарником — и тогда поиск по одному
    только argv[0] его вообще не находит, находя лишь его дочерние
    content-процессы (у них argv[0] Firefox выставляет сам, единообразно).

    /proc/<pid>/exe — это symlink на реальный файл в файловой системе,
    его подделать через argv[0] нельзя, поэтому именно он и используется
    как основной способ определения "это firefox-bin или нет".

    Если прочитать /proc/<pid>/exe не удалось (процесс уже завершился,
    либо нет прав), используем переданный argv0 как более слабый запасной
    вариант, чтобы не терять вообще никакой сигнал.
    """
    exe = _read_process_exe(pid)
    if exe:
        return Path(exe).name
    if argv0:
        return Path(argv0).name
    return None

# Аргументы командной строки, по которым однозначно определяется, что это
# ДОЧЕРНИЙ процесс Firefox (вкладка/content-процесс, RDD, Utility, GMP-плагин
# и т.п.), а не главный/родительский процесс браузера. Дочерние процессы
# запускаются ТЕМ ЖЕ бинарником firefox-bin, что и главный — отличить их по
# одному только имени файла (argv[0]) невозможно, зато по этим флагам можно
# всегда: их ставит сам Firefox при запуске подпроцесса определённого типа,
# и главный процесс их никогда не получает.
_FIREFOX_CHILD_PROCESS_FLAGS = (
    "-contentproc",
    "-rdd",
    "-utility",
    "-socket-process",
    "-forkserver",
    "-plugin-container",
    "-gmp-plugin",
)


def _is_firefox_child_process(argv_rest: list[str]) -> bool:
    """True, если это дочерний (content/RDD/Utility/...) процесс Firefox,
    а не главный/родительский. См. _FIREFOX_CHILD_PROCESS_FLAGS."""
    return any(flag in argv_rest for flag in _FIREFOX_CHILD_PROCESS_FLAGS)


def find_running_firefox_bin_pid(timeout: float = 0.0, poll_interval: float = 0.3) -> int | None:
    """
    Ищет уже запущенный ГЛАВНЫЙ (не дочерний) процесс с именем firefox-bin
    через /proc/<pid>/cmdline. При timeout > 0 — ждёт появления такого
    процесса до timeout секунд (например, пока пользователь сам не откроет
    свой обычный Firefox), иначе проверяет один раз и сразу возвращает
    результат.

    ВАЖНО: у Firefox многопроцессная архитектура — вкладки, RDD/Utility и
    другие подпроцессы запускаются ТЕМ ЖЕ бинарником firefox-bin, что и
    главный процесс, только с доп. флагами вроде -contentproc. Если не
    отфильтровать их через _is_firefox_child_process(), можно случайно
    найти и вернуть pid дочернего content-процесса вместо настоящего
    главного — тогда его "аккуратное закрытие" никак не повлияет на
    реальный браузер (Firefox просто пересоздаст этот подпроцесс), а
    основной процесс с основным профилем пользователя продолжит работать
    как ни в чём не бывало.
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None

    deadline = time.monotonic() + timeout
    first_pass = True
    while first_pass or time.monotonic() < deadline:
        first_pass = False
        for pid_dir in proc_root.iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline_raw = (pid_dir / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError):
                continue
            parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
            if not parts:
                continue
            if _process_binary_name(int(pid_dir.name), parts[0]) != "firefox-bin":
                continue
            if _is_firefox_child_process(parts[1:]):
                continue
            return int(pid_dir.name)
        if timeout <= 0:
            break
        time.sleep(poll_interval)
    return None


def _is_pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _find_locked_profile_pid(profile_path: str, exclude_pid: int | None = None) -> int | None:
    """
    Ищет ГЛАВНЫЙ (не дочерний content/RDD/...) процесс firefox-bin, реально
    работающий с указанным профилем. Проверяет по убыванию надёжности:

      1. Явный аргумент -profile <path>, совпадающий с profile_path —
         самое надёжное совпадение, возвращается сразу.
      2. Иначе (обычный запуск без -profile/-P, профиль по умолчанию) —
         хотя бы один открытый файловый дескриптор процесса указывает
         внутрь папки профиля (prefs.js, places.sqlite, cookies.sqlite,
         sessionstore и т.п. — таких файлов у активного профиля всегда
         открыто много, в отличие от одного-единственного lock-файла).

    Раньше здесь использовалась проверка конкретно на .parentlock через
    _profile_from_open_fds — но на практике часть сборок Firefox
    открывает .parentlock и сразу делает unlink() (файл остаётся
    заблокирован через fcntl, но физически удалён с диска), и тогда
    readlink() возвращает путь с суффиксом " (deleted)", который уже не
    проходит проверку "заканчивается на .parentlock" — то есть тот способ
    не находил вообще НИКАКОЙ процесс, включая заведомо правильный.
    Дочерние процессы (см. _is_firefox_child_process) всегда исключаются,
    т.к. их "закрытие" ни на что не влияет — Firefox просто пересоздаёт их.
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None
    target_dir = str(Path(profile_path).resolve())
    fallback_pid: int | None = None
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if exclude_pid is not None and pid == exclude_pid:
            continue
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
        if not parts or _process_binary_name(pid, parts[0]) != "firefox-bin":
            continue
        argv_rest = parts[1:]
        if _is_firefox_child_process(argv_rest):
            continue
        matched_explicit = False
        for i, arg in enumerate(argv_rest):
            if arg == "-profile" and i + 1 < len(argv_rest):
                if str(Path(argv_rest[i + 1]).resolve()) == target_dir:
                    return pid
                matched_explicit = True
                break
        if not matched_explicit and fallback_pid is None:
            if _process_has_open_file_in_dir(pid_dir, target_dir):
                fallback_pid = pid
    return fallback_pid


def _process_has_open_file_in_dir(pid_dir: Path, dir_path: str) -> bool:
    """
    True, если хотя бы один открытый файловый дескриптор процесса
    указывает на файл ВНУТРИ указанной директории. Учитывает, что Linux
    дописывает суффикс ' (deleted)' к пути уже удалённого, но всё ещё
    открытого файла — такой суффикс отбрасывается перед сравнением.
    """
    fd_dir = pid_dir / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except (FileNotFoundError, PermissionError):
        return False
    dir_prefix = str(Path(dir_path).resolve()).rstrip("/") + "/"
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        if target.startswith(dir_prefix):
            return True
    return False


# Диагностический дамп процессов firefox-bin (см. _debug_dump_firefox_bin_processes
# ниже) по умолчанию ВЫКЛЮЧЕН — он был нужен только чтобы разобраться в поведении
# закрытия старого процесса firefox-bin, и в обычной работе только засоряет лог.
# Включается флагом --debug-firefox-processes (см. main()).
_DEBUG_FIREFOX_PROCESSES = False


def _debug_dump_firefox_bin_processes(label: str) -> None:
    """
    Диагностика: печатает ПОЛНЫЙ список всех процессов firefox-bin,
    видимых прямо сейчас в /proc — pid, ppid, дочерний ли это процесс
    (content/RDD/Utility/...) и явный -profile из аргументов, если есть.

    Нужна только для того, чтобы по одному логу разобраться в поведении
    закрытия старого процесса. Не влияет на поведение скрипта — только
    печатает информацию в stdout, и только если включена явно (см.
    _DEBUG_FIREFOX_PROCESSES / --debug-firefox-processes).
    """
    if not _DEBUG_FIREFOX_PROCESSES:
        return
    proc_root = Path("/proc")
    if not proc_root.exists():
        return
    rows: list[tuple[int, int | None, bool, bool, str | None, str, str]] = []
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
        if not parts or _process_binary_name(pid, parts[0]) != "firefox-bin":
            continue
        argv_rest = parts[1:]
        ppid = _read_ppid(pid)
        is_orphan = ppid is None or ppid == 1 or not _is_pid_alive(ppid)
        is_child = _is_firefox_child_process(argv_rest)
        explicit_profile = None
        for i, arg in enumerate(argv_rest):
            if arg == "-profile" and i + 1 < len(argv_rest):
                explicit_profile = argv_rest[i + 1]
                break
        args_preview = " ".join(argv_rest)[:100]
        exe_path = _read_process_exe(pid) or "?"
        rows.append((pid, ppid, is_child, is_orphan, explicit_profile, args_preview, exe_path))
    if rows:
        print(f"[диагностика:{label}] найдено {len(rows)} процесс(ов) firefox-bin:")
        for pid, ppid, is_child, is_orphan, explicit_profile, args_preview, exe_path in sorted(rows):
            kind = "ДОЧЕРНИЙ" if is_child else "главный"
            orphan_note = " ОСИРОТЕВШИЙ" if is_orphan else ""
            print(
                f"    pid={pid} ppid={ppid}{orphan_note} тип={kind} exe={exe_path} "
                f"явный_profile={explicit_profile or '-'} аргументы='{args_preview}'"
            )
    else:
        print(f"[диагностика:{label}] процессов firefox-bin не найдено.")


def _read_ppid_map() -> dict[int, int]:
    """Читает /proc и строит словарь {pid: ppid} для всех доступных процессов."""
    ppid_map: dict[int, int] = {}
    proc_root = Path("/proc")
    if not proc_root.exists():
        return ppid_map
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        ppid = _read_ppid(pid)
        if ppid is not None:
            ppid_map[pid] = ppid
    return ppid_map


def _collect_process_tree(root_pid: int) -> list[int]:
    """
    Возвращает root_pid и ВСЕХ его потомков (рекурсивно) — снимок дерева
    процессов на текущий момент.

    Это нужно, потому что у Firefox многопроцессная архитектура: SIGTERM,
    посланный ТОЛЬКО главному процессу firefox-bin, не гарантирует
    завершения его дочерних content/RDD/Utility/... процессов. Если
    главный процесс не успевает корректно закрыться сам и его приходится
    добивать SIGKILL, дочерние процессы просто осиротевают (их ppid
    становится равен 1) и продолжают работать сами по себе — удерживая
    основной профиль пользователя занятым, хотя "родительский" Firefox
    формально уже закрыт.
    """
    ppid_map = _read_ppid_map()
    children_map: dict[int, list[int]] = {}
    for pid, ppid in ppid_map.items():
        children_map.setdefault(ppid, []).append(pid)

    tree = [root_pid]
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children_map.get(current, []):
            if child not in tree:
                tree.append(child)
                stack.append(child)
    return tree


def _terminate_pid_gracefully(
    pid: int, timeout: float = 15.0, poll_interval: float = 0.2, kill_tree: bool = False
) -> None:
    """Аккуратно (НЕ kill -9 сразу) завершает процесс с данным pid: сначала
    SIGTERM — даёт приложению шанс нормально закрыться (сохранить сессию,
    закрыть вкладки и т.п.), и только если оно не уложилось в timeout —
    SIGKILL как крайний случай, чтобы не зависнуть навсегда.

    kill_tree=True: сигналы посылаются НЕ только pid, а pid И ВСЕМ его
    текущим потомкам (см. _collect_process_tree) — иначе при эскалации до
    SIGKILL дочерние процессы (вкладки/content-процессы) осиротевают и
    остаются работать сами по себе вместе с основным профилем пользователя,
    вместо того чтобы закрыться вместе с родителем. Для завершения
    найденного/лишнего firefox-bin всегда следует использовать kill_tree=True."""
    if not _is_pid_alive(pid):
        return

    targets = _collect_process_tree(pid) if kill_tree else [pid]

    any_signalled = False
    for target in targets:
        if not _is_pid_alive(target):
            continue
        try:
            os.kill(target, signal.SIGTERM)
            any_signalled = True
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f"WARNING: нет прав на завершение процесса pid {target} — оставляю как есть.", file=sys.stderr)

    if not any_signalled:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not _is_pid_alive(t) for t in targets):
            return
        time.sleep(poll_interval)

    still_alive = [t for t in targets if _is_pid_alive(t)]
    if still_alive:
        print(
            f"Процесс(ы) pid {still_alive} не завершились за {timeout:.0f}с — "
            f"принудительно (SIGKILL)...",
            file=sys.stderr,
        )
        for target in still_alive:
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(_is_pid_alive(t) for t in still_alive):
            time.sleep(poll_interval)


def _cleanup_stray_firefox_bin_processes() -> bool:
    """
    Ищет ТОЛЬКО ДЕЙСТВИТЕЛЬНО ОСИРОТЕВШИЕ процессы firefox-bin (ppid не
    существует или равен 1) — то есть такие, у которых больше нет живого
    родителя. Такое остаётся от предыдущего запуска этого же скрипта: если
    тогда главный процесс firefox-bin не закрылся сам за отведённое время
    и его пришлось добивать SIGKILL "в одиночку" (см. _terminate_pid_gracefully
    БЕЗ kill_tree — старое поведение), его дочерние content/RDD/...
    процессы осиротели вместо того, чтобы закрыться вместе с родителем, и
    остались висеть дальше, удерживая основной профиль пользователя.

    КРИТИЧЕСКИ ВАЖНО отличать такие настоящие "хвосты" от дочерних
    content-процессов ЖИВОГО, нормально работающего браузера: у последних
    ppid указывает на реально работающий (не осиротевший) главный процесс.
    Если по ошибке считать любой найденный firefox-bin (включая дочерний
    процесс работающей вкладки) "хвостом" и убивать его — это просто
    убьёт случайную вкладку/content-процесс живого браузера (пользователь
    увидит "вкладка неожиданно закрылась"), а сам главный процесс при этом
    даже не будет затронут, потому что она искалась/находилась по имени
    firefox-bin, а не по факту осиротения.

    Проблема в том, что find_running_firefox_bin_pid() ищет именно ГЛАВНЫЙ
    (не дочерний) процесс — дочерние процессы он сознательно игнорирует
    (см. _is_firefox_child_process). Если из настоящего Firefox остались
    только осиротевшие дочерние процессы, а главного процесса больше нет,
    find_running_firefox_bin_pid() провисит весь wait_timeout впустую и
    вернёт None, хотя по факту профиль всё ещё занят зависшими процессами.
    Но если единственный видимый firefox-bin — это дочерний процесс ЖИВОГО
    главного (см. выше), значит дело не в "хвостах", а в том, что сам
    главный процесс просто не опознаётся как firefox-bin (см.
    _process_binary_name) — трогать в этом случае вообще ничего не нужно.

    Завершает найденные ОСИРОТЕВШИЕ "хвосты" целиком (каждый — со всем
    своим текущим поддеревом) и возвращает True, если что-то было найдено
    и завершено.
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return False

    orphan_pids: list[int] = []
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
        if not parts or _process_binary_name(pid, parts[0]) != "firefox-bin":
            continue
        ppid = _read_ppid(pid)
        # Осиротевший = либо ppid вообще не прочитать, либо ppid == 1
        # (init/systemd — стандартный получатель осиротевших процессов),
        # либо процесс с этим ppid уже не существует. Если же ppid
        # указывает на живой процесс — это либо нормальный дочерний
        # content-процесс работающего браузера, либо сам главный процесс
        # (у него ppid — это шелл/менеджер сессии/systemd пользователя и
        # т.п., но это НЕ означает осиротение) — такие процессы не трогаем.
        if ppid is None or ppid == 1 or not _is_pid_alive(ppid):
            orphan_pids.append(pid)

    if not orphan_pids:
        return False

    print(
        "Обнаружены осиротевшие процессы firefox-bin (без живого "
        "родителя) — похоже на не до конца закрытый предыдущий запуск. "
        "Завершаю их перед повторным поиском...",
        file=sys.stderr,
    )
    handled: set[int] = set()
    for pid in orphan_pids:
        if pid in handled or not _is_pid_alive(pid):
            continue
        handled.update(_collect_process_tree(pid))
        _terminate_pid_gracefully(pid, kill_tree=True)
    return True


def resolve_firefox_binary_and_profile(
    explicit_profile: str | None,
    explicit_binary: str | None,
    wait_timeout: float = 60.0,
) -> tuple[str, str, int | None]:
    """
    Единая точка определения (профиль Firefox, бинарник firefox-bin, pid
    уже запущенного firefox-bin, который нужно будет закрыть). Печатает
    "Ожидаю появления процесса firefox-bin..." ПЕРВЫМ делом — раньше
    любого другого вывода — и использует ОДИН И ТОТ ЖЕ найденный процесс
    и для профиля, и для бинарника (а не два независимых поиска, которые
    в принципе могли бы найти разные процессы/профили). Профиль,
    определяемый через открытые fd этого процесса (_profile_from_open_fds),
    уже проверен на реальное существование каталога на диске — так что
    "призрачный" путь (процесс держит fd, а каталога уже физически нет —
    например, был отмонтирован/удалён) сюда не попадёт.

    Возвращает (profile_path, firefox_binary, pending_close_pid).
    pending_close_pid (если не None) должен быть аккуратно завершён
    вызывающим кодом (см. GeckoSession.__init__) ПОСЛЕ копирования
    профиля — не раньше, иначе на некоторых нестандартных/портативных
    сборках Firefox, которые подчищают свою рабочую папку профиля при
    закрытии, make_profile_copy() упрётся в уже несуществующий исходный
    каталог.
    """
    if explicit_binary:
        if not Path(explicit_binary).is_file():
            raise RuntimeError(f"Указанный --firefox-binary не найден: {explicit_binary}")
        profile_path = explicit_profile or find_running_firefox_profile() or find_default_firefox_profile()
        print(f"Использую профиль Firefox: {profile_path}")
        return profile_path, explicit_binary, None

    print(f"Ожидаю появления процесса firefox-bin (до {wait_timeout:.0f}с)...")
    pid = find_running_firefox_bin_pid(timeout=wait_timeout)
    _debug_dump_firefox_bin_processes("сразу после обнаружения исходного процесса")

    if pid is None and _cleanup_stray_firefox_bin_processes():
        _debug_dump_firefox_bin_processes("после очистки осиротевших процессов")
        print("Повторно жду появления главного процесса firefox-bin (до 15с)...")
        pid = find_running_firefox_bin_pid(timeout=15.0)
        _debug_dump_firefox_bin_processes("после повторного ожидания")

    firefox_binary: str | None = None
    pending_close_pid: int | None = None
    pid_profile: str | None = None
    if pid is not None:
        exe = _read_process_exe(pid)
        if exe:
            print(f"Найден процесс firefox-bin (pid {pid}): {exe}")
            firefox_binary = exe
            pending_close_pid = pid
            pid_profile = _profile_from_open_fds(Path(f"/proc/{pid}"))

    if firefox_binary is None:
        firefox_binary = shutil.which("firefox-bin")
    if firefox_binary is None:
        for path in (
            "/usr/lib/firefox/firefox-bin",
            "/usr/lib/firefox-bin/firefox-bin",
            "/usr/lib64/firefox/firefox-bin",
            "/opt/firefox/firefox-bin",
            "/usr/local/firefox/firefox-bin",
        ):
            if Path(path).is_file():
                firefox_binary = path
                break
    if firefox_binary is None:
        raise RuntimeError(
            "Не найден бинарник firefox-bin: ни среди запущенных процессов, ни в "
            "PATH, ни по типичным путям. Укажите его явно: "
            "--firefox-binary /path/to/firefox-bin"
        )

    profile_path = explicit_profile or pid_profile or find_running_firefox_profile() or find_default_firefox_profile()
    print(f"Использую профиль Firefox: {profile_path}")

    return profile_path, firefox_binary, pending_close_pid


def _find_process_locking_profile(profile_path: str, expected_names: tuple[str, ...]) -> tuple[str, int] | None:
    """
    Ищет среди запущенных процессов такой, который держит блокировку
    (lock/.parentlock) внутри указанного каталога профиля, и чьё имя
    исполняемого файла входит в expected_names. Возвращает (exe_name, pid)
    либо None, если ничего подходящего пока не найдено (например, браузер
    ещё не успел стартовать).
    """
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None

    target = str(Path(profile_path).resolve())
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        parts = [p for p in cmdline_raw.decode("utf-8", "replace").split("\x00") if p]
        if not parts:
            continue
        exe_name = _process_binary_name(int(pid_dir.name), parts[0])
        if exe_name not in expected_names:
            continue

        fd_dir = pid_dir / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for entry in entries:
            try:
                link_target = os.readlink(entry)
            except OSError:
                continue
            if not link_target.endswith((".parentlock", "/lock")):
                continue
            try:
                if str(Path(link_target).parent.resolve()) == target:
                    return exe_name, int(pid_dir.name)
            except OSError:
                continue
    return None


def wait_for_firefox_bin(profile_path: str, timeout: float = 30.0, poll_interval: float = 0.2) -> int:
    """
    Дожидается, пока для указанного профиля реально поднимется процесс
    именно firefox-bin (а не firefox-esr/firefox), и возвращает его pid.
    Если вместо этого обнаруживается firefox-esr — сразу кидает понятную
    ошибку вместо тихой подмены браузера.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _find_process_locking_profile(profile_path, ("firefox", "firefox-bin", "firefox-esr"))
        if found:
            exe_name, pid = found
            if exe_name == "firefox-bin":
                return pid
            raise RuntimeError(
                f"Вместо firefox-bin запустился процесс '{exe_name}' (pid {pid}). "
                "Похоже, что бинарник по умолчанию ведёт на другую сборку Firefox. "
                "Укажите нужный бинарник явно: --firefox-binary /path/to/firefox-bin"
            )
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Не дождался запуска процесса firefox-bin для профиля за {timeout:.0f}с."
    )


# --------------------------------------------------------------------------
# Аудио: monitor-source
# --------------------------------------------------------------------------

def get_default_sink_name() -> str:
    """Определяет sink (устройство вывода), используемое системой по
    умолчанию, через pactl. Это работает одинаково независимо от того,
    какая именно звуковая карта стоит в системе (встроенная в мат.плату,
    внешняя USB-карта и т.д.) — pactl/PulseAudio-PipeWire сами
    абстрагируют конкретное оборудование, скрипту не нужно знать про
    него ничего специфичного. Если 'default sink' почему-то не задан
    явно (бывает на некоторых минимальных конфигурациях), в качестве
    запасного варианта берём первый sink из списка."""
    try:
        result = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, check=True
        )
        sink = result.stdout.strip()
        if sink and sink != "@DEFAULT_SINK@":
            return sink
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"WARNING: 'pactl get-default-sink' не сработал ({exc}), пробую резервный способ...",
              file=sys.stderr)

    # Резервный путь: разобрать 'pactl list short sinks' и взять первый.
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True,
        )
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if lines:
            return lines[0].split("\t")[1]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        pass

    raise RuntimeError(
        "Не удалось определить звуковое устройство вывода ни через "
        "'pactl get-default-sink', ни через 'pactl list short sinks'. "
        "Проверьте, что PulseAudio/PipeWire запущены (см. также "
        "--list-audio-devices для диагностики)."
    )


def list_audio_devices() -> None:
    """Диагностика: печатает доступные sink'и (устройства вывода) и то,
    какой из них считается default — полезно при проверке работы на
    другой звуковой карте (см. --list-audio-devices)."""
    try:
        default_sink = get_default_sink_name()
    except Exception as exc:
        default_sink = f"<не удалось определить: {exc}>"
    print(f"Default sink (используется для захвата): {default_sink}")
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True,
        )
        print("Все доступные sink'и (устройства вывода звука):")
        for line in result.stdout.splitlines():
            print(f"  {line}")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"Не удалось получить список sink'ов: {exc}", file=sys.stderr)


def ensure_parec() -> str:
    """Проверяет, что в системе есть 'parec' (пакет pulseaudio-utils / pipewire-pulse).

    Запись идёт через parec -> ffmpeg по трубе (сырой PCM), а не через
    встроенный в ffmpeg вход '-f pulse', так как многие сборки ffmpeg
    (в т.ч. дистрибутивные) собраны без --enable-libpulse и падают с
    ошибкой 'Unknown input format: pulse'.
    """
    path = shutil.which("parec")
    if not path:
        raise RuntimeError(
            "Не найдена утилита 'parec' — она нужна для захвата звука. "
            "Установите пакет с ней, например: sudo apt install pulseaudio-utils "
            "(на системах с PipeWire обычно ставится вместе с pipewire-pulse)."
        )
    return path


# --------------------------------------------------------------------------
# Изоляция звука: запись ТОЛЬКО из нашего Firefox, никаких других
# браузеров/приложений в системе, даже если они играют одновременно.
# --------------------------------------------------------------------------

def _read_ppid(pid: int) -> int | None:
    """Читает PPID процесса из /proc/<pid>/stat. Имя процесса (comm) может
    содержать пробелы и скобки, поэтому парсим от последней ')'."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    rparen = raw.rfind(")")
    if rparen == -1:
        return None
    fields = raw[rparen + 2:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])  # fields[0] = state, fields[1] = ppid
    except ValueError:
        return None


def get_descendant_pids(root_pid: int) -> set[int]:
    """Возвращает root_pid и ВСЕ его процессы-потомки (по дереву процессов).

    Firefox — многопроцессный браузер: звук конкретной вкладки может идти
    не из главного firefox-bin, а из дочернего content-процесса или
    отдельного аудио-декодера (RDD/Utility). Поэтому 'наш Firefox' — это
    не один pid, а всё поддерево процессов, растущее из firefox_pid.
    Список пересобирается заново на каждый вызов, т.к. Firefox создаёт и
    завершает дочерние процессы динамически (новые вкладки, новые треки)."""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return {root_pid}

    children: dict[int, list[int]] = {}
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        ppid = _read_ppid(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)

    result: set[int] = set()
    stack = [root_pid]
    while stack:
        p = stack.pop()
        if p in result:
            continue
        result.add(p)
        stack.extend(children.get(p, []))
    return result


def pactl_list_sink_inputs() -> list[dict]:
    """Парсит 'pactl list sink-inputs' в список {index, sink, pid}."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sink-inputs"], capture_output=True, text=True, check=True,
        )
    except Exception:
        return []
    entries: list[dict] = []
    blocks = re.split(r"(?m)^Sink Input #(\d+)\s*$", result.stdout)
    it = iter(blocks[1:])
    for index_str, body in zip(it, it):
        entry: dict = {"index": int(index_str)}
        sink_match = re.search(r"(?m)^\s*Sink:\s*(\d+)", body)
        if sink_match:
            entry["sink"] = int(sink_match.group(1))
        pid_match = re.search(r'application\.process\.id\s*=\s*"(\d+)"', body)
        if pid_match:
            entry["pid"] = int(pid_match.group(1))
        entries.append(entry)
    return entries


def pactl_sink_name_to_index(sink_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True,
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == sink_name:
            try:
                return int(parts[0])
            except ValueError:
                return None
    return None


def pactl_load_module(module_name: str, *props: str) -> int | None:
    try:
        result = subprocess.run(
            ["pactl", "load-module", module_name, *props],
            capture_output=True, text=True, check=True,
        )
        return int(result.stdout.strip())
    except Exception:
        return None


def pactl_unload_module(module_id: int) -> None:
    try:
        subprocess.run(["pactl", "unload-module", str(module_id)], capture_output=True, text=True)
    except Exception:
        pass


def pactl_move_sink_input(index: int, sink_name: str) -> bool:
    try:
        subprocess.run(
            ["pactl", "move-sink-input", str(index), sink_name],
            capture_output=True, text=True, check=True,
        )
        return True
    except Exception:
        return False


class FirefoxAudioIsolator:
    """
    Гарантирует, что parec физически может захватить ТОЛЬКО звук процесса
    firefox-bin, запущенного этим скриптом, и его потомков (вкладки,
    контент-процессы, аудио-декодер) — и ничего больше. Обычный
    '<default_sink>.monitor' захватывает смешанный звук ВСЕХ приложений в
    системе (в т.ч. другого браузера типа Chromium, системных звуков и
    т.п.) — этого недостаточно, когда требуется гарантия изоляции.

    Метод — маршрутизация на уровне звукового сервера, а не фильтрация
    после записи (после записи уже поздно: чужой звук физически
    смешивается на уровне PulseAudio/PipeWire ДО того, как до него
    добирается parec):
      1. создаётся отдельный виртуальный null-sink, не подключённый ни к
         какому реальному устройству;
      2. аудио-поток(и) именно нашего firefox-bin (и только его потомков)
         принудительно переносятся (move-sink-input) на этот null-sink —
         фоновый поток делает это непрерывно, чтобы подхватывать новые
         потоки (Suno создаёт новый <audio> на каждый трек);
      3. чтобы пользователь по-прежнему слышал звук из Suno через колонки
         как обычно, monitor этого null-sink зацикливается обратно на
         исходное устройство вывода (module-loopback);
      4. parec записывает monitor именно этого null-sink — физически
         никакое другое приложение туда попасть не может.

    Если создать null-sink не получилось — считаем это фатальной ошибкой
    и НЕ пытаемся откатиться на запись системного default-monitor, чтобы
    не рисковать записью чужого звука в нарушение требования изоляции.
    """

    def __init__(self, firefox_root_pid: int, poll_interval: float = 0.5):
        self.firefox_root_pid = firefox_root_pid
        self.poll_interval = poll_interval
        self.null_sink_name = f"auto_record_suno_capture_{os.getpid()}"
        self.null_sink_index: int | None = None
        self._null_sink_module: int | None = None
        self._loopback_module: int | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.original_default_sink: str | None = None

    def start(self) -> str:
        """Настраивает изоляцию, возвращает monitor-источник для parec
        (monitor нашего null-sink'а, НЕ системный default sink!)."""
        try:
            self.original_default_sink = get_default_sink_name()
        except Exception:
            self.original_default_sink = None

        self._null_sink_module = pactl_load_module(
            "module-null-sink",
            f"sink_name={self.null_sink_name}",
            "sink_properties=device.description=AutoRecordSunoCapture",
        )
        if self._null_sink_module is None:
            raise RuntimeError(
                "Не удалось создать виртуальный null-sink для изоляции звука "
                "Firefox (pactl load-module module-null-sink). Без этого "
                "скрипт не может гарантировать, что запись не захватит звук "
                "других приложений, поэтому он отказывается продолжать. "
                "Проверьте, что pactl/PulseAudio-PipeWire работают "
                "(попробуйте вручную: pactl load-module module-null-sink)."
            )

        for _ in range(20):  # индекс sink'а появляется не мгновенно
            self.null_sink_index = pactl_sink_name_to_index(self.null_sink_name)
            if self.null_sink_index is not None:
                break
            time.sleep(0.1)

        if self.original_default_sink:
            self._loopback_module = pactl_load_module(
                "module-loopback",
                f"source={self.null_sink_name}.monitor",
                f"sink={self.original_default_sink}",
                "latency_msec=20",
            )
            if self._loopback_module is None:
                print(
                    "WARNING: не удалось создать module-loopback обратно на "
                    "основное устройство вывода — во время записи вы не "
                    "будете слышать звук из Suno через колонки (на саму "
                    "запись это не влияет).",
                    file=sys.stderr,
                )
        else:
            print(
                "WARNING: не удалось определить sink по умолчанию — "
                "пропускаю loopback (звук из Suno не будет слышен через "
                "колонки во время записи, запись при этом не пострадает).",
                file=sys.stderr,
            )

        self._thread = threading.Thread(target=self._router_loop, daemon=True)
        self._thread.start()
        # Дать роутеру хотя бы один цикл, чтобы перенести уже существующие
        # (если вдруг Firefox успел создать их до вызова start()) потоки.
        time.sleep(self.poll_interval)

        return f"{self.null_sink_name}.monitor"

    def _router_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._route_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval)

    def _route_once(self) -> None:
        allowed_pids = get_descendant_pids(self.firefox_root_pid)
        for entry in pactl_list_sink_inputs():
            pid = entry.get("pid")
            if pid is None or pid not in allowed_pids:
                continue  # НЕ наш Firefox — не трогаем ни в коем случае
            if self.null_sink_index is not None and entry.get("sink") == self.null_sink_index:
                continue  # уже там, где надо — лишний move-sink-input не нужен
            pactl_move_sink_input(entry["index"], self.null_sink_name)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._loopback_module is not None:
            pactl_unload_module(self._loopback_module)
        if self._null_sink_module is not None:
            pactl_unload_module(self._null_sink_module)


def _ffmpeg_has_mp3(ffmpeg_path: str) -> bool:
    """Проверяет, есть ли в данном бинарнике ffmpeg кодек libmp3lame."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(re.search(r"\blibmp3lame\b", result.stdout))


def _download_static_ffmpeg_linux() -> str:
    """
    Скачивает статическую GPL-сборку ffmpeg (johnvansickle.com), которая
    включает libmp3lame, и кеширует бинарник в FFMPEG_CACHE_DIR. Linux-only
    (как и остальная автоматика с /proc в этом скрипте).
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("arm"):
        arch = "armhf"
    else:
        raise RuntimeError(f"Автозагрузка статического ffmpeg не поддерживает архитектуру {machine}.")

    url = f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
    print(f"Скачиваю статическую сборку ffmpeg (с поддержкой mp3) для {arch}: {url}")

    FFMPEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = FFMPEG_CACHE_DIR / f"ffmpeg-release-{arch}-static.tar.xz"
    urllib.request.urlretrieve(url, archive_path)

    with tarfile.open(archive_path) as tf:
        try:
            tf.extractall(FFMPEG_CACHE_DIR, filter="data")
        except TypeError:
            tf.extractall(FFMPEG_CACHE_DIR)  # Python < 3.12 без параметра filter

    archive_path.unlink(missing_ok=True)

    extracted_dirs = sorted(FFMPEG_CACHE_DIR.glob("ffmpeg-*-static"))
    if not extracted_dirs:
        raise RuntimeError("После распаковки не найдена папка со статической сборкой ffmpeg.")

    src_binary = extracted_dirs[-1] / "ffmpeg"
    if not src_binary.exists():
        raise RuntimeError(f"В распакованном архиве не найден бинарник ffmpeg: {src_binary}")

    dest_binary = FFMPEG_CACHE_DIR / "ffmpeg"
    shutil.copy2(src_binary, dest_binary)
    dest_binary.chmod(dest_binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    shutil.rmtree(extracted_dirs[-1], ignore_errors=True)

    print(f"ffmpeg (mp3) готов: {dest_binary}")
    return str(dest_binary)


def ensure_ffmpeg_with_mp3() -> str:
    """Возвращает путь к закешированному (или только что скачанному) ffmpeg с libmp3lame."""
    cached_binary = FFMPEG_CACHE_DIR / "ffmpeg"
    if cached_binary.exists() and _ffmpeg_has_mp3(str(cached_binary)):
        return str(cached_binary)

    binary = _download_static_ffmpeg_linux()
    if not _ffmpeg_has_mp3(binary):
        raise RuntimeError("Скачанная статическая сборка ffmpeg неожиданно не содержит libmp3lame.")
    return binary


# Какой конкретно энкодер нужен ffmpeg для каждого выбираемого в меню
# audio-контейнера — используется, чтобы решить, хватает ли системного
# ffmpeg, или нужно подтягивать статическую сборку (ensure_ffmpeg_with_mp3
# на самом деле тянет полную GPL-сборку с mp3/aac/vorbis/flac разом, имя
# оставлено историческим).
_CONTAINER_REQUIRED_ENCODER = {
    "mp3": "libmp3lame",
    "aac": None,   # 'aac' встроен почти всегда; libfdk_aac определяется отдельно, не блокирует
    "ogg": "libvorbis",
    "flac": "flac",
}


def _ffmpeg_has_encoder(ffmpeg_path: str, encoder_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(re.search(rf"\b{re.escape(encoder_name)}\b", result.stdout))


def resolve_ffmpeg_bin_for_encoder(
    required_encoder: str | None,
    *,
    auto_download: bool = True,
    strict: bool = False,
) -> str:
    """Универсальная версия подбора ffmpeg: принимает явное имя энкодера
    (а не ключ контейнера), поэтому годится и для аудио-контейнеров
    (см. resolve_ffmpeg_bin ниже — тонкая обёртка над этой функцией), и
    для видео-контейнеров (используется из auto_record_youtube.py, у
    которого свой набор контейнеров/кодеков — libx264/libvpx-vp9).

    1. Если у системного ffmpeg уже есть нужный кодек — используем его.
    2. Иначе, если разрешено (auto_download) и мы на Linux — качаем и
       кешируем статическую GPL-сборку ffmpeg (в ней есть разом
       mp3/aac/vorbis/flac И libx264/libvpx-vp9 — это полная сборка,
       несмотря на историческое имя функции ensure_ffmpeg_with_mp3).
    3. Если и это не удалось:
       - при strict=False (поведение по умолчанию, как раньше для
         аудио) — тихий откат на системный ffmpeg с предупреждением,
         пусть сам ffmpeg выдаст ошибку, если это критично;
       - при strict=True (используется для видео) — явная ошибка с
         понятной причиной, а не откат на заведомо нерабочий кодек.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and (required_encoder is None or _ffmpeg_has_encoder(system_ffmpeg, required_encoder)):
        return system_ffmpeg

    downloaded = None
    download_error: Exception | None = None
    if auto_download and platform.system() == "Linux":
        try:
            downloaded = ensure_ffmpeg_with_mp3()
            if required_encoder is None or _ffmpeg_has_encoder(downloaded, required_encoder):
                return downloaded
        except Exception as exc:
            download_error = exc
            print(f"WARNING: не удалось получить ffmpeg с нужным кодеком автоматически ({exc}).", file=sys.stderr)

    if strict:
        details = []
        if system_ffmpeg:
            details.append(f"в системном ffmpeg ({system_ffmpeg}) он отсутствует")
        else:
            details.append("системный ffmpeg не найден в PATH")
        if downloaded:
            details.append(f"в автоматически загруженной сборке ({downloaded}) он тоже отсутствует")
        elif download_error is not None:
            details.append(f"автозагрузка статической сборки ffmpeg не удалась ({download_error})")
        elif not auto_download:
            details.append("автозагрузка отключена флагом --no-auto-ffmpeg")
        elif platform.system() != "Linux":
            details.append("автозагрузка статической сборки поддерживается только на Linux")
        raise RuntimeError(
            f"Не удалось найти или получить ffmpeg с кодеком '{required_encoder}': " + "; ".join(details) + "."
        )

    if system_ffmpeg:
        print(
            f"WARNING: в системном ffmpeg не найден кодек '{required_encoder}' — "
            f"попробую всё равно, ffmpeg сам выдаст ошибку, если это критично.",
            file=sys.stderr,
        )
        return system_ffmpeg

    raise RuntimeError("В PATH не найден ffmpeg, и автозагрузка не удалась/отключена.")


def resolve_ffmpeg_bin(container_id: str | None, auto_download: bool = True) -> str:
    """Возвращает путь к ffmpeg, у которого точно есть энкодер, нужный для
    выбранного пользователем audio-контейнера (или любой ffmpeg, если
    контейнер не выбран — тогда кодирование вообще не требуется, только
    PCM WAV). Тонкая обёртка над resolve_ffmpeg_bin_for_encoder, см. её
    докстринг за подробностями."""
    required_encoder = _CONTAINER_REQUIRED_ENCODER.get(container_id) if container_id else None
    return resolve_ffmpeg_bin_for_encoder(required_encoder, auto_download=auto_download, strict=False)


_PROFILE_COPY_SKIP_NAMES = {
    "lock", ".parentlock", "parent.lock",
    "sessionstore.jsonlz4", "sessionstore.js",
    "sessionstore-backups", "sessionCheckpoints.json",
}

_PROFILE_COPY_USER_JS_OVERRIDES = """
// --- auto_record_suno: чистый старт без восстановления вкладок ---
user_pref("browser.startup.page", 1);
user_pref("browser.startup.homepage", "about:blank");
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_tabs_undo", 0);
user_pref("browser.warnOnQuit", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.tabs.warnOnCloseOtherTabs", false);
"""


def _copy_firefox_profile_into(source_profile: str, dest: Path) -> None:
    """
    Общее ядро копирования профиля, используемое и для одноразовой
    временной копии (make_profile_copy), и для постоянной копии в кэше
    (save_firefox_profile_to_cache). Логин, куки, localStorage/IndexedDB и
    настройки на момент копирования сохраняются; сама копируемая
    (исходная) папка профиля не затрагивается.

    Данные сессии (открытые/закреплённые вкладки — sessionstore.jsonlz4 и
    его бэкапы) намеренно НЕ копируются: иначе Firefox при старте
    попытался бы восстановить все вкладки исходного профиля. Поверх копии
    дописывается user.js, который форсирует чистый старт с одной пустой
    вкладкой независимо от настроек исходного профиля (user.js
    применяется поверх prefs.js при каждом запуске и не меняет сам
    скопированный prefs.js).
    """
    src = Path(source_profile)

    def ignore(_dir, names):
        return [n for n in names if n in _PROFILE_COPY_SKIP_NAMES]

    shutil.copytree(src, dest, ignore=ignore, symlinks=False, dirs_exist_ok=True)
    with open(dest / "user.js", "a", encoding="utf-8") as f:
        f.write(_PROFILE_COPY_USER_JS_OVERRIDES)


def make_profile_copy(source_profile: str) -> str:
    """
    Копирует профиль во временную (одноразовую, на этот запуск) папку,
    чтобы обойти блокировку профиля (Firefox не даёт открыть один и тот же
    профиль вторым процессом — 'profile in use'/HTTP 500 при создании
    сессии geckodriver). См. _copy_firefox_profile_into для деталей того,
    что именно копируется.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="auto_record_suno_profile_"))
    dest = tmp_dir / "profile"
    _copy_firefox_profile_into(source_profile, dest)
    return str(dest)


# --------------------------------------------------------------------------
# Постоянный кэш профиля/бинарника Firefox (см. FIREFOX_PROFILE_CACHE_DIR /
# FIREFOX_BINARY_CACHE_FILE выше)
# --------------------------------------------------------------------------

def save_firefox_binary_to_cache(firefox_binary: str) -> None:
    """Запоминает путь, откуда в этот раз был запущен firefox-bin, чтобы в
    следующий раз не нужно было искать/дожидаться уже запущенного
    процесса — можно сразу стартовать его же по сохранённому пути."""
    FIREFOX_BINARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIREFOX_BINARY_CACHE_FILE.write_text(firefox_binary, encoding="utf-8")


def load_cached_firefox_binary() -> str | None:
    """Возвращает сохранённый ранее путь к firefox-bin, если файл есть и
    (что важно) сам бинарник по этому пути всё ещё существует — например,
    Firefox мог обновиться/переехать, тогда кэш молча игнорируется и
    скрипт вернётся к обычному поиску."""
    try:
        path = FIREFOX_BINARY_CACHE_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if path and Path(path).is_file():
        return path
    return None


def save_firefox_profile_to_cache(source_profile: str) -> str:
    """
    Сохраняет ПОСТОЯННУЮ копию профиля в FIREFOX_PROFILE_CACHE_DIR — в
    отличие от make_profile_copy(), эта копия не удаляется после
    завершения работы и переиспользуется на последующих запусках напрямую
    (см. get_cached_firefox_profile), так что копировать профиль заново
    каждый раз больше не требуется.

    Копия строится во временной папке рядом с кэшем и атомарно подменяет
    предыдущую (os.replace на директорию) — чтобы в случае сбоя
    посередине копирования не остался наполовину скопированный,
    нерабочий кэш.
    """
    FIREFOX_PROFILE_CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    tmp_new = Path(tempfile.mkdtemp(
        prefix="firefox_profile_cache_new_", dir=str(FIREFOX_PROFILE_CACHE_DIR.parent),
    ))
    dest = tmp_new / "profile"
    try:
        _copy_firefox_profile_into(source_profile, dest)
        if FIREFOX_PROFILE_CACHE_DIR.exists():
            shutil.rmtree(FIREFOX_PROFILE_CACHE_DIR)
        os.replace(dest, FIREFOX_PROFILE_CACHE_DIR)
    finally:
        shutil.rmtree(tmp_new, ignore_errors=True)
    return str(FIREFOX_PROFILE_CACHE_DIR)


def get_cached_firefox_profile() -> str | None:
    """Возвращает путь к постоянной копии профиля в кэше, если она есть и
    выглядит как настоящий профиль Firefox (а не пустая/битая папка)."""
    if not FIREFOX_PROFILE_CACHE_DIR.is_dir():
        return None
    if not (FIREFOX_PROFILE_CACHE_DIR / "prefs.js").is_file():
        return None
    # Этот кэш-профиль эксклюзивно используется только нашим скриптом (это
    # не основной профиль пользователя), поэтому здесь безопасно снять
    # возможный "зависший" lock/.parentlock, оставшийся после аварийного
    # завершения предыдущего запуска (например, kill -9/сбой питания) —
    # иначе Firefox отказался бы стартовать с "profile in use".
    for lock_name in ("lock", ".parentlock", "parent.lock"):
        try:
            (FIREFOX_PROFILE_CACHE_DIR / lock_name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return str(FIREFOX_PROFILE_CACHE_DIR)


def clear_firefox_cache() -> None:
    """Полностью сбрасывает кэш профиля/бинарника — следующий запуск снова
    потребует уже запущенный основной firefox-bin, из которого кэш будет
    пересоздан заново (--reset-firefox-cache в CLI)."""
    shutil.rmtree(FIREFOX_PROFILE_CACHE_DIR, ignore_errors=True)
    try:
        FIREFOX_BINARY_CACHE_FILE.unlink()
    except FileNotFoundError:
        pass


def resolve_firefox_launch_plan(
    explicit_profile: str | None,
    explicit_binary: str | None,
    wait_timeout: float = 60.0,
    use_cache: bool = True,
) -> tuple[str, str, int | None, bool]:
    """
    Более высокоуровневая обёртка над resolve_firefox_binary_and_profile():
    сначала пробует воспользоваться сохранённым кэшем (постоянная копия
    профиля + запомненный путь к firefox-bin), и только если кэша ещё нет
    (первый запуск) или он не прошёл валидацию — падает обратно на старое
    поведение (дождаться уже запущенного основного firefox-bin) и потом
    сохраняет результат в кэш для всех последующих запусков.

    Возвращает (profile_path, firefox_binary, pending_close_pid,
    use_profile_copy) — последнее говорит вызывающему коду (GeckoSession),
    нужно ли ещё раз копировать профиль перед запуском: если профиль уже
    взят из постоянного кэша — не нужно, это и так отдельная выделенная
    копия, использовать её можно напрямую.

    Если явно заданы --profile/--firefox-binary, кэш не используется —
    в этом случае пользователь сам управляет тем, что запускать.
    """
    if not explicit_profile and not explicit_binary and use_cache:
        cached_binary = load_cached_firefox_binary()
        cached_profile = get_cached_firefox_profile()
        if cached_binary and cached_profile:
            print(f"Использую сохранённую копию профиля Firefox: {cached_profile}")
            print(f"Использую сохранённый путь к firefox-bin: {cached_binary}")
            print(
                "(Запущенный основной firefox-bin для этого не требуется — "
                "используйте --no-firefox-cache, если нужно принудительно "
                "определить профиль/бинарник заново.)"
            )
            return cached_profile, cached_binary, None, False

    profile_path, firefox_binary, pending_close_pid = resolve_firefox_binary_and_profile(
        explicit_profile, explicit_binary, wait_timeout=wait_timeout,
    )

    if not explicit_profile and use_cache:
        try:
            cached_dir = save_firefox_profile_to_cache(profile_path)
            save_firefox_binary_to_cache(firefox_binary)
            print(
                f"Профиль сохранён в кэш ({cached_dir}) — при следующем запуске "
                "открытый основной Firefox уже не потребуется."
            )
        except OSError as exc:
            print(f"WARNING: не удалось сохранить профиль/бинарник в кэш ({exc}).", file=sys.stderr)

    return profile_path, firefox_binary, pending_close_pid, True


# --------------------------------------------------------------------------
# Минимальный WebDriver-клиент через HTTP (замена selenium)
# --------------------------------------------------------------------------

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """Как wait_for_port в v40.py — ждёт, пока порт начнёт принимать соединения."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def http_post(url: str, payload: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            err = parsed.get("value", {})
            detail = f"{err.get('error', '')}: {err.get('message', '')}"
        except Exception:
            pass
        raise RuntimeError(f"geckodriver HTTP {exc.code}: {detail}") from None


def http_get(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_delete(url: str, timeout: float = 10.0) -> None:
    req = urllib.request.Request(url, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Проверка доступности интернета
# --------------------------------------------------------------------------

_NETWORK_ERROR_MARKERS = (
    "dnsnotfound", "neterror", "unknown host", "net::err",
    "name_not_resolved", "econnrefused", "connection refused",
    "connection reset", "network is unreachable", "network changed",
    "temporary failure in name resolution", "not have a connection",
    "no internet", "мы не можем подключиться",
)


def _looks_like_network_error(exc: BaseException) -> bool:
    """Грубая эвристика: похожа ли ошибка на проблему с интернет-соединением
    (а не, например, на баг в самом скрипте или ошибку ffmpeg)."""
    text = str(exc).lower()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def is_internet_available(timeout: float = 3.0) -> bool:
    """Пробует установить TCP-соединение с несколькими надёжными хостами."""
    probes = [("1.1.1.1", 443), ("8.8.8.8", 443), ("suno.com", 443)]
    for host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def wait_for_internet(poll_interval: float = 5.0) -> None:
    """Блокируется и периодически проверяет соединение, пока интернет не
    появится снова. Оповещает пользователя один раз при начале ожидания."""
    if is_internet_available():
        return
    print(
        "ВНИМАНИЕ: похоже, отсутствует подключение к интернету. "
        f"Жду восстановления соединения (проверка каждые {poll_interval:.0f}с)...",
        file=sys.stderr,
    )
    while not is_internet_available():
        time.sleep(poll_interval)
    print("Соединение с интернетом восстановлено, продолжаю работу.")


def navigate_with_retry(session: "GeckoSession", url: str) -> None:
    """Как session.navigate(), но при явной сетевой ошибке (DNS/нет сети)
    не прерывает работу скрипта, а сообщает об этом пользователю, ждёт
    восстановления интернета и повторяет попытку перехода."""
    while True:
        try:
            session.navigate(url)
            return
        except RuntimeError as exc:
            if _looks_like_network_error(exc):
                print(
                    f"Не удалось открыть страницу ({exc}). Проверяю доступность интернета...",
                    file=sys.stderr,
                )
                wait_for_internet()
                print("Повторяю попытку перехода по ссылке...")
                continue
            raise


class GeckoSession:
    """Тонкая обёртка над geckodriver HTTP API — то, что делает selenium, но напрямую."""

    def __init__(self, profile_path: str, headless: bool, firefox_binary: str,
                 pending_close_pid: int | None = None, use_profile_copy: bool = True,
                 startup_timeout: float = 30.0):
        geckodriver_path = ensure_geckodriver()
        print(f"Использую бинарник Firefox: {firefox_binary}")

        self._profile_copy_dir: Path | None = None
        run_profile = profile_path
        if use_profile_copy:
            print("Копирую профиль во временную папку (чтобы не закрывать основной Firefox)...")
            run_profile = make_profile_copy(profile_path)
            self._profile_copy_dir = Path(run_profile).parent
        self.run_profile = run_profile

        # Найденный (см. resolve_firefox_binary_and_profile) уже запущенный
        # firefox-bin закрываем ТОЛЬКО СЕЙЧАС — уже ПОСЛЕ того, как профиль
        # скопирован (если use_profile_copy=True) или уже не нужен исходным
        # (если используется профиль напрямую) — чтобы не потерять исходную
        # папку профиля до копирования на нестандартных/портативных сборках
        # Firefox, которые подчищают её при закрытии.
        if pending_close_pid is not None:
            print(
                "Аккуратно завершаю найденный процесс firefox-bin — дальше "
                "будет запущен собственный экземпляр Firefox с временным "
                "профилем, основной профиль пользователя при этом не "
                "затрагивается."
            )
            _terminate_pid_gracefully(pending_close_pid, kill_tree=True)
            print("Процесс firefox-bin закрыт.")
            _debug_dump_firefox_bin_processes("сразу после попытки закрытия исходного процесса, до старта новой копии")

        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [geckodriver_path, "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._wait_ready()

        firefox_args = ["-profile", run_profile]
        if headless:
            firefox_args.append("--headless")

        payload = {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "firefox",
                    "moz:firefoxOptions": {"args": firefox_args, "binary": firefox_binary},
                }
            }
        }
        resp = http_post(f"{self.base_url}/session", payload, timeout=60.0)
        self.session_id = resp["value"]["sessionId"]

        print("Жду, пока реально поднимется процесс firefox-bin...")
        pid = wait_for_firefox_bin(run_profile, timeout=startup_timeout)
        print(f"firefox-bin запущен (pid {pid}).")
        _debug_dump_firefox_bin_processes("сразу после подтверждённого старта новой копии")
        # Понадобится для изоляции звука: захват должен видеть ТОЛЬКО этот
        # процесс и его потомков (вкладки/контент-процессы/аудио-декодер),
        # и никакие другие браузеры/приложения в системе.
        self.firefox_pid = pid

        # Подстраховка: основной изначальный (родительский) процесс
        # firefox-bin с основным профилем пользователя к этому моменту уже
        # должен быть закрыт (см. pending_close_pid выше). Но если по каким-то
        # причинам (гонка, найденный ранее pid оказался дочерним
        # content-процессом и т.п.) он всё ещё жив ПОСЛЕ того, как поднялся
        # наш новый firefox-bin с временным профилем — это ненормально и его
        # нужно аккуратно закрыть. Даём 10 секунд (вдруг он и сам вот-вот
        # закроется) и только потом завершаем именно его — по факту
        # удержания лока основного профиля, чтобы точно не задеть наш новый
        # процесс (self.firefox_pid) или чужие content-процессы.
        if profile_path != run_profile:
            leftover_pid = _find_locked_profile_pid(profile_path, exclude_pid=self.firefox_pid)
            if leftover_pid is not None:
                print(
                    f"Обнаружен всё ещё работающий исходный процесс firefox-bin "
                    f"(pid {leftover_pid}) с основным профилем пользователя — "
                    f"этого быть не должно. Жду 10с и аккуратно завершаю его..."
                )
                time.sleep(10.0)
                # Перепроверяем на случай, если он успел закрыться сам за
                # эти 10с, или лок основного профиля уже перехватил другой
                # процесс (например, если пользователь сам открыл Firefox
                # заново) — тогда трогать его не нужно.
                if _find_locked_profile_pid(profile_path, exclude_pid=self.firefox_pid) == leftover_pid:
                    _terminate_pid_gracefully(leftover_pid, kill_tree=True)
                    print(f"Исходный процесс firefox-bin (pid {leftover_pid}) закрыт.")

    def _wait_ready(self, timeout: float = 30.0) -> None:
        """Как launch_firefox в v40.py: ждём порт + отдельно проверяем, что процесс
        не упал раньше времени (например, если geckodriver уже занят другим портом
        или Firefox не смог запуститься с этим профилем)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                stderr = ""
                try:
                    if self.proc.stderr:
                        stderr = self.proc.stderr.read().decode("utf-8", "replace")
                except Exception:
                    pass
                raise RuntimeError(
                    f"geckodriver завершился с кодом {self.proc.returncode}"
                    + (f": {stderr[-1200:]}" if stderr else "")
                )
            if wait_for_port("127.0.0.1", self.port, timeout=0.25):
                try:
                    status = http_get(f"{self.base_url}/status", timeout=1.0)
                    if status.get("value", {}).get("ready", True):
                        return
                except Exception:
                    pass
            time.sleep(0.15)
        raise TimeoutError(f"geckodriver на 127.0.0.1:{self.port} не поднялся вовремя.")

    def navigate(self, url: str) -> None:
        http_post(f"{self.base_url}/session/{self.session_id}/url", {"url": url}, timeout=60.0)

    def execute_script(self, script: str, args: list | None = None):
        resp = http_post(
            f"{self.base_url}/session/{self.session_id}/execute/sync",
            {"script": script, "args": args or []},
            timeout=30.0,
        )
        return resp.get("value")

    def quit(self) -> None:
        http_delete(f"{self.base_url}/session/{self.session_id}")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self._profile_copy_dir is not None:
            shutil.rmtree(self._profile_copy_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Логика проигрывания/записи (эквивалент того, что раньше делал selenium)
# --------------------------------------------------------------------------

def prime_and_wait_for_playback(session: GeckoSession, playback_rate: float, poll_interval: float = 0.3, max_wait: float = 600.0) -> dict:
    """
    Автообнаружение трека вместо ручного URL:
    на каждом опросе выставляет playbackRate/preservesPitch любому найденному
    audio/video (в том числе новым, если пользователь переключил трек), и
    возвращает информацию о плеере, как только он реально начал играть
    (paused: false) — это и есть момент старта записи.
    """
    script = """
    const el = document.querySelector('audio,video');
    if (!el) return null;
    el.playbackRate = arguments[0];
    el.preservesPitch = true;
    let title = '';
    try { title = (navigator.mediaSession && navigator.mediaSession.metadata && navigator.mediaSession.metadata.title) || document.title || ''; } catch (_) {}
    return {
        duration: Number.isFinite(el.duration) ? el.duration : -1,
        readyState: el.readyState,
        paused: !!el.paused,
        src: el.currentSrc || el.src || '',
        title
    };
    """
    deadline = time.monotonic() + max_wait
    printed_waiting = False
    while time.monotonic() < deadline:
        info = session.execute_script(script, [playback_rate])
        if info is not None and info.get("readyState", 0) >= 1:
            if not info.get("paused", True):
                return info
            if not printed_waiting:
                print("Плеер найден, ускорение выставлено. Жду нажатия play (можно нажать самому в браузере)...")
                printed_waiting = True
        time.sleep(poll_interval)
    raise TimeoutError("Не дождался начала воспроизведения трека.")


class RecordingGate:
    """
    Потокобезопасный "вентиль" записи: определяет, должны ли байты,
    прочитанные из parec, физически попадать в файл записи прямо сейчас.

    Это и есть механизм физической остановки/продолжения записи в wav:
    - close() — с этого момента поступающий звук в файл НЕ пишется
      (реальная, физическая пауза записи, а не пометка для вырезания
      постфактум);
    - open()  — запись в файл возобновляется с этого момента.

    Почему это надёжнее наивного SIGSTOP у parec (так было в одной из
    более старых версий и не сработало): SIGSTOP останавливает только
    процесс-читатель, но не отключает сам поток захвата на уровне
    звукового сервера — PulseAudio/PipeWire продолжает буферизовать то,
    что физически звучит на устройстве, и после SIGCONT эта накопленная
    буферизация может "вылиться" в файл разом (в т.ч. чужой звук, если
    что-то звучало на устройстве в момент паузы).

    Здесь же parec НИКОГДА не останавливается и не замораживается — он
    продолжает непрерывно писать поток в свой stdout (это и держит
    захват "живым"), а решение писать байты в итоговый wav или молча
    отбросить их принимается в отдельном потоке-ретрансляторе
    (см. _relay_audio_to_encoder) на лету, байт за байтом. Отброшенные
    во время паузы байты нигде не накапливаются и не "утекают" в файл
    после возобновления — то есть никакого постфактум-вырезания частей
    файла (как раньше через trim_audio_cuts) больше не требуется.
    """

    def __init__(self, initially_open: bool = True) -> None:
        self._event = threading.Event()
        if initially_open:
            self._event.set()

    def open(self) -> None:
        self._event.set()

    def close(self) -> None:
        self._event.clear()

    def is_open(self) -> bool:
        return self._event.is_set()


def _relay_audio_to_encoder(
    parec_stdout, ffmpeg_stdin, gate: RecordingGate, stop_event: threading.Event,
    chunk_size: int = 4096,
    frame_size: int = RAW_CAPTURE_FRAME_SIZE,
) -> None:
    """
    Работает в отдельном потоке всё время записи трека: непрерывно читает
    сырой PCM из stdout parec (тем самым не давая parec ни на миг
    остановиться/зависнуть на буфере) и передаёт эти байты в stdin ffmpeg
    ТОЛЬКО пока gate.is_open() — то есть пока плеер реально проигрывает
    трек. Пока gate закрыт (пауза), прочитанные байты просто
    отбрасываются — в файл они не попадают ни в каком виде.

    ВАЖНО про выравнивание по фреймам: read() из пайпа НЕ обязан
    возвращать данные, выровненные по границе сэмпл-фрейма (для
    s24le/2ch фрейм — 6 байт: 3 байта * 2 канала), а chunk_size=4096 сам
    по себе на 6 не делится. Если писать/отбрасывать байты как попало,
    то после любого закрытия gate (пауза) на кол-во байт, не кратное
    frame_size, вся дальнейшая передаваемая ffmpeg последовательность
    "съезжает по фазе" относительно 6-байтных фреймов — именно это
    вызывает ffmpeg-ошибки вида "Invalid PCM packet, data has size N
    but at least a size of 6 was expected". Поэтому здесь ведётся
    буфер remainder — недописанный "хвост" последнего неполного
    фрейма, который переносится к следующему чтению, а в ffmpeg (или в
    никуда, при закрытом gate) всегда уходит строго целое число полных
    фреймов.
    """
    remainder = b""
    try:
        while not stop_event.is_set():
            chunk = parec_stdout.read(chunk_size)
            if not chunk:
                break  # parec завершился (например, его terminate() уже вызвали)
            data = remainder + chunk
            usable_len = (len(data) // frame_size) * frame_size
            usable, remainder = data[:usable_len], data[usable_len:]
            if usable and gate.is_open():
                try:
                    ffmpeg_stdin.write(usable)
                except (BrokenPipeError, OSError):
                    break
    finally:
        try:
            ffmpeg_stdin.close()
        except Exception:
            pass


def wait_for_track_boundary(
    session: GeckoSession,
    track_src: str,
    timeout: float,
    gate: RecordingGate,
    poll_interval: float = 0.3,
    resume_confirm_polls: int = 2,
    resume_time_epsilon: float = 0.01,
) -> str:
    """
    Следит за состоянием плеера, пока не наступит одно из событий:
      - "ended"         — текущий трек доиграл до конца;
      - "track_changed" — реально начал играть ДРУГОЙ трек (сменился src) —
                           сигнал закончить текущую запись и начать новую;
      - "timeout"        — не дождались ни того, ни другого за отведённое время.

    По ходу дела физически управляет записью через gate: как только плеер
    ставится на паузу — gate.close() (запись в wav физически
    останавливается), как только воспроизведение реально продолжается —
    gate.open() (запись физически возобновляется с этого момента, без
    "дыры" тишины и без склеивания постфактум).

    Реальность звучания определяется не по el.paused напрямую, а по
    факту, что currentTime элемента продвигается вперёд несколько
    опросов подряд — сразу после нажатия "продолжить" el.paused уже
    false, но звук физически может пойти на 1-2 опроса позже
    (буферизация). Запись возобновляется (gate.open()) только когда
    звучание подтвердилось, поэтому в файл не попадает и эта короткая
    тишина буферизации.
    """
    script = """
    const el = document.querySelector('audio,video');
    if (!el) return null;
    return {
        ended: !!el.ended,
        paused: !!el.paused,
        src: el.currentSrc || el.src || '',
        currentTime: el.currentTime
    };
    """
    deadline = time.monotonic() + timeout + 5
    last_current_time: float | None = None
    advancing_streak = 0

    while time.monotonic() < deadline:
        state = session.execute_script(script)
        if state is None:
            time.sleep(poll_interval)
            continue

        if state.get("ended"):
            return "ended"

        current_src = state.get("src", "")
        current_time = state.get("currentTime")
        user_paused = bool(state.get("paused"))

        # Реальный признак того, что звук физически звучит прямо сейчас:
        # currentTime продвинулся с прошлого опроса. Во время паузы и во
        # время буферизации после нажатия "продолжить" currentTime стоит
        # на месте, даже если el.paused уже false.
        time_advanced = (
            last_current_time is not None
            and current_time is not None
            and (current_time - last_current_time) > resume_time_epsilon
        )
        last_current_time = current_time

        if not user_paused and time_advanced:
            advancing_streak += 1
        else:
            advancing_streak = 0

        really_playing = not user_paused and advancing_streak >= resume_confirm_polls

        if really_playing and track_src and current_src and current_src != track_src:
            # Реально заиграл другой трек (не просто снятие с паузы того же).
            return "track_changed"

        if user_paused:
            if gate.is_open():
                gate.close()
                print("Воспроизведение поставлено на паузу — запись в wav физически остановлена...")
            # Время на паузе не должно "съедать" таймаут ожидания.
            deadline += poll_interval
        elif not gate.is_open():
            if really_playing:
                gate.open()
                print("Воспроизведение продолжено — запись в wav физически возобновлена...")
            else:
                # play() уже вызван, но звук ещё физически не пошёл
                # (буферизация) — запись остаётся на паузе, таймаут не тратим.
                deadline += poll_interval

        time.sleep(poll_interval)

    print("WARNING: таймаут ожидания окончания/смены трека — останавливаю запись по расчётному времени.", file=sys.stderr)
    return "timeout"


def probe_wav_duration(ffmpeg_bin: str, path: Path) -> float | None:
    """Достаёт длительность файла из вывода 'ffmpeg -i <path>' (без ffprobe)."""
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True,
        )
    except Exception:
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

class _PaSampleSpec(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


_PA_SAMPLE_S16LE = 3      # pa_sample_format_t
_PA_STREAM_PLAYBACK = 1   # pa_stream_direction_t


class SilenceKeepalive:
    """
    Держит указанный sink "живым" на всё время работы скрипта, проигрывая
    в него непрерывный поток цифровой тишины напрямую через системную
    библиотеку libpulse-simple (без запуска внешних программ вроде
    'paplay' и без какой-либо загрузки чего-либо из интернета — эта
    библиотека физически не может отсутствовать там, где уже работают
    'parec'/'pactl', так как идёт в том же пакете, что и libpulse.so.0,
    от которого они оба зависят).

    Смысл: по умолчанию PulseAudio/PipeWire "усыпляет" звуковое устройство
    по таймауту простоя (module-suspend-on-idle), пока трек в Suno стоит
    на паузе. При возобновлении воспроизведения внешнему аудио-интерфейсу
    может потребоваться несколько секунд на "пробуждение" — именно это и
    даёт звуковую пустоту в начале записи после паузы. Постоянный (пусть
    и полностью тихий) поток на sink не даёт ему засыпать вообще, так что
    "будить" уже нечего.
    """

    CHUNK_BYTES = 4096  # ~23мс тишины за одну запись при 44100Hz/16bit/stereo

    def __init__(self, sink_name: str):
        self.sink_name = sink_name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lib = None
        self._handle = None

    @staticmethod
    def _load_libpulse_simple():
        lib_name = ctypes.util.find_library("pulse-simple")
        candidates = [lib_name] if lib_name else []
        candidates += ["libpulse-simple.so.0", "libpulse-simple.so"]
        for name in candidates:
            if not name:
                continue
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        return None

    def start(self) -> bool:
        lib = self._load_libpulse_simple()
        if lib is None:
            return False

        lib.pa_simple_new.restype = ctypes.c_void_p
        lib.pa_simple_new.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.POINTER(_PaSampleSpec), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        lib.pa_simple_write.restype = ctypes.c_int
        lib.pa_simple_write.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int),
        ]
        lib.pa_simple_free.restype = None
        lib.pa_simple_free.argtypes = [ctypes.c_void_p]

        spec = _PaSampleSpec(format=_PA_SAMPLE_S16LE, rate=44100, channels=2)
        err = ctypes.c_int(0)
        handle = lib.pa_simple_new(
            None,                                   # server (по умолчанию)
            b"auto_record_suno",                     # имя приложения
            _PA_STREAM_PLAYBACK,
            self.sink_name.encode("utf-8"),          # конкретное устройство
            b"keepalive-silence",                    # имя потока
            ctypes.byref(spec),
            None, None,
            ctypes.byref(err),
        )
        if not handle:
            return False

        self._lib = lib
        self._handle = handle
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        silence = b"\x00" * self.CHUNK_BYTES
        err = ctypes.c_int(0)
        while not self._stop_event.is_set():
            # pa_simple_write блокируется до тех пор, пока сервер не готов
            # принять данные, поэтому сама естественно держит темп ~реального
            # времени — дополнительный sleep() не нужен.
            ret = self._lib.pa_simple_write(self._handle, silence, len(silence), ctypes.byref(err))
            if ret < 0:
                break

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._lib is not None and self._handle:
            try:
                self._lib.pa_simple_free(self._handle)
            except Exception:
                pass


def start_silence_keepalive(sink_name: str) -> SilenceKeepalive | None:
    keepalive = SilenceKeepalive(sink_name)
    if keepalive.start():
        return keepalive
    print(
        "WARNING: не удалось запустить keepalive-проигрывание тишины через "
        "libpulse-simple — не могу держать звуковое устройство активным во "
        "время пауз. После долгих пауз возможна короткая пустота в начале "
        "записи из-за 'пробуждения' аудио-устройства.",
        file=sys.stderr,
    )
    return None


def sanitize_filename(name: str, fallback: str = "suno_capture") -> str:
    name = name.strip()
    if not name:
        return fallback
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:80] or fallback


def make_unique_output_paths(
    dest_dir: Path, base_name: str, raw_suffix: str, out_suffix: str
) -> tuple[Path, Path]:
    """Подбирает пару путей (raw_wav, final_out) так, чтобы НЕ перезаписать
    уже существующий итоговый файл. Suno нередко генерирует несколько
    разных треков с одинаковым названием (например, каверы/варианты одной
    песни) — поэтому при совпадении имени добавляется числовой суффикс
    _01, _02, ... вместо молчаливой перезаписи предыдущей записи."""
    candidate = base_name
    counter = 0
    while True:
        raw_wav = dest_dir / f"{candidate}{raw_suffix}"
        final_out = dest_dir / f"{candidate}{out_suffix}"
        if not final_out.exists():
            return raw_wav, final_out
        counter += 1
        candidate = f"{base_name}_{counter:02d}"


def record_and_save_track(
    session: "GeckoSession",
    args: argparse.Namespace,
    ffmpeg_bin: str,
    dest_dir: Path,
    monitor_source: str,
    stop_event: "threading.Event | None" = None,
) -> Path:
    """Ждёт начала воспроизведения одного трека, записывает его в
    96kHz/24bit WAV, затем при необходимости кодирует в выбранный
    пользователем контейнер (args.container: mp3/aac/ogg/flac или None —
    см. format_options.py и encode_helpers.py), и сохраняет итоговый
    файл. Возвращает путь к сохранённому файлу.

    Может выбрасывать исключения (в т.ч. связанные с потерей интернета) —
    вызывающий код в main() решает, как на это реагировать, не завершая
    работу всего скрипта."""
    print(f"Автообнаружение трека и выставление playbackRate={args.rate}...")
    info = prime_and_wait_for_playback(session, args.rate)

    out_suffix = output_suffix_for_container(args.container)
    out_name = args.out_name or sanitize_filename(info.get("title", ""))
    raw_suffix = "_capture.wav" if args.rate == 1.0 else f"_{args.rate}x.wav"
    raw_wav, final_out = make_unique_output_paths(dest_dir, out_name, raw_suffix, out_suffix)
    print(f"Обнаружен играющий трек: \"{info.get('title', '')}\" -> {final_out.name}")

    duration = info.get("duration", -1)
    if duration <= 0:
        print("WARNING: не удалось определить длительность трека заранее.", file=sys.stderr)
        expected_capture_time = 600
    else:
        expected_capture_time = duration / args.rate
        if args.rate == 1.0:
            print(f"Длительность оригинала: {duration:.1f}s")
        else:
            print(f"Длительность оригинала: {duration:.1f}s -> ожидаемое время записи: {expected_capture_time:.1f}s")

    if raw_wav.exists():
        print(f"Удаляю старый временный файл записи: {raw_wav}")
        raw_wav.unlink()

    print(f"Запускаю запись (parec -> [gate] -> ffmpeg, {RAW_CAPTURE_RATE}Hz/{RAW_CAPTURE_BIT_DEPTH}bit)...")
    parec_proc = None
    ffmpeg_proc = None
    relay_thread = None
    relay_stop_event = threading.Event()
    # Трек уже подтверждённо играет (см. prime_and_wait_for_playback выше),
    # поэтому вентиль записи открыт с самого начала.
    gate = RecordingGate(initially_open=True)
    try:
        parec_proc = subprocess.Popen(
            parec_raw_capture_args(monitor_source),
            stdout=subprocess.PIPE,
        )
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_raw_wav_args(ffmpeg_bin, raw_wav),
            stdin=subprocess.PIPE,
        )
        # ffmpeg больше не читает напрямую из stdout parec: между ними стоит
        # поток-ретранслятор (_relay_audio_to_encoder), который непрерывно
        # вычитывает поток из parec (не давая ему остановиться/зависнуть на
        # заполненном буфере), но передаёт байты в stdin ffmpeg только пока
        # gate открыт. Это и есть физическая остановка/продолжение записи
        # в wav по паузе плеера — см. подробности в RecordingGate.
        relay_thread = threading.Thread(
            target=_relay_audio_to_encoder,
            args=(parec_proc.stdout, ffmpeg_proc.stdin, gate, relay_stop_event),
            daemon=True,
        )
        relay_thread.start()

        reason = "interrupted"
        try:
            reason = wait_for_track_boundary(
                session, info.get("src", ""), expected_capture_time, gate,
            )
        except KeyboardInterrupt:
            # Ctrl+C / "Стоп" из GUI (SIGINT) — прилетает сюда, пока мы ждём
            # естественного конца трека. Раньше это исключение пролетало
            # насквозь через весь try (включая graceful-остановку записи
            # ниже И кодирование в выбранный контейнер ещё ниже, ПОСЛЕ
            # try/finally) — finally успевал только аварийно kill()-нуть
            # процессы, а сам wav так и оставался НЕ перекодированным в
            # выбранный контейнер. Ловим здесь и считаем это таким же
            # полноправным "концом записи", как естественная граница
            # трека, — весь код ниже (graceful-остановка, кодирование в
            # контейнер) выполняется в точности так же, как обычно.
            if stop_event is not None:
                stop_event.set()
        print(f"Событие окончания записи: {reason}")

        print("Останавливаю запись...")
        relay_stop_event.set()
        parec_proc.terminate()
        try:
            parec_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            parec_proc.kill()
            parec_proc.wait(timeout=10)

        # Ретранслятор сам закроет ffmpeg_proc.stdin, как только получит EOF
        # от уже завершённого parec (или сработает stop_event) — дожидаемся
        # этого перед ожиданием самого ffmpeg, иначе тот будет висеть на
        # чтении из ещё не закрытого stdin.
        relay_thread.join(timeout=10)

        try:
            ffmpeg_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=10)

        if ffmpeg_proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg завершился с ошибкой при захвате звука (код {ffmpeg_proc.returncode}) "
                "— запись не удалась, см. вывод ffmpeg выше."
            )
    finally:
        relay_stop_event.set()
        if parec_proc is not None and parec_proc.poll() is None:
            parec_proc.kill()
        if relay_thread is not None and relay_thread.is_alive():
            relay_thread.join(timeout=5)
        if ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            ffmpeg_proc.kill()

    if not raw_wav.exists() or raw_wav.stat().st_size == 0:
        raise RuntimeError("Файл записи пуст или не создан.")

    recorded_duration = probe_wav_duration(ffmpeg_bin, raw_wav)
    if recorded_duration is not None:
        print(f"Длительность записанного файла: {recorded_duration:.1f}s (ожидалось ~{expected_capture_time:.1f}s)")
        if expected_capture_time > 0 and abs(recorded_duration - expected_capture_time) > max(5.0, expected_capture_time * 0.25):
            print(
                "WARNING: длительность записи заметно отличается от ожидаемой — "
                "запись могла пройти некорректно (проверьте итоговый файл).",
                file=sys.stderr,
            )

    print(f"Записано: {raw_wav}")

    tempo_filter_args: list[str] = []
    if args.rate != 1.0:
        print(f"Замедляю обратно (atempo={1/args.rate:.4f})...")
        tempo_filter_args = ["-filter:a", f"atempo={1/args.rate}"]

    if args.container:
        print(f"Кодирую в {args.container} (320kbit CBR / 48kHz, из 96kHz/24bit источника)...")
    else:
        print("Контейнер не выбран — оставляю как WAV 96kHz/24bit (без перекодирования).")

    # Если нужен atempo (rate != 1.0), а контейнер не выбран (чистый WAV),
    # atempo всё равно нужно применить — encode_final_audio() в этом
    # случае просто переименует raw_wav, поэтому atempo для rate!=1.0
    # с пустым container обрабатываем отдельным проходом ffmpeg заранее.
    if tempo_filter_args and not args.container:
        tempo_applied = raw_wav.with_name(raw_wav.stem + "_tempo.wav")
        subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
             "-i", str(raw_wav), *tempo_filter_args,
             "-c:a", RAW_CAPTURE_WAV_CODEC, str(tempo_applied)],
            check=True,
        )
        raw_wav.unlink(missing_ok=True)
        raw_wav = tempo_applied

    encode_final_audio(
        ffmpeg_bin, raw_wav, final_out, args.container,
        extra_filter_args=tempo_filter_args if args.container else [],
    )

    print(f"Готово: {final_out}")
    return final_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="https://suno.com/create",
                     help="Ссылка, откуда стартовать (по умолчанию — ваша библиотека Suno; "
                          "конкретный трек определяется автоматически по факту начала воспроизведения)")
    ap.add_argument("out_name", nargs="?", default=None,
                     help="Имя выходного файла. Если не указано — берётся из названия трека автоматически")
    ap.add_argument("-d", "--destination", default=".", help="Папка сохранения итогового mp3 (по умолчанию текущая)")
    ap.add_argument("--profile", default=None, help="Путь к профилю Firefox (если не задан — определяется автоматически)")
    ap.add_argument("--firefox-binary", default=None,
                     help="Путь к бинарнику firefox-bin (если не задан — берётся из уже "
                          "запущенного процесса firefox-bin, либо ищется в PATH/типичных путях)")
    ap.add_argument("--firefox-wait-timeout", type=float, default=60.0,
                     help="Сколько секунд ждать появления процесса firefox-bin, если он ещё "
                          "не запущен (по умолчанию 60; 0 — не ждать)")
    ap.add_argument("--rate", type=float, default=1.0,
                     help="Ускорение воспроизведения при записи (по умолчанию 1.0 — "
                          "обычная скорость, без потери качества). Значения >1 ускоряют "
                          "запись, но требуют обратного time-stretch через ffmpeg atempo, "
                          "который вносит собственные искажения — используйте только если "
                          "скорость записи важнее качества звука.")
    ap.add_argument("--headless", action="store_true", help="Запуск без окна (звук всё равно пойдёт через monitor-source)")
    ap.add_argument("--no-profile-copy", action="store_true",
                     help="Использовать профиль напрямую без копирования (нужно закрыть основной Firefox)")
    ap.add_argument("--no-firefox-cache", action="store_true",
                     help="Не использовать сохранённую с прошлого запуска копию профиля/путь "
                          "к firefox-bin — определить их заново (нужен запущенный основной "
                          "firefox-bin), и обновить кэш результатом")
    ap.add_argument("--reset-firefox-cache", action="store_true",
                     help="Стереть сохранённую копию профиля и путь к firefox-bin, затем выйти")
    ap.add_argument("--no-auto-ffmpeg", action="store_true",
                     help="Не скачивать статическую сборку ffmpeg, даже если у системного "
                          "ffmpeg нет кодека, нужного для выбранного --container")
    ap.add_argument("--container", default=None, choices=["mp3", "aac", "ogg", "flac"],
                     help="В какой контейнер кодировать после записи (используется блочным "
                          "GUI). Если не задано — итоговый файл остаётся WAV 96kHz/24bit "
                          "без перекодирования (пункт 3 требований).")
    ap.add_argument("--list-audio-devices", action="store_true",
                     help="Показать список звуковых устройств (sink'ов) и default sink, "
                          "затем выйти — для диагностики на других звуковых картах.")
    ap.add_argument("--debug-firefox-processes", action="store_true",
                     help="Печатать подробный дамп всех процессов firefox-bin (pid/ppid/exe/"
                          "аргументы) на каждом шаге поиска и закрытия. По умолчанию выключено "
                          "— включайте только если нужно разобраться в проблеме с закрытием/"
                          "запуском firefox-bin.")
    args = ap.parse_args()

    global _DEBUG_FIREFOX_PROCESSES
    _DEBUG_FIREFOX_PROCESSES = args.debug_firefox_processes

    if args.list_audio_devices:
        list_audio_devices()
        return 0

    if args.reset_firefox_cache:
        clear_firefox_cache()
        print("Кэш профиля/бинарника Firefox очищен.")
        return 0

    profile_path, firefox_binary, pending_close_pid, use_profile_copy = resolve_firefox_launch_plan(
        args.profile, args.firefox_binary, wait_timeout=args.firefox_wait_timeout,
        use_cache=not args.no_firefox_cache,
    )
    if args.no_profile_copy:
        use_profile_copy = False

    dest_dir = Path(args.destination).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ensure_parec()
    ffmpeg_bin = resolve_ffmpeg_bin(args.container, auto_download=not args.no_auto_ffmpeg)
    print(f"Использую ffmpeg: {ffmpeg_bin}")
    print(f"Контейнер: {args.container or 'не выбран -> WAV 96kHz/24bit по умолчанию'}")

    print("Запускаю geckodriver + firefox-bin...")
    session = GeckoSession(
        profile_path,
        headless=args.headless,
        firefox_binary=firefox_binary,
        pending_close_pid=pending_close_pid,
        use_profile_copy=use_profile_copy,
    )

    # Изоляция звука: захват должен видеть ТОЛЬКО этот, только что
    # запущенный процесс firefox-bin (и его потомков) — никакие другие
    # браузеры/приложения в системе (например, Chromium) захватываться не
    # должны ни при каких обстоятельствах. Если настроить изоляцию не
    # получилось — сознательно прерываем работу, а не тихо откатываемся на
    # запись системного monitor'а (это бы нарушило требование изоляции).
    print("Настраиваю изоляцию звука (запись только из этого Firefox)...")
    audio_isolator = FirefoxAudioIsolator(session.firefox_pid)
    try:
        monitor_source = audio_isolator.start()
    except Exception:
        session.quit()
        raise
    print(f"Захват изолирован: {monitor_source} (только firefox-bin pid={session.firefox_pid} и его потомки)")

    keepalive_proc = start_silence_keepalive(audio_isolator.null_sink_name)
    if keepalive_proc is not None:
        print(f"Держу виртуальный sink активным во время пауз (sink: {audio_isolator.null_sink_name}).")

    saved_tracks: list[Path] = []
    stop_event = threading.Event()
    try:
        # Первое открытие страницы: если интернета нет или он пропадёт прямо
        # сейчас — не падаем, а сообщаем пользователю и ждём восстановления.
        navigate_with_retry(session, args.url)
        print(f"Открыл {args.url}.")
        print(
            "Скрипт теперь работает непрерывно: после сохранения трека он "
            "не завершится, а будет ждать следующего нажатия play. "
            "Чтобы остановить скрипт — Ctrl+C."
        )

        while True:
            try:
                final_out = record_and_save_track(
                    session, args, ffmpeg_bin, dest_dir, monitor_source, stop_event,
                )
                saved_tracks.append(final_out)
                if stop_event.is_set():
                    # Остановлено пользователем ВО ВРЕМЯ этого трека — он
                    # уже полностью доведён до конца (кодирование в
                    # выбранный контейнер выполнено как обычно, см.
                    # record_and_save_track), поэтому дальше просто
                    # завершаем сессию, а не ждём следующий трек.
                    print("\nОстановлено пользователем — запись сохранена, завершаю сессию.")
                    break
                print("Жду следующий трек (нажмите play в Suno)...")
            except KeyboardInterrupt:
                # Ctrl+C ДО начала воспроизведения (пока ждём автообнаружение
                # трека) — записывать ещё нечего, отменяем как раньше.
                raise
            except Exception as exc:
                if _looks_like_network_error(exc):
                    print(
                        f"Похоже, во время работы с треком пропал интернет ({exc}).",
                        file=sys.stderr,
                    )
                    wait_for_internet()
                    print("Соединение восстановлено, продолжаю ждать треки...")
                else:
                    print(f"ERROR при обработке трека: {exc}", file=sys.stderr)
                    print("Скрипт не завершает работу — жду следующий трек...", file=sys.stderr)
                    time.sleep(2)
                continue
    except KeyboardInterrupt:
        print("\nОстановлено пользователем (Ctrl+C).")
    finally:
        session.quit()
        if keepalive_proc is not None:
            keepalive_proc.stop()
        audio_isolator.stop()

    if not saved_tracks:
        print("ERROR: не удалось сохранить ни одного трека.", file=sys.stderr)
        return 1

    print(f"Всего сохранено треков за сессию: {len(saved_tracks)}")
    for path in saved_tracks:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
