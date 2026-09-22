#!/usr/bin/env python3
"""
win_stdio.py — вспомогательный модуль для работы БЕЗ окна консоли.

Зачем он нужен
--------------
Раньше AutoRecord.exe собирался как консольное приложение, поэтому за окном
программы всегда висело второе, пустое чёрное окно (консоль). Штатные способы
его спрятать не годятся: у PyInstaller опция --hide-console не срабатывает на
Windows 11 с Windows Terminal (issue #8022), а ShowWindow(GetConsoleWindow())
прячет только «классический» conhost. Поэтому программа теперь собирается как
windowed (GUI) приложение — консоли нет вообще. Но у такой сборки есть три
известных особенности, которые закрывает этот модуль:

1. sys.stdin / sys.stdout / sys.stderr в windowed-сборке могут быть None
   (PyInstaller >= 5.7 оставляет их None, как pythonw.exe). Любой sys.stdout.flush()
   или sys.stderr.write() тогда падает с AttributeError. ensure_std_streams()
   подставляет рабочие потоки: сперва пытается взять реальные дескрипторы,
   которые передал родитель (GUI запускает воркер с pipe'ами — GetStdHandle
   их видит, даже если Python их не подхватил), иначе — файл журнала / os.devnull.

2. Консольные программы (geckodriver.exe, ffmpeg.exe, taskkill.exe), запущенные
   из процесса БЕЗ консоли, получают собственное НОВОЕ окно консоли — на экране
   мигают/висят чёрные окна. install_no_window_popen() один раз подменяет
   subprocess.Popen так, чтобы все такие запуски шли с CREATE_NO_WINDOW. Заодно
   stdin по умолчанию = DEVNULL: иначе ffmpeg унаследовал бы канал управления
   (stdin воркера) и «съел» бы команду остановки.

3. Остановить воркер через CTRL_BREAK_EVENT (как раньше) нельзя — у GUI нет
   консоли, а GenerateConsoleCtrlEvent работает только внутри одной консоли.
   Вместо этого GUI пишет строку STOP в stdin воркера, а воркер слушает stdin в
   отдельном потоке (start_stdin_stop_listener) и по команде поднимает
   KeyboardInterrupt в главном потоке — ровно тот же путь, что и раньше
   (finally -> session.quit() -> корректное закрытие firefox.exe). Бонус: если
   GUI аварийно завершился, канал закрывается (EOF) — воркер тоже останавливается
   сам, а не остаётся невидимым фоновым процессом.

Модуль безопасно импортируется и на не-Windows (нужно для тестов логики).
"""

from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

_IS_WIN = sys.platform == "win32"

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12

STOP_COMMAND = "STOP"

# Куда воркер пишет вывод, если ни один из его стандартных дескрипторов не
# оказался рабочим (аварийный вариант, чтобы причину всё равно можно было
# прочитать). Путь совпадает с папкой состояния остальной программы.
WORKER_LOG_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "auto_record_suno" / "worker-stdio.log"

# True, если вместо настоящего stdin подставлена «заглушка» (os.devnull):
# тогда канал управления недоступен, и читать из него нельзя — сразу был бы EOF,
# который воркер принял бы за команду остановки.
STDIN_IS_FALLBACK = False

_shared_log_stream = None


# --------------------------------------------------------------------------
# kernel32 (лениво, чтобы модуль импортировался не только на Windows)
# --------------------------------------------------------------------------

def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.GetStdHandle.argtypes = [ctypes.c_uint32]
    k32.GetStdHandle.restype = ctypes.c_void_p
    k32.GetFileType.argtypes = [ctypes.c_void_p]
    k32.GetFileType.restype = ctypes.c_uint32
    k32.GetConsoleWindow.argtypes = []
    k32.GetConsoleWindow.restype = ctypes.c_void_p
    return k32


def _os_std_handle(std_id: int) -> "int | None":
    """Возвращает рабочий OS-дескриптор стандартного потока или None."""
    if not _IS_WIN:
        return None
    try:
        k32 = _kernel32()
        handle = k32.GetStdHandle(std_id & 0xFFFFFFFF)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            return None
        if k32.GetFileType(handle) == 0:      # FILE_TYPE_UNKNOWN
            return None
        return int(handle)
    except Exception:  # noqa: BLE001
        return None


def has_console_window() -> bool:
    """True, если у процесса есть видимое/невидимое окно консоли."""
    if not _IS_WIN:
        return False
    try:
        return bool(_kernel32().GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 1. Стандартные потоки
# --------------------------------------------------------------------------

def _stream_from_os_handle(handle: int, writable: bool):
    import msvcrt  # только Windows

    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY if writable else os.O_RDONLY)
    raw = io.open(fd, "wb" if writable else "rb", buffering=0, closefd=False)
    if writable:
        return io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8", errors="replace",
                                line_buffering=True, write_through=True)
    return io.TextIOWrapper(io.BufferedReader(raw), encoding="utf-8", errors="replace")


