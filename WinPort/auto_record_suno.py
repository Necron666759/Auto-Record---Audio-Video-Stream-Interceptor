#!/usr/bin/env python3
"""
auto_record_suno.py (Windows 10/11 редакция, без selenium)

Полностью автоматизированная запись собственной композиции с Suno —
"запустил и всё". Вместо пакета selenium используется прямое общение с
geckodriver по HTTP (протокол WebDriver Classic) — из pip-зависимостей
внешних не нужно (WASAPI-захват — чистый ctypes, см. win_audio_loopback.py).

Это Windows-порт исходной (Linux) версии программы, урезанный по
согласованному ТЗ:
  1) Блок YouTube (видео/аудио) убран полностью — файл auto_record_youtube.py
     в этой поставке отсутствует.
  2) Пункт "захват экрана" убран — файл screen_capture.py отсутствует,
     программа никогда не пишет видео.
  3) "Звук внутри видео" как отдельная опция убран (не нужен без видео).
  4) Работа только с Suno.
  5) Шапка программы — "Auto Record - Audio Stream Interceptor" (см. i18n.py).

Что изменилось по сравнению с Linux-версией технически:
  - Изоляция звука: вместо PulseAudio (null-sink + move-sink-input +
    parec) используется нативный Windows WASAPI "Process Loopback
    Capture" (см. win_audio_loopback.py) — захват звука именно процесса
    firefox.exe, запущенного этим скриптом, и его дерева потомков.
    Требует Windows 10 версии 2004+ или Windows 11.
  - Поиск/закрытие процессов: вместо /proc используется win_process.py
    (ctypes + Toolhelp32Snapshot).
  - Поиск профиля/бинарника Firefox упрощён: программа больше НЕ требует
    заранее открытого основного Firefox и не закрывает его — профиль
    берётся из profiles.ini (%APPDATA%\\Mozilla\\Firefox\\profiles.ini),
    бинарник — из реестра/стандартных путей установки/PATH. Firefox
    всегда запускается своим отдельным процессом с копией профиля,
    поэтому открытый основной Firefox не мешает и не трогается.

⚠️ Модули win_process.py и win_audio_loopback.py написаны по документации
WinAPI/WASAPI, но не были протестированы на реальной Windows-машине (см.
предупреждения в их докстрингах и в README_WINDOWS.md) — при первом
запуске стоит внимательно проверить, что запись реально идёт только из
Firefox.

Требования:
  ffmpeg в PATH (или он будет скачан автоматически, см. ensure_ffmpeg_with_mp3)
  Python 3.9+ (только стандартная библиотека)
  Windows 10 (2004+) или Windows 11 — для изоляции звука по процессу

  geckodriver отдельно ставить НЕ нужно — при первом запуске скрипт сам
  скачает нужную версию с GitHub Releases в
  %LOCALAPPDATA%\\auto_record_suno\\geckodriver и будет переиспользовать
  её в дальнейшем.

Использование:
  python auto_record_suno.py
  python auto_record_suno.py -d C:\\Users\\me\\Music
  python auto_record_suno.py "https://suno.com/song/КОНКРЕТНЫЙ_ID" -d C:\\Music
"""

from __future__ import annotations

import argparse
import collections
import configparser
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import winreg
import zipfile
from pathlib import Path

from format_options import (
    get_container, all_containers,
    RAW_CAPTURE_RATE, RAW_CAPTURE_BIT_DEPTH, RAW_CAPTURE_WAV_CODEC,
)
from encode_helpers import (
    ffmpeg_raw_wav_args_dynamic,
    encode_final_audio,
    output_suffix_for_container,
)
from win_audio_loopback import (
    ProcessLoopbackCapture, DefaultDeviceLoopbackCapture, LoopbackCaptureError,
)
from win_audio_devices import run_audio_diagnostics
import win_process
import win_stdio

if sys.platform != "win32":
    print(
        "ERROR: эта редакция auto_record_suno.py рассчитана только на "
        "Windows 10/11 (использует WASAPI/WinAPI через ctypes).",
        file=sys.stderr,
    )
    raise SystemExit(1)

# --------------------------------------------------------------------------
# Работа без окна консоли (см. win_stdio.py и README_WINDOWS.md, раздел про
# скрытие второго окна). Программа собирается как windowed-приложение, поэтому:
#   1) sys.stdin/stdout/stderr могут быть None — подставляем рабочие потоки
#      (реальные pipe'ы от GUI; если их нет — файл журнала, путь напечатается
#      ниже, как только поток заработает);
#   2) geckodriver.exe / ffmpeg.exe / taskkill.exe без этого открывали бы свои
#      окна консоли — все подпроцессы запускаются с CREATE_NO_WINDOW.
# При запуске из обычного терминала (есть консоль) подмена Popen не ставится.
# --------------------------------------------------------------------------
_STDIO_LOG_FILE = win_stdio.ensure_std_streams(prefer_log_file=True)
win_stdio.install_no_window_popen()

# --------------------------------------------------------------------------
# Кодировка консоли (см. README_WINDOWS.md, раздел про 'charmap' codec):
# по умолчанию Python на Windows пишет в консоль в кодировке активной
# кодовой страницы (напр. cp1251/cp866), а не в UTF-8. Название трека
# из Suno или просто "красивое" тире/стрелка в наших же сообщениях легко
# может содержать символ, которого нет в этой кодовой странице — тогда
# print() падает с UnicodeEncodeError ("'charmap' codec can't encode
# character..."), и это ломает буквально ЛЮБОЕ сообщение с таким
# символом (включая наши собственные WARNING/ERROR-строки), а не только
# конкретное название трека. reconfigure(errors="replace") — стандартный
# способ сделать print() НЕУБИВАЕМЫМ: неотображаемые в текущей консоли
# символы просто заменяются на '?', но сама программа не падает и не
# зацикливается на одной и той же ошибке.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            # line_buffering=True — принципиально: PyInstaller-сборка игнорирует
            # PYTHONUNBUFFERED, и stdout (pipe в GUI) иначе копился бы блоками,
            # а лог показывался пачкой только при завершении процесса (из-за
            # этого stderr в логе шёл ВПЕРЕДИ stdout).
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (ValueError, OSError):
            pass


def _on_ctrl_break(signum, frame) -> None:  # noqa: ANN001 — сигнатура обработчика сигналов фиксирована
    """CTRL_BREAK_EVENT (см. recorder_gui.py — GUI останавливает процесс
    именно им, а не CTRL_C_EVENT, т.к. это единственный сигнал, который
    subprocess.Popen.send_signal умеет адресно доставить одному
    конкретному дочернему процессу на Windows) сам по себе НЕ поднимает
    KeyboardInterrupt в Python — если не поставить свой обработчик,
    Windows использует обработчик консоли по умолчанию, который просто
    убивает процесс мгновенно (код выхода 3221225786 /
    0xC000013A / STATUS_CONTROL_C_EXIT — то, что видно в логе GUI), минуя
    весь try/except/finally в main() (включая session.quit(), который
    должен закрывать firefox.exe). Поэтому явно переводим сигнал в
    привычный KeyboardInterrupt, который уже штатно перехватывается по
    всему коду (как обычный Ctrl+C в консоли на Linux/macOS)."""
    raise KeyboardInterrupt()


signal.signal(signal.SIGBREAK, _on_ctrl_break)