def _open_shared_log():
    global _shared_log_stream
    if _shared_log_stream is None:
        WORKER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _shared_log_stream = open(WORKER_LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    return _shared_log_stream


def ensure_std_streams(prefer_log_file: bool = False) -> "Path | None":
    """Гарантирует, что sys.stdin/stdout/stderr — не None.

    Порядок: (1) то, что уже есть; (2) реальные дескрипторы, унаследованные от
    родителя (GUI запускает воркер с pipe'ами); (3) для stdout/stderr — файл
    журнала (если prefer_log_file), иначе os.devnull. Возвращает путь к файлу
    журнала, если он понадобился, иначе None."""
    global STDIN_IS_FALLBACK
    used_log: "Path | None" = None
    for name, std_id, writable in (("stdin", STD_INPUT_HANDLE, False),
                                   ("stdout", STD_OUTPUT_HANDLE, True),
                                   ("stderr", STD_ERROR_HANDLE, True)):
        if getattr(sys, name, None) is not None:
            continue
        stream = None
        handle = _os_std_handle(std_id)
        if handle is not None:
            try:
                stream = _stream_from_os_handle(handle, writable)
            except Exception:  # noqa: BLE001
                stream = None
        if stream is None:
            if writable and prefer_log_file:
                try:
                    stream = _open_shared_log()
                    used_log = WORKER_LOG_PATH
                except OSError:
                    stream = None
            if stream is None:
                stream = open(os.devnull, "w" if writable else "r", encoding="utf-8")
            if not writable:
                STDIN_IS_FALLBACK = True
        setattr(sys, name, stream)
    return used_log


# --------------------------------------------------------------------------
# 2. Запуск подпроцессов без окон консоли
# --------------------------------------------------------------------------

def apply_windowless_defaults(kwargs: dict, positional_count: int = 0,
                              stdout_ok: bool = True, stderr_ok: bool = True) -> dict:
    """Дополняет kwargs для subprocess.Popen (изменяет и возвращает его):
    CREATE_NO_WINDOW; stdin по умолчанию DEVNULL; stdout/stderr по умолчанию
    DEVNULL, только если унаследовать собственные дескрипторы нельзя."""
    flags = kwargs.get("creationflags", 0) or 0
    if not (flags & (DETACHED_PROCESS | CREATE_NEW_CONSOLE)):
        flags |= CREATE_NO_WINDOW
    kwargs["creationflags"] = flags
    # Popen(args, bufsize, executable, stdin, stdout, stderr, ...) — если std-потоки
    # переданы позиционно, не вмешиваемся.
    if positional_count < 4 and kwargs.get("stdin") is None:
        kwargs["stdin"] = subprocess.DEVNULL
    if positional_count < 5 and kwargs.get("stdout") is None and not stdout_ok:
        kwargs["stdout"] = subprocess.DEVNULL
    if positional_count < 6 and kwargs.get("stderr") is None and not stderr_ok:
        kwargs["stderr"] = subprocess.DEVNULL
    return kwargs


_popen_patched = False


def install_no_window_popen(only_without_console: bool = True) -> bool:
    """Подменяет subprocess.Popen.__init__ (см. п. 2 в докстринге модуля).
    Если only_without_console=True и у процесса есть консоль (запуск из
    терминала), ничего не меняет: дочерние программы и так работают в ней.
    Возвращает True, если подмена установлена."""
    global _popen_patched
    if not _IS_WIN or _popen_patched:
        return False
    if only_without_console and has_console_window():
        return False
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        apply_windowless_defaults(
            kwargs, positional_count=len(args),
            stdout_ok=_os_std_handle(STD_OUTPUT_HANDLE) is not None,
            stderr_ok=_os_std_handle(STD_ERROR_HANDLE) is not None,
        )
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched  # type: ignore[method-assign]
    _popen_patched = True
    return True


# --------------------------------------------------------------------------
# 3. Канал управления: команда STOP через stdin
# --------------------------------------------------------------------------

def start_stdin_stop_listener(on_stop: "Callable[[str], None] | None" = None) -> "threading.Thread | None":
    """Запускает поток, читающий stdin построчно. Строка STOP (или EOF — родитель
    закрыл канал/умер) => on_stop(причина), по умолчанию — KeyboardInterrupt в
    главном потоке. Вызывать из ГЛАВНОГО потока. None — если канала нет."""
    if sys.stdin is None or STDIN_IS_FALLBACK:
        return None
    if on_stop is None:
        on_stop = interrupt_main_thread
    try:
        import signal
        # CREATE_NEW_PROCESS_GROUP отключает Ctrl+C у процесса — убеждаемся, что
        # обработчик SIGINT (default_int_handler) на месте, иначе interrupt_main
        # ничего бы не сделал.
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass

    def loop() -> None:
        reason = "eof"
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip().upper() == STOP_COMMAND:
                    reason = "stop"
                    break
        except (OSError, ValueError):
            pass
        try:
            on_stop(reason)
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=loop, name="stdin-stop-listener", daemon=True)
    thread.start()
    return thread


def interrupt_main_thread(reason: str) -> None:
    import _thread
    _thread.interrupt_main()