def _app_base_dir() -> Path:
    """Папка, где реально лежит исполняемый файл — .exe при запуске из
    собранной PyInstaller-сборки (getattr(sys, 'frozen', False)), либо
    папка этого .py при обычном запуске "python auto_record_suno.py".
    Используется только для CODECS_DIR (см. ниже) — папки состояния вроде
    кэша профиля Firefox сознательно остаются в %LOCALAPPDATA% (это
    per-user данные, а не то, что скачивается из интернета)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_writable_download_dir(preferred: Path, fallback: Path, what: str) -> Path:
    """Пытается создать/использовать preferred (по требованию — рядом с
    .exe), при неудаче (например, .exe лежит в Program Files и запущен
    без прав администратора) молча не остаёмся ни с чем, а откатываемся
    на fallback (%LOCALAPPDATA%) с явным предупреждением — лучше
    рабочая программа с кэшем не в идеальном месте, чем сообщение об
    ошибке доступа при каждом запуске."""
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred
    except OSError as exc:
        print(
            f"WARNING: не удалось использовать папку '{preferred}' для {what} ({exc}) — "
            f"использую вместо неё '{fallback}'. Обычно это означает, что программа "
            "установлена в защищённое место (например, Program Files) без прав "
            "администратора.",
            file=sys.stderr,
        )
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
# Состояние программы (per-user, не скачивается из интернета): копия
# профиля Firefox и запомненный путь к firefox.exe — остаётся в
# %LOCALAPPDATA%, как и было.
CACHE_ROOT = LOCAL_APPDATA / "auto_record_suno"
FIREFOX_PROFILE_CACHE_DIR = CACHE_ROOT / "firefox_profile"
FIREFOX_BINARY_CACHE_FILE = CACHE_ROOT / "firefox_binary_path.txt"

# Скачиваемые бинарники (geckodriver, ffmpeg) — по требованию ТЗ хранятся
# РЯДОМ С ИСПОЛНЯЕМЫМ ФАЙЛОМ в отдельной папке (portable-режим: всё
# необходимое для работы лежит там же, где сам .exe, а не расползается
# по системным папкам пользователя). Если это невозможно (например, .exe
# лежит в Program Files без прав администратора на запись) — используется
# %LOCALAPPDATA% как запасной вариант, см. _resolve_writable_download_dir().
CODECS_DIR = _resolve_writable_download_dir(
    _app_base_dir() / "AutoRecord-Codecs",
    CACHE_ROOT / "codecs",
    "скачиваемых кодеков (ffmpeg/geckodriver)",
)
GECKODRIVER_CACHE_DIR = CODECS_DIR / "geckodriver"
FFMPEG_CACHE_DIR = CODECS_DIR / "ffmpeg"


# --------------------------------------------------------------------------
# geckodriver: автозагрузка (Windows-архив, win64/win32 zip)
# --------------------------------------------------------------------------

def _geckodriver_asset_name() -> str:
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").lower()
    arch = "win64" if machine in ("amd64", "x86_64") else "win32"
    return f"geckodriver-{{version}}-{arch}.zip"


def _download_with_progress(url: str, dest_path: Path, label: str) -> None:
    """Скачивает url в dest_path, печатая прогресс в процентах.

    Печатаем каждые ~10% ОТДЕЛЬНОЙ строкой (с явным flush) — не '\\r' с
    перезаписью текущей строки, как принято в обычном терминале: лог этой
    программы читается не только в реальной консоли, но и построчно
    перекачивается в лог-панель recorder_gui.py (см. _reader_thread) —
    там '\\r' без завершающего '\\n' не отображается как "живой" прогресс
    построчного чтения, поэтому используем обычные строки."""
    print(f"Скачиваю {label}: {url}")
    last_reported = -1

    def _report(block_num: int, block_size: int, total_size: int) -> None:
        nonlocal last_reported
        if total_size <= 0:
            return
        downloaded = min(block_num * block_size, total_size)
        pct = downloaded * 100 // total_size
        milestone = (pct // 10) * 10
        if milestone != last_reported:
            last_reported = milestone
            print(f"  {milestone:3d}%  ({downloaded // 1024:,} / {total_size // 1024:,} КБ)".replace(",", " "),
                  flush=True)

    urllib.request.urlretrieve(url, dest_path, reporthook=_report)
    if last_reported < 100:
        print("  100%  (готово)", flush=True)


def _latest_geckodriver_release() -> tuple[str, str]:
    """Возвращает (version, asset_download_url) последнего релиза с GitHub."""
    api_url = "https://api.github.com/repos/mozilla/geckodriver/releases/latest"
    req = urllib.request.Request(api_url, headers={"User-Agent": "auto_record_suno"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    version = data["tag_name"]
    asset_name = _geckodriver_asset_name().format(version=version)

    for asset in data.get("assets", []):
        if asset["name"] == asset_name:
            return version, asset["browser_download_url"]

    raise RuntimeError(f"Не найден подходящий файл релиза geckodriver: {asset_name}")


def ensure_geckodriver() -> str:
    """Возвращает путь к рабочему geckodriver, скачивая его при необходимости."""
    existing = shutil.which("geckodriver")
    if existing:
        return existing

    cached_binary = GECKODRIVER_CACHE_DIR / "geckodriver.exe"
    if cached_binary.exists():
        return str(cached_binary)

    print("geckodriver не найден — скачиваю автоматически с GitHub Releases...")
    GECKODRIVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    version, url = _latest_geckodriver_release()
    print(f"Версия: {version}, источник: {url}")

    archive_path = GECKODRIVER_CACHE_DIR / Path(url).name
    _download_with_progress(url, archive_path, f"geckodriver {version}")

    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(GECKODRIVER_CACHE_DIR)
    archive_path.unlink(missing_ok=True)

    if not cached_binary.exists():
        raise RuntimeError(f"После распаковки не найден бинарник по ожидаемому пути: {cached_binary}")

    print(f"geckodriver готов: {cached_binary}")
    return str(cached_binary)


# --------------------------------------------------------------------------
# Профиль Firefox (profiles.ini) — Windows-путь
# --------------------------------------------------------------------------

def _firefox_appdata_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("Переменная окружения %APPDATA% не задана — не могу найти профиль Firefox.")
    return Path(appdata) / "Mozilla" / "Firefox"


def find_default_firefox_profile() -> str:
    """Читает %APPDATA%\\Mozilla\\Firefox\\profiles.ini и возвращает путь
    к default-профилю.

    ВАЖНО (исправленный баг): в profiles.ini поле 'Default' означает
    РАЗНОЕ в разных секциях —
      - в секциях [InstallXXXXXXXX] (современный формат, Firefox ~67+):
        Default — это САМ ОТНОСИТЕЛЬНЫЙ ПУТЬ к профилю-умолчанию для
        данной инсталляции (именно этим полем реально руководствуется
        сам Firefox при обычном запуске без -P);
      - в секциях [ProfileN] (более старый формат / общий список
        профилей): Default — это ФЛАГ '1'/'0' ("этот профиль — тот, что
        по умолчанию"), а НЕ путь. Путь всегда лежит в отдельном поле
        'Path'.
    Более ранняя версия этой функции ошибочно брала config.get(section,
    'Default') как путь для ЛЮБОЙ секции — для [ProfileN] это давало
    буквальную строку '1' вместо реального пути к профилю (на практике
    выливалось в попытку скопировать несуществующую папку
    '...\\Firefox\\1' и ошибку WinError 3)."""
    base = _firefox_appdata_dir()
    ini_path = base / "profiles.ini"
    if not ini_path.exists():
        raise RuntimeError(
            f"Не найден {ini_path} — Firefox профиль не обнаружен автоматически. "
            "Убедитесь, что Firefox хотя бы раз запускался на этом компьютере, "
            "или укажите профиль явно через --profile."
        )

    config = configparser.ConfigParser()
    config.read(ini_path)

    # 1) Секции [InstallXXXXXXXX] — Default там уже готовый путь.
    for section in config.sections():
        if section.startswith("Install") and config.has_option(section, "Default"):
            return str(base / config.get(section, "Default"))

    # 2) Секции [ProfileN] — путь только в Path, Default там лишь флаг.
    profile_paths: list[str] = []
    default_path: str | None = None
    for section in config.sections():
        if not section.startswith("Profile") or not config.has_option(section, "Path"):
            continue
        path = config.get(section, "Path")
        profile_paths.append(path)
        if config.has_option(section, "Default") and config.getboolean(section, "Default", fallback=False):
            default_path = path

    if default_path:
        return str(base / default_path)
    for path in profile_paths:
        if "default-release" in path:
            return str(base / path)
    if profile_paths:
        return str(base / profile_paths[0])

    raise RuntimeError("В profiles.ini не найдено ни одного профиля.")


# --------------------------------------------------------------------------
# Поиск бинарника firefox.exe: реестр App Paths -> типичные папки установки
# -> PATH
# --------------------------------------------------------------------------

def _firefox_binary_from_registry() -> str | None:
    """Стандартный Windows-механизм 'App Paths' — большинство инсталляторов
    (в т.ч. Firefox) регистрируют там путь к своему .exe. Проверяем и
    HKLM, и HKCU (пользовательская установка без прав администратора)."""
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and Path(value).exists():
                    return value
        except OSError:
            continue
    return None


def _firefox_binary_from_common_paths() -> str | None:
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Mozilla Firefox" / "firefox.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def find_firefox_binary(explicit_binary: str | None = None) -> str:
    if explicit_binary:
        if not Path(explicit_binary).exists():
            raise RuntimeError(f"Указанный --firefox-binary не найден: {explicit_binary}")
        return explicit_binary

    from_registry = _firefox_binary_from_registry()
    if from_registry:
        return from_registry

    from_common = _firefox_binary_from_common_paths()
    if from_common:
        return from_common

    from_path = shutil.which("firefox") or shutil.which("firefox.exe")
    if from_path:
        return from_path

    raise RuntimeError(
        "Не удалось автоматически найти firefox.exe (проверены реестр App Paths, "
        "стандартные папки установки Program Files/Program Files (x86)/LOCALAPPDATA, "
        "и PATH). Укажите путь явно флагом --firefox-binary."
    )


# --------------------------------------------------------------------------
# Кэш профиля/бинарника между запусками
# --------------------------------------------------------------------------

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
    def ignore(_dir, names):
        return [n for n in names if n in _PROFILE_COPY_SKIP_NAMES]

    shutil.copytree(source_profile, dest, ignore=ignore, dirs_exist_ok=True)
    with open(dest / "user.js", "a", encoding="utf-8") as f:
        f.write(_PROFILE_COPY_USER_JS_OVERRIDES)


def make_profile_copy(source_profile: str) -> str:
    """Копирует профиль во временную папку — используется каждый запуск,
    когда постоянный кэш профиля ещё не создан (см. save_firefox_profile_to_cache)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="auto_record_suno_profile_"))
    dest = tmp_dir / "profile"
    _copy_firefox_profile_into(source_profile, dest)
    return str(dest)


def save_firefox_binary_to_cache(firefox_binary: str) -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    FIREFOX_BINARY_CACHE_FILE.write_text(firefox_binary, encoding="utf-8")


def load_cached_firefox_binary() -> str | None:
    try:
        path = FIREFOX_BINARY_CACHE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return path if path and Path(path).exists() else None


def save_firefox_profile_to_cache(source_profile: str) -> str:
    if FIREFOX_PROFILE_CACHE_DIR.exists():
        shutil.rmtree(FIREFOX_PROFILE_CACHE_DIR, ignore_errors=True)
    FIREFOX_PROFILE_CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    _copy_firefox_profile_into(source_profile, FIREFOX_PROFILE_CACHE_DIR)
    return str(FIREFOX_PROFILE_CACHE_DIR)


def get_cached_firefox_profile() -> str | None:
    if FIREFOX_PROFILE_CACHE_DIR.exists() and any(FIREFOX_PROFILE_CACHE_DIR.iterdir()):
        return str(FIREFOX_PROFILE_CACHE_DIR)
    return None


def clear_firefox_cache() -> None:
    shutil.rmtree(FIREFOX_PROFILE_CACHE_DIR, ignore_errors=True)
    FIREFOX_BINARY_CACHE_FILE.unlink(missing_ok=True)


def resolve_firefox_launch_plan(
    explicit_profile: str | None,
    explicit_binary: str | None,
    use_cache: bool = True,
) -> tuple[str, str, bool]:
    """Возвращает (profile_path, firefox_binary, use_profile_copy).

    В отличие от Linux-версии, здесь не нужно ждать/закрывать уже
    запущенный основной Firefox — профиль берётся из profiles.ini,
    бинарник — из find_firefox_binary(). Если явно заданы
    --profile/--firefox-binary, кэш не используется."""
    if not explicit_profile and not explicit_binary and use_cache:
        cached_binary = load_cached_firefox_binary()
        cached_profile = get_cached_firefox_profile()
        if cached_binary and cached_profile:
            print(f"Использую сохранённую копию профиля Firefox: {cached_profile}")
            print(f"Использую сохранённый путь к firefox.exe: {cached_binary}")
            return cached_profile, cached_binary, False

    profile_path = explicit_profile or find_default_firefox_profile()
    firefox_binary = find_firefox_binary(explicit_binary)
    print(f"Использую профиль Firefox: {profile_path}")

    if not explicit_profile and use_cache:
        try:
            cached_dir = save_firefox_profile_to_cache(profile_path)
            save_firefox_binary_to_cache(firefox_binary)
            print(f"Профиль сохранён в кэш ({cached_dir}) — при следующем запуске это ускорит старт.")
        except OSError as exc:
            print(f"WARNING: не удалось сохранить профиль/бинарник в кэш ({exc}).", file=sys.stderr)

    return profile_path, firefox_binary, True


# --------------------------------------------------------------------------
# ffmpeg: автозагрузка Windows-сборки (gyan.dev), если у системного нет
# нужного кодека
# --------------------------------------------------------------------------

def _ffmpeg_has_encoder(ffmpeg_path: str, encoder_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return bool(re.search(rf"\b{re.escape(encoder_name)}\b", result.stdout))


def _download_static_ffmpeg_windows() -> str:
    """Скачивает готовую Windows-сборку ffmpeg (gyan.dev 'essentials' —
    включает libmp3lame/libvorbis/flac/aac из коробки) и кэширует в
    FFMPEG_CACHE_DIR.

    ⚠️ URL ниже указывает на актуальную на момент написания ссылку
    gyan.dev; если сборка переедет — поправьте эту константу (или
    используйте --no-auto-ffmpeg и укажите свой ffmpeg.exe в PATH)."""
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

    FFMPEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = FFMPEG_CACHE_DIR / "ffmpeg-release-essentials.zip"
    _download_with_progress(url, archive_path, "сборку ffmpeg для Windows (с поддержкой mp3/aac/vorbis/flac)")

    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(FFMPEG_CACHE_DIR)
    archive_path.unlink(missing_ok=True)

    extracted_dirs = sorted(FFMPEG_CACHE_DIR.glob("ffmpeg-*-essentials_build"))
    if not extracted_dirs:
        raise RuntimeError("После распаковки не найдена папка со сборкой ffmpeg.")

    src_binary = extracted_dirs[-1] / "bin" / "ffmpeg.exe"
    if not src_binary.exists():
        raise RuntimeError(f"В распакованном архиве не найден ffmpeg.exe: {src_binary}")

    dest_binary = FFMPEG_CACHE_DIR / "ffmpeg.exe"
    shutil.copy2(src_binary, dest_binary)
    shutil.rmtree(extracted_dirs[-1], ignore_errors=True)

    print(f"ffmpeg готов: {dest_binary}")
    return str(dest_binary)


_CONTAINER_REQUIRED_ENCODER = {
    "mp3": "libmp3lame",
    "aac": None,
    "ogg": "libvorbis",
    "flac": "flac",
}


def resolve_ffmpeg_bin(container_id: str | None, auto_download: bool = True) -> str:
    required_encoder = _CONTAINER_REQUIRED_ENCODER.get(container_id) if container_id else None

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and (required_encoder is None or _ffmpeg_has_encoder(system_ffmpeg, required_encoder)):
        return system_ffmpeg

    cached_binary = FFMPEG_CACHE_DIR / "ffmpeg.exe"
    if cached_binary.exists() and (required_encoder is None or _ffmpeg_has_encoder(str(cached_binary), required_encoder)):
        return str(cached_binary)

    if auto_download:
        try:
            downloaded = _download_static_ffmpeg_windows()
            if required_encoder is None or _ffmpeg_has_encoder(downloaded, required_encoder):
                return downloaded
        except Exception as exc:
            print(f"WARNING: не удалось получить ffmpeg с нужным кодеком автоматически ({exc}).", file=sys.stderr)

    if system_ffmpeg:
        print(
            f"WARNING: в системном ffmpeg не найден кодек '{required_encoder}' — "
            "попробую всё равно, ffmpeg сам выдаст ошибку, если это критично.",
            file=sys.stderr,
        )
        return system_ffmpeg

    raise RuntimeError("В PATH не найден ffmpeg, и автозагрузка не удалась/отключена (--no-auto-ffmpeg).")


def ensure_ffmpeg_with_mp3() -> str:
    """Возвращает путь к закешированному (или только что скачанному) ffmpeg.exe
    с libmp3lame. Скачиваемая сборка gyan.dev 'essentials' содержит разом
    mp3/aac/vorbis/flac/opus И libx264/libvpx-vp9 (нужны видео-бэкенду YouTube) —
    несмотря на историческое имя функции (оно совпадает с Linux-версией, откуда
    её вызывает auto_record_youtube.py)."""
    cached_binary = FFMPEG_CACHE_DIR / "ffmpeg.exe"
    if cached_binary.exists() and _ffmpeg_has_encoder(str(cached_binary), "libmp3lame"):
        return str(cached_binary)
    return _download_static_ffmpeg_windows()


def resolve_ffmpeg_bin_for_encoder(
    required_encoder: str | None,
    *,
    auto_download: bool = True,
    strict: bool = False,
) -> str:
    """Универсальная версия подбора ffmpeg: принимает явное имя энкодера (а не
    ключ контейнера) — годится и для видео-контейнеров (auto_record_youtube.py:
    libx264 / libvpx-vp9), как в Linux-версии.

    1. Системный ffmpeg (PATH), если в нём есть нужный кодек.
    2. Закешированный ffmpeg.exe (FFMPEG_CACHE_DIR), если в нём есть кодек.
    3. Если разрешено (auto_download) — скачивание сборки gyan.dev (там есть всё).
    4. Если и это не удалось: strict=False — откат на системный ffmpeg с
       предупреждением; strict=True (для видео) — явная ошибка с причиной."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and (required_encoder is None or _ffmpeg_has_encoder(system_ffmpeg, required_encoder)):
        return system_ffmpeg

    cached_binary = FFMPEG_CACHE_DIR / "ffmpeg.exe"
    if cached_binary.exists() and (required_encoder is None or _ffmpeg_has_encoder(str(cached_binary), required_encoder)):
        return str(cached_binary)

    downloaded = None
    download_error: Exception | None = None
    if auto_download:
        try:
            downloaded = _download_static_ffmpeg_windows()
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
            details.append(f"автозагрузка сборки ffmpeg не удалась ({download_error})")
        elif not auto_download:
            details.append("автозагрузка отключена флагом --no-auto-ffmpeg")
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

    raise RuntimeError("В PATH не найден ffmpeg, и автозагрузка не удалась/отключена (--no-auto-ffmpeg).")


# --------------------------------------------------------------------------
# Минимальный WebDriver-клиент через HTTP (замена selenium)
# --------------------------------------------------------------------------

import socket  # noqa: E402 (после блока констант для читаемости группировки)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# Запросы к ЛОКАЛЬНОМУ geckodriver (127.0.0.1) не должны ходить через прокси:
# urllib по умолчанию берёт прокси из настроек Windows (его выставляют, например,
# VPN/прокси-клиенты), и тогда команды к геккодрайверу могли бы уходить в чужой
# прокси и возвращаться оттуда ошибками 500/502/404, а не от самого geckodriver.
_LOCAL_OPENER: "urllib.request.OpenerDirector | None" = None


def _local_opener() -> "urllib.request.OpenerDirector":
    global _LOCAL_OPENER
    if _LOCAL_OPENER is None:
        opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler({}),        # пустой словарь = «никаких прокси»
            urllib.request.HTTPHandler(),           # только http:// — SSL здесь не нужен
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            opener.add_handler(handler)
        _LOCAL_OPENER = opener
    return _LOCAL_OPENER


class WebDriverError(RuntimeError):
    """geckodriver ответил ошибкой (HTTP 4xx/5xx). В отличие от голого
    urllib.error.HTTPError («HTTP Error 500: Internal Server Error») несёт
    НАСТОЯЩУЮ причину из тела ответа (поля error/message протокола WebDriver):
    например «unknown error: Reached error page: about:neterror?e=dnsNotFound»
    (нет интернета) или «invalid session id» (браузер уже закрыт). Без этого
    невозможно отличить «пропал интернет» от «Firefox закрылся»."""

    def __init__(self, status: int, error: str, message: str, url: str = "") -> None:
        self.status = status
        self.error = error
        self.message = message
        self.url = url
        text = f"HTTP {status} {error}"
        if message:
            text += f": {message}"
        super().__init__(text)


class BrowserGoneError(RuntimeError):
    """Firefox (или geckodriver) закрылся/упал/потерял сессию: выполнять
    команды больше некому, браузер нужно запускать заново."""


class DriverUnresponsiveError(RuntimeError):
    """Процессы живы, но geckodriver не ответил на команду вовремя."""


class PlaybackWaitTimeout(RuntimeError):
    """За отведённое время никто не нажал play — это не ошибка, просто ждём дальше."""


def _webdriver_error_from_http(exc: urllib.error.HTTPError) -> WebDriverError:
    error, message = "unknown error", ""
    try:
        raw = exc.read().decode("utf-8", "replace")
        value = json.loads(raw).get("value", {})
        if isinstance(value, dict):
            error = str(value.get("error") or error)
            message = str(value.get("message") or "").strip()
        elif raw:
            message = raw.strip()
    except Exception:
        pass
    message = " ".join(message.split())[:400]  # без переносов строк и длинных стеков
    return WebDriverError(exc.code, error, message, getattr(exc, "url", "") or "")


def _local_json_request(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with _local_opener().open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise _webdriver_error_from_http(exc) from None


def http_post(url: str, payload: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    return _local_json_request(req, timeout)


def http_get(url: str, timeout: float = 10.0) -> dict:
    return _local_json_request(urllib.request.Request(url, method="GET"), timeout)


def http_delete(url: str, timeout: float = 10.0) -> None:
    try:
        _local_json_request(urllib.request.Request(url, method="DELETE"), timeout)
    except (WebDriverError, urllib.error.URLError, OSError):
        pass


_NETWORK_ERROR_MARKERS = (
    "dnsnotfound", "neterror", "unknown host", "net::err",
    "name_not_resolved", "econnrefused", "connection refused",
    "connection reset", "network is unreachable", "network changed",
    "temporary failure in name resolution", "not have a connection",
    "no internet", "timed out", "timeout",
    "мы не можем подключиться",
)


def _looks_like_network_error(exc: BaseException) -> bool:
    """Похожа ли ошибка на проблему с интернет-соединением.

    ВАЖНО: раньше сетевой считалась ЛЮБАЯ OSError — а OSError бросают и
    ctypes/WinAPI (например, [WinError -2147483634] при сбое WASAPI), и файловые
    операции. Из-за этого сбой захвата звука выдавался за "пропал интернет",
    и цикл бесконечно повторял одну и ту же ошибку. Теперь: ошибки захвата
    звука — никогда не сетевые; «браузер закрылся» и «geckodriver завис» — тоже
    не сетевые; ответ geckodriver (WebDriverError) считается сетевым только
    по его НАСТОЯЩЕМУ тексту (about:neterror, dnsNotFound и т.п. — раньше этот
    текст терялся, и обрыв интернета при открытии страницы ронял всю программу);
    сетевыми считаются также URLError/сокетные/Connection*-ошибки."""
    if isinstance(exc, (LoopbackCaptureError, BrowserGoneError, DriverUnresponsiveError, PlaybackWaitTimeout)):
        return False
    if isinstance(exc, (WebDriverError, urllib.error.HTTPError)):
        pass  # решим по тексту ниже
    elif isinstance(exc, (urllib.error.URLError, socket.timeout, socket.gaierror, ConnectionError, TimeoutError)):
        return True
    elif isinstance(exc, OSError) and getattr(exc, "winerror", None) is not None:
        return False  # ошибка WinAPI, а не сети
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


def wait_for_internet(poll_interval: float = 5.0, max_wait: float = 180.0) -> bool:
    """Ждёт появления интернета не дольше max_wait секунд. True — интернет есть.

    Ожидание ограничено намеренно: проверка идёт с самой программы (не из
    Firefox) и может ошибаться — например, если 1.1.1.1/8.8.8.8 заблокированы, а
    доступ к Suno идёт через VPN/прокси только в браузере. Раньше при такой
    ошибке программа зависала в ожидании навсегда, хотя у браузера интернет был.
    По истечении max_wait просто возвращаем False — вызывающий код всё равно
    повторит попытку сам."""
    if is_internet_available():
        return True
    print(
        "ВНИМАНИЕ: похоже, отсутствует подключение к интернету. "
        f"Жду восстановления соединения (проверка каждые {poll_interval:.0f}с, не дольше {max_wait:.0f}с)...",
        file=sys.stderr,
    )
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if is_internet_available():
            print("Соединение с интернетом восстановлено, продолжаю работу.")
            return True
    print(
        "ВНИМАНИЕ: проверка не подтвердила интернет, но продолжаю попытки "
        "(возможно, доступ идёт через VPN/прокси, который эта проверка не видит).",
        file=sys.stderr,
    )
    return False


def navigate_with_retry(session: "GeckoSession", url: str) -> None:
    """Как session.navigate(), но при сетевой ошибке (DNS/нет сети/обрыв) не
    прерывает работу и НЕ закрывает Firefox, а ждёт восстановления интернета и
    повторяет переход с нарастающей паузой. BrowserGoneError пробрасывается —
    его обрабатывает main() (перезапуск браузера)."""
    attempt = 0
    while True:
        try:
            session.navigate(url)
            return
        except BrowserGoneError:
            raise
        except Exception as exc:
            if not _looks_like_network_error(exc):
                raise
            attempt += 1
            print(f"Не удалось открыть страницу ({str(exc)[:300]}). Проверяю доступность интернета...", file=sys.stderr)
            wait_for_internet()
            delay = min(3 * attempt, 30)
            print(f"Повторяю попытку перехода по ссылке через {delay}с (попытка {attempt})...")
            time.sleep(delay)


_EXIT_CODE_HINTS = {
    0x00000000: "штатное завершение (браузер закрыли — вручную, другой программой или он сам)",
    0x00000001: "код 1 — так обычно выглядит принудительное завершение (taskkill / Диспетчер задач)",
    0xC000013A: "прерван консольным сигналом Ctrl+C / Ctrl+Break",
    0xC0000005: "аварийное падение Firefox (нарушение доступа к памяти)",
    0xC0000409: "аварийное падение Firefox (fail-fast / переполнение буфера стека)",
    0x80000003: "аварийное падение Firefox (crash / breakpoint)",
}


def describe_exit_code(code: int) -> str:
    return f"код выхода {code} (0x{code & 0xFFFFFFFF:08X}) — " + _EXIT_CODE_HINTS.get(
        code & 0xFFFFFFFF, "нестандартный код (см. документацию Windows по этому коду)")


class GeckoSession:
    """Тонкая обёртка над geckodriver HTTP API — то, что делает selenium, но
    напрямую. Всегда запускает СВОЙ firefox.exe с копией профиля — открытый
    основной Firefox пользователя не требуется и не трогается.

    Отслеживает, жив ли браузер: все команды идут через _call(), который при
    любом сбое проверяет процессы и, если Firefox/geckodriver закрылись, бросает
    BrowserGoneError (а не голое «HTTP Error 500/404»)."""

    # Ошибки WebDriver, после которых работать с этой сессией уже нельзя.
    _GONE_ERRORS = ("invalid session id", "no such window", "session not created")
    _GONE_MARKERS = (
        "failed to decode response from marionette",
        "without establishing a connection",
        "browsing context has been discarded",
        "connection refused", "connection reset", "connection closed",
        "unexpectedly closed", "browser has been closed",
    )

    def __init__(self, profile_path: str, headless: bool, firefox_binary: str,
                 use_profile_copy: bool = True, startup_timeout: float = 30.0):
        geckodriver_path = ensure_geckodriver()
        print(f"Использую бинарник Firefox: {firefox_binary}")

        self._profile_copy_dir: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.firefox_pid = 0
        self._watch = None
        self._driver_log: collections.deque[str] = collections.deque(maxlen=200)
        self.started_at = time.monotonic()
        self._started_wall = time.time()
        self._failure_reported = False

        try:
            run_profile = profile_path
            if use_profile_copy:
                print("Копирую профиль во временную папку (чтобы не закрывать основной Firefox)...")
                run_profile = make_profile_copy(profile_path)
                self._profile_copy_dir = Path(run_profile).parent
            self.run_profile = run_profile

            self.port = find_free_port()
            self.base_url = f"http://127.0.0.1:{self.port}"
            # stderr geckodriver (а через него и stderr самого Firefox) ОБЯЗАТЕЛЬНО
            # должен вычитываться: раньше это был subprocess.PIPE, который никто не
            # читал, — как только буфер трубы (несколько КБ) заполнялся, geckodriver и
            # Firefox блокировались на записи в лог и «замирали»/отваливались.
            # Теперь отдельный поток вычитывает вывод и хранит последние строки
            # (пригодятся для диагностики, если браузер закроется).
            self.proc = subprocess.Popen(
                [geckodriver_path, "--port", str(self.port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            threading.Thread(target=self._drain_driver_log, daemon=True).start()
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

            print("Жду, пока реально поднимется процесс firefox.exe...")
            # Надёжнее всего PID главного процесса браузера сообщает сам Firefox
            # через Marionette: capability "moz:processID" в ответе geckodriver.
            # (Первый firefox.exe-потомок geckodriver может оказаться коротко
            # живущим launcher-процессом — тогда захват по его PID бы "молчал".)
            pid_from_caps = (resp.get("value", {}).get("capabilities", {}) or {}).get("moz:processID")
            if isinstance(pid_from_caps, int) and pid_from_caps > 0 and win_process.is_pid_alive(pid_from_caps):
                self.firefox_pid = pid_from_caps
                print(f"firefox.exe запущен (pid {self.firefox_pid}, по данным Marionette).")
            else:
                self.firefox_pid = self._wait_for_firefox_process(run_profile, timeout=startup_timeout)
                print(f"firefox.exe запущен (pid {self.firefox_pid}).")
            self._watch = win_process.ProcessWatch(self.firefox_pid)
        except BaseException:
            # Не оставляем за собой висящий geckodriver/Firefox и временный профиль.
            self.quit()
            raise

    # ---- вывод geckodriver ------------------------------------------------

    def _drain_driver_log(self) -> None:
        stream = self.proc.stderr if self.proc is not None else None
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._driver_log.append(line)
        except (OSError, ValueError):
            pass

    def _wait_for_firefox_process(self, run_profile: str, timeout: float) -> int:
        """Ищет среди процессов firefox.exe тот, что запущен с нашим
        временным профилем (по командной строке через geckodriver capabilities
        мы уже знаем PID геквдрайвера, но надёжнее всего — самый молодой
        firefox.exe, чей родитель это наш geckodriver.proc)."""
        deadline = time.monotonic() + timeout
        geckodriver_pid = self.proc.pid
        while time.monotonic() < deadline:
            procs = win_process.list_processes()
            by_ppid = {}
            for p in procs:
                by_ppid.setdefault(p.ppid, []).append(p)
            for child in by_ppid.get(geckodriver_pid, []):
                if child.name.lower() == "firefox.exe":
                    return child.pid
            time.sleep(0.3)
        raise TimeoutError(
            f"Не дождался запуска firefox.exe (дочернего от geckodriver, pid={geckodriver_pid}) за {timeout:.0f}с."
        )

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.3)  # дать потоку-«вычитывателю» дочитать хвост вывода
                tail = "\n".join(list(self._driver_log)[-15:])
                raise RuntimeError(
                    f"geckodriver завершился с кодом {self.proc.returncode}"
                    + (f": {tail[-1200:]}" if tail else "")
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

    # ---- состояние браузера ----------------------------------------------

    def is_alive(self) -> bool:
        """Работают ли И geckodriver, И сам Firefox (по коду завершения процесса,
        а не по «удалось ли открыть handle» — см. win_process.is_pid_alive)."""
        if self.proc is None or self.proc.poll() is not None:
            return False
        if self._watch is not None:
            return self._watch.is_running()
        return bool(self.firefox_pid) and win_process.is_pid_alive(self.firefox_pid)

    def describe_failure(self) -> str:
        """Подробный отчёт «почему браузер недоступен» — чтобы при закрытии Firefox
        было видно, закрыли его штатно или он упал."""
        lines = []
        code = self._watch.exit_code() if self._watch is not None else None
        if code is not None:
            lines.append(f"Firefox (pid {self.firefox_pid}) завершился: {describe_exit_code(code)}.")
        elif self.proc is not None and self.proc.poll() is None and self.is_alive():
            lines.append(f"Процесс Firefox (pid {self.firefox_pid}) ещё жив, но сессия/вкладка потеряна.")
        else:
            lines.append(f"Firefox (pid {self.firefox_pid}) не работает.")
        if self.proc is not None and self.proc.poll() is not None:
            lines.append(f"geckodriver тоже завершился (код {self.proc.returncode}).")
        try:
            dumps = sorted((Path(self.run_profile) / "minidumps").glob("*.dmp"))
            fresh = [d for d in dumps if d.stat().st_mtime >= self._started_wall - 5]
            if fresh:
                lines.append(
                    f"Найден отчёт о падении Firefox: {fresh[-1]} — Firefox именно УПАЛ "
                    "(файл можно приложить к разбору проблемы)."
                )
        except OSError:
            pass
        tail = list(self._driver_log)[-12:]
        if tail:
            lines.append("Последние строки вывода geckodriver/Firefox:")
            lines.extend(f"    {ln[:300]}" for ln in tail)
        return "\n".join(lines)

    def _is_gone_error(self, exc: WebDriverError) -> bool:
        if exc.error in self._GONE_ERRORS:
            return True
        text = f"{exc.error} {exc.message}".lower()
        if "neterror" in text or "reached error page" in text:
            return False  # страница не открылась из-за сети — сам браузер при этом цел
        return any(marker in text for marker in self._GONE_MARKERS)

    def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        url = f"{self.base_url}/session/{self.session_id}{path}"
        try:
            if method == "POST":
                return http_post(url, payload if payload is not None else {}, timeout=timeout)
            return http_get(url, timeout=timeout)
        except WebDriverError as exc:
            if self._is_gone_error(exc) or not self.is_alive():
                raise BrowserGoneError(f"{exc}") from exc
            raise
        except (urllib.error.URLError, OSError) as exc:
            # Это НЕ интернет: сломалась связь с локальным geckodriver.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                if not self.is_alive():
                    raise BrowserGoneError(f"geckodriver не отвечает ({exc})") from exc
                raise DriverUnresponsiveError(f"geckodriver не ответил за {timeout:.0f}с") from exc
            raise BrowserGoneError(f"нет связи с geckodriver ({exc})") from exc

    def navigate(self, url: str) -> None:
        self._call("POST", "/url", {"url": url}, timeout=60.0)

    def execute_script(self, script: str, args: list | None = None):
        resp = self._call("POST", "/execute/sync", {"script": script, "args": args or []}, timeout=30.0)
        return resp.get("value")

    def quit(self) -> None:
        """Закрывает браузер и подчищает всё. Безопасно вызывать повторно и на
        уже «мёртвой» сессии — никогда не бросает исключений."""
        try:
            if self.session_id and self.proc is not None and self.proc.poll() is None:
                http_delete(f"{self.base_url}/session/{self.session_id}")
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        # Подчистка на случай, если firefox.exe (и его контент-процессы)
        # не завершился вместе с сессией geckodriver. Проверка через ProcessWatch
        # (handle удерживает PID) — исключает убийство постороннего процесса, если
        # PID уже перешёл к другой программе.
        try:
            if self.firefox_pid and (self._watch.is_running() if self._watch is not None
                                     else win_process.is_pid_alive(self.firefox_pid)):
                win_process.kill_process_tree(self.firefox_pid)
        except Exception:
            pass
        if self._watch is not None:
            try:
                self._watch.close()
            except Exception:
                pass
        if self._profile_copy_dir is not None:
            shutil.rmtree(self._profile_copy_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Логика проигрывания/записи
# --------------------------------------------------------------------------

def prime_and_wait_for_playback(session: GeckoSession, playback_rate: float, poll_interval: float = 0.3, max_wait: float = 600.0) -> dict:
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
    raise PlaybackWaitTimeout("Не дождался начала воспроизведения трека.")


class RecordingGate:
    """Физически управляет тем, попадают ли байты, прочитанные из
    аудио-захвата, в файл записи прямо сейчас (см. _relay_audio_to_encoder)."""

    def __init__(self, initially_open: bool = True) -> None:
        self._open = threading.Event()
        if initially_open:
            self._open.set()

    def open(self) -> None:
        self._open.set()

    def close(self) -> None:
        self._open.clear()

    def is_open(self) -> bool:
        return self._open.is_set()


def _relay_audio_to_encoder(
    capture_stdout, ffmpeg_stdin, gate: RecordingGate, stop_event: threading.Event,
    frame_size: int, chunk_size: int = 4096,
) -> None:
    """Непрерывно читает сырой PCM из захвата (не давая ему ни на миг
    зависнуть на буфере) и передаёт байты в stdin ffmpeg ТОЛЬКО пока
    gate.is_open() — то есть пока плеер реально проигрывает трек.

    Как и в Linux-версии, ведётся буфер remainder, чтобы разбиение на
    чтения не "сдвигало по фазе" сэмпл-фреймы (frame_size = каналы *
    байт/сэмпл реального формата захвата, см. win_audio_loopback.py)."""
    remainder = b""
    try:
        while not stop_event.is_set():
            chunk = capture_stdout.read(chunk_size)
            if not chunk:
                break
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
    transient_errors = 0

    while time.monotonic() < deadline:
        try:
            state = session.execute_script(script)
            transient_errors = 0
        except BrowserGoneError as exc:
            # Firefox закрылся прямо во время записи. Не теряем уже записанное:
            # возвращаем причину, а запись останавливается и сохраняется как обычно.
            print(
                f"WARNING: Firefox закрылся во время записи ({exc}) — "
                "сохраняю то, что успело записаться.", file=sys.stderr,
            )
            return "browser_closed"
        except Exception as exc:
            # Разовый сбой одной команды (страница перестраивается, обрыв сети и т.п.)
            # не должен обрывать запись трека — терпим несколько подряд.
            transient_errors += 1
            if transient_errors >= 10:
                raise
            print(f"WARNING: не удалось опросить плеер ({str(exc)[:200]}), повторяю...", file=sys.stderr)
            time.sleep(1.0)
            continue
        if state is None:
            time.sleep(poll_interval)
            continue

        if state.get("ended"):
            return "ended"

        current_src = state.get("src", "")
        current_time = state.get("currentTime")
        user_paused = bool(state.get("paused"))

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
            return "track_changed"

        if user_paused:
            if gate.is_open():
                gate.close()
                print("Воспроизведение поставлено на паузу — запись в wav физически остановлена...")
            deadline += poll_interval
        elif not gate.is_open():
            if really_playing:
                gate.open()
                print("Воспроизведение продолжено — запись в wav физически возобновлена...")
            else:
                deadline += poll_interval

        time.sleep(poll_interval)

    print("WARNING: таймаут ожидания окончания/смены трека — останавливаю запись по расчётному времени.", file=sys.stderr)
    return "timeout"


def probe_wav_duration(ffmpeg_bin: str, path: Path) -> float | None:
    """Достаёт длительность файла из вывода 'ffmpeg -i <path>' (без ffprobe) —
    используется только как диагностическая сверка после записи (см.
    record_and_save_track), не как основной способ хронометража."""
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
    capture_factory,
    stop_event: "threading.Event | None" = None,
) -> Path:
    """Ждёт начала воспроизведения одного трека, записывает его в WAV (в
    реальном формате захвата WASAPI), затем при необходимости кодирует в
    выбранный пользователем контейнер, и сохраняет итоговый файл.
    Возвращает путь к сохранённому файлу.

    capture_factory — функция без аргументов, создающая и .start()-ующая
    НОВЫЙ объект захвата (ProcessLoopbackCapture/DefaultDeviceLoopbackCapture)
    для этого конкретного трека."""
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
        print(f"Длительность оригинала: {duration:.1f}s -> ожидаемое время записи: {expected_capture_time:.1f}s")

    if raw_wav.exists():
        print(f"Удаляю старый временный файл записи: {raw_wav}")
        raw_wav.unlink()

    capture = None
    ffmpeg_proc = None
    relay_thread = None
    relay_stop_event = threading.Event()
    gate = RecordingGate(initially_open=True)
    try:
        capture = capture_factory()
        print(
            f"Запускаю запись (WASAPI [{capture.description}] -> [gate] -> ffmpeg -> "
            f"WAV {RAW_CAPTURE_RATE}Hz/{RAW_CAPTURE_BIT_DEPTH}bit)..."
        )
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_raw_wav_args_dynamic(
                ffmpeg_bin, raw_wav, capture.sample_rate, capture.channels,
                capture.sample_fmt,
            ),
            stdin=subprocess.PIPE,
        )
        relay_thread = threading.Thread(
            target=_relay_audio_to_encoder,
            args=(capture.stdout, ffmpeg_proc.stdin, gate, relay_stop_event, capture.bytes_per_frame),
            daemon=True,
        )
        relay_thread.start()

        reason = "interrupted"
        try:
            reason = wait_for_track_boundary(session, info.get("src", ""), expected_capture_time, gate)
        except KeyboardInterrupt:
            if stop_event is not None:
                stop_event.set()
        print(f"Событие окончания записи: {reason}")

        print("Останавливаю запись...")
        relay_stop_event.set()
        capture.stop(timeout=10)
        if capture.error is not None:
            print(f"WARNING: захват звука прервался с ошибкой: {capture.error}", file=sys.stderr)

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
        if capture is not None and capture.poll() is None:
            capture.stop(timeout=5)
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
    if capture is not None and capture.total_frames > 0 and capture.audible_frames == 0:
        print(
            "WARNING: в записи только тишина — Firefox не отдал звук выбранному захвату. "
            "Проверьте, что трек действительно играет и не заглушён в микшере громкости; "
            "если повторяется — попробуйте аварийный режим (--capture-mode device).",
            file=sys.stderr,
        )

    # Если запись велась на ускоренной скорости (--rate != 1.0), звук в
    # raw_wav звучит быстрее и выше по тону, чем оригинал — 'atempo'
    # возвращает нормальную скорость/тон без потери качества (в отличие
    # от банальной смены частоты дискретизации).
    tempo_filter_args: list[str] = []
    if args.rate != 1.0:
        print(f"Замедляю обратно (atempo={1 / args.rate:.4f})...")
        tempo_filter_args = ["-filter:a", f"atempo={1 / args.rate}"]

    if args.container:
        print(f"Кодирую в {args.container} (из WAV {RAW_CAPTURE_RATE}Hz/{RAW_CAPTURE_BIT_DEPTH}bit)...")
    else:
        print(f"Контейнер не выбран — оставляю как WAV {RAW_CAPTURE_RATE}Hz/{RAW_CAPTURE_BIT_DEPTH}bit (без перекодирования).")

    # Если нужен atempo (rate != 1.0), а контейнер не выбран (чистый WAV),
    # atempo всё равно нужно применить — encode_final_audio() в этом
    # случае просто переименует raw_wav, поэтому atempo с пустым
    # container обрабатываем отдельным проходом ffmpeg заранее.
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

    result_path = encode_final_audio(
        ffmpeg_bin, raw_wav, final_out, args.container,
        extra_filter_args=tempo_filter_args if args.container else [],
    )
    print(f"Готово: {result_path}")
    return result_path


def list_audio_devices() -> int:
    """Диагностика звука (кнопка «Проверить звуковые устройства» в GUI):
    версия Windows, устройства вывода/ввода Core Audio, реальная проверка
    запуска захвата (Process Loopback и аварийный режим). Реализация — в
    win_audio_devices.py."""
    return run_audio_diagnostics()


def setup_control_channel(control_stdin: bool) -> None:
    """Подготовка к работе под управлением GUI (общая для Suno- и YouTube-воркера):
    предупреждение, если stdout недоступен и журнал идёт в файл, и — при флаге
    --control-stdin — запуск слушателя команды STOP в stdin (см. win_stdio.py)."""
    if _STDIO_LOG_FILE is not None:
        print(f"WARNING: стандартный вывод недоступен, лог дублируется в файл: {_STDIO_LOG_FILE}")
    if control_stdin:
        def _on_stop_command(reason: str) -> None:
            if reason == "stop":
                print("\nПолучена команда остановки от интерфейса.")
            else:
                print("\nКанал управления закрыт (интерфейс завершился) — останавливаюсь.")
            win_stdio.interrupt_main_thread(reason)

        if win_stdio.start_stdin_stop_listener(_on_stop_command) is None:
            print("WARNING: канал управления (stdin) недоступен — остановка из интерфейса "
                  "будет принудительной.", file=sys.stderr)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="https://suno.com/create",
                     help="Ссылка, откуда стартовать (по умолчанию — ваша библиотека Suno)")
    ap.add_argument("out_name", nargs="?", default=None,
                     help="Имя выходного файла. Если не указано — берётся из названия трека")
    ap.add_argument("-d", "--destination", default=".", help="Папка сохранения (по умолчанию текущая)")
    ap.add_argument("--profile", default=None, help="Путь к профилю Firefox (если не задан — автоматически)")
    ap.add_argument("--firefox-binary", default=None, help="Путь к firefox.exe (если не задан — определяется автоматически)")
    ap.add_argument("--rate", type=float, default=1.0,
                     help="Ускорение воспроизведения при записи (по умолчанию 1.0)")
    ap.add_argument("--headless", action="store_true", help="Запуск без окна")
    ap.add_argument("--no-profile-copy", action="store_true",
                     help="Использовать профиль напрямую без копирования")
    ap.add_argument("--no-firefox-cache", action="store_true",
                     help="Не использовать сохранённую с прошлого запуска копию профиля/бинарника")
    ap.add_argument("--reset-firefox-cache", action="store_true",
                     help="Стереть сохранённую копию профиля и путь к firefox.exe, затем выйти")
    ap.add_argument("--no-auto-ffmpeg", action="store_true",
                     help="Не скачивать сборку ffmpeg автоматически")
    ap.add_argument("--container", default=None, choices=[c.id for c in all_containers()],
                     help="В какой контейнер кодировать после записи. Если не задано — WAV без перекодирования.")
    ap.add_argument("--capture-mode", default="process", choices=["process", "device"],
                     help="'process' (по умолчанию) — изолированный захват только звука Firefox "
                          "(WASAPI Process Loopback, нужна Windows 10 2004+/11). 'device' — аварийный "
                          "fallback без изоляции: пишет ВЕСЬ звук системы (используйте, только если "
                          "'process' не работает на вашей машине).")
    ap.add_argument("--list-audio-devices", action="store_true",
                     help="Диагностика звука: устройства Windows + проверка запуска захвата, затем выйти")
    ap.add_argument("--control-stdin", action="store_true",
                     help="Служебный флаг (его ставит GUI): слушать stdin — строка STOP или закрытие "
                          "канала останавливает запись штатно (вместо CTRL_BREAK, у GUI нет консоли)")
    args = ap.parse_args()

    if args.list_audio_devices:
        return list_audio_devices()

    if args.reset_firefox_cache:
        clear_firefox_cache()
        print("Кэш профиля/бинарника Firefox очищен.")
        return 0

    setup_control_channel(args.control_stdin)

    profile_path, firefox_binary, use_profile_copy = resolve_firefox_launch_plan(
        args.profile, args.firefox_binary, use_cache=not args.no_firefox_cache,
    )
    if args.no_profile_copy:
        use_profile_copy = False

    dest_dir = Path(args.destination).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = resolve_ffmpeg_bin(args.container, auto_download=not args.no_auto_ffmpeg)
    print(f"Использую ffmpeg: {ffmpeg_bin}")
    print(f"Контейнер: {args.container or 'не выбран -> WAV по умолчанию'}")

    def launch_session() -> GeckoSession:
        return GeckoSession(
            profile_path, headless=args.headless, firefox_binary=firefox_binary,
            use_profile_copy=use_profile_copy,
        )

    print("Запускаю geckodriver + firefox.exe...")
    session = launch_session()

    if args.capture_mode == "device":
        print(
            "WARNING: --capture-mode device пишет ВЕСЬ звук системы, а не только "
            "Firefox — используйте только как аварийный вариант.", file=sys.stderr,
        )

        def capture_factory():
            cap = DefaultDeviceLoopbackCapture()
            cap.start()
            return cap
    else:
        print(f"Настраиваю изоляцию звука (запись только из этого Firefox, pid={session.firefox_pid})...")
        print(
            f"Захват изолирован: только звук firefox.exe (pid={session.firefox_pid}) "
            "и его дочерних процессов (WASAPI Process Loopback Capture)."
        )

        def capture_factory():
            # session — переменная main(): после перезапуска браузера здесь
            # автоматически используется PID НОВОГО firefox.exe.
            cap = ProcessLoopbackCapture(session.firefox_pid, include_tree=True)
            cap.start()
            return cap

    saved_tracks: list[Path] = []
    stop_event = threading.Event()
    capture_failures = 0        # подряд идущие сбои ЗАПУСКА захвата
    MAX_CAPTURE_FAILURES = 3
    fatal_capture_error = False

    MAX_BROWSER_RESTARTS = 5    # подряд перезапусков браузера без единого сохранённого трека
    restart_streak = 0
    MAX_CONSECUTIVE_ERRORS = 5  # подряд необъяснимых ошибок при живом браузере -> перезапуск
    consecutive_errors = 0
    fatal_browser_error = False
    session_broken: str | None = None   # причина, по которой браузер нужно перезапустить
    needs_open = True                   # нужно (заново) открыть стартовую страницу

    def restart_session(reason: str) -> bool:
        """Перезапускает Firefox после закрытия/падения. False — сдаёмся."""
        nonlocal session, restart_streak
        print(f"ВНИМАНИЕ: браузер недоступен ({reason}).", file=sys.stderr)
        print(session.describe_failure(), file=sys.stderr)
        if time.monotonic() - session.started_at > 600:
            restart_streak = 0   # прошлый запуск проработал долго — это не «цикл падений»
        restart_streak += 1
        if restart_streak > MAX_BROWSER_RESTARTS:
            print(
                f"ERROR: Firefox закрывается {MAX_BROWSER_RESTARTS} раз подряд без единого "
                "сохранённого трека — дальнейшие перезапуски бессмысленны. Проверьте отчёт выше "
                "(код выхода / отчёт о падении) и лог geckodriver.", file=sys.stderr,
            )
            return False
        delay = min(3 * restart_streak, 15)
        print(f"Перезапускаю браузер (попытка {restart_streak}/{MAX_BROWSER_RESTARTS}) через {delay}с...")
        session.quit()
        time.sleep(delay)
        session = launch_session()
        return True

    try:
        while True:
            try:
                if session_broken is not None:
                    reason = session_broken
                    session_broken = "не удалось перезапустить браузер"   # если запуск упадёт — останемся «сломанными»
                    if not restart_session(reason):
                        fatal_browser_error = True
                        break
                    session_broken = None
                    needs_open = True

                if needs_open:
                    navigate_with_retry(session, args.url)
                    print(f"Открыл {args.url}.")
                    print(
                        "Скрипт теперь работает непрерывно: после сохранения трека он "
                        "не завершится, а будет ждать следующего нажатия play. "
                        "Чтобы остановить скрипт — Ctrl+C."
                    )
                    needs_open = False

                final_out = record_and_save_track(
                    session, args, ffmpeg_bin, dest_dir, capture_factory, stop_event,
                )
                saved_tracks.append(final_out)
                capture_failures = 0
                consecutive_errors = 0
                restart_streak = 0
                if stop_event.is_set():
                    print("\nОстановлено пользователем — запись сохранена, завершаю сессию.")
                    break
                if not session.is_alive():
                    session_broken = "Firefox закрылся во время записи"
                    continue
                print("Жду следующий трек (нажмите play в Suno)...")
            except KeyboardInterrupt:
                raise
            except PlaybackWaitTimeout:
                print("Воспроизведение так и не началось — продолжаю ждать (нажмите play в Suno)...")
                continue
            except LoopbackCaptureError as exc:
                if not session.is_alive():
                    # Захват «не запустился» потому, что Firefox уже закрыт — это не
                    # проблема версии Windows, счётчик сбоев захвата не трогаем.
                    session_broken = f"Firefox закрыт ({exc})"
                    continue
                capture_failures += 1
                print(f"ERROR: не удалось запустить захват звука "
                      f"(попытка {capture_failures}/{MAX_CAPTURE_FAILURES}): {exc}", file=sys.stderr)
                if capture_failures >= MAX_CAPTURE_FAILURES:
                    print(
                        "ERROR: захват звука Firefox не запускается — дальнейшие повторы бессмысленны. "
                        "Нужна Windows 10 версии 2004 (сборка 19041) или новее / Windows 11. "
                        "Аварийный вариант без изоляции (пишет ВЕСЬ звук системы): --capture-mode device "
                        "(в GUI — чекбокс «Аварийный режим»).", file=sys.stderr,
                    )
                    fatal_capture_error = True
                    break
                time.sleep(3)
                continue
            except Exception as exc:
                if isinstance(exc, BrowserGoneError) or not session.is_alive():
                    session_broken = str(exc) or "Firefox закрылся"
                    continue
                consecutive_errors += 1
                if _looks_like_network_error(exc):
                    print(f"Похоже, во время работы с треком пропал интернет ({exc}).", file=sys.stderr)
                    wait_for_internet()
                    print("Продолжаю ждать треки...")
                else:
                    print(f"ERROR при обработке трека: {exc}", file=sys.stderr)
                    print("Скрипт не завершает работу — жду следующий трек...", file=sys.stderr)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    consecutive_errors = 0
                    session_broken = f"{MAX_CONSECUTIVE_ERRORS} ошибок подряд, браузер не отвечает как надо"
                    continue
                time.sleep(2)
                continue
    except KeyboardInterrupt:
        print("\nОстановлено пользователем (Ctrl+C).")
    finally:
        session.quit()

    if not saved_tracks:
        print("ERROR: не удалось сохранить ни одного трека.", file=sys.stderr)
        return 2 if fatal_capture_error else (3 if fatal_browser_error else 1)

    print(f"Всего сохранено треков за сессию: {len(saved_tracks)}")
    for path in saved_tracks:
        print(f"  - {path}")
    return 3 if fatal_browser_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
