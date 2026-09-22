#!/usr/bin/env python3
"""
win_process.py

Замена линуксового чтения /proc на Windows: перечисление процессов через
ctypes + WinAPI (CreateToolhelp32Snapshot / Process32First / Process32Next),
получение полного пути исполняемого файла процесса
(QueryFullProcessImageNameW) и корректное завершение дерева процессов
(taskkill /T /F — самый надёжный штатный способ убить процесс со всеми
его потомками на Windows, без необходимости самому обходить дерево
дочерних процессов).

Только стандартная библиотека Python (ctypes, subprocess) — без pip
зависимостей.

ВНИМАНИЕ: этот модуль написан по документированному поведению WinAPI, но
не был протестирован на реальной Windows-машине (разработка велась в
Linux-контейнере). Перед использованием в проде стоит прогнать
find_processes_by_name()/get_process_exe_path()/get_children() на
реальной Windows 10/11 и, при необходимости, поправить сигнатуры ctypes.
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None

STILL_ACTIVE = 259  # GetExitCodeProcess вернул это значение = процесс ещё работает

# Отдельный экземпляр kernel32 со СВОИМИ прототипами функций (argtypes/restype),
# чтобы не менять глобально общие функции ctypes.windll.kernel32, которыми
# пользуются другие части программы. Явные прототипы нужны, чтобы 64-битный
# HANDLE не обрезался до 32 бит.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
if _k32 is not None:
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str  # только имя файла, напр. "firefox.exe" (без пути)


def _require_windows() -> None:
    if kernel32 is None:
        raise RuntimeError(
            "win_process.py работает только на Windows (нужен ctypes.windll)."
        )


def list_processes() -> list[ProcInfo]:
    """Перечисляет все видимые текущему пользователю процессы через
    CreateToolhelp32Snapshot — аналог обхода /proc в линуксовой версии."""
    _require_windows()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    entries: list[ProcInfo] = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while found:
            name = entry.szExeFile.decode("mbcs", errors="replace")
            entries.append(ProcInfo(pid=entry.th32ProcessID, ppid=entry.th32ParentProcessID, name=name))
            found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return entries


def find_processes_by_name(exe_name: str) -> list[ProcInfo]:
    """Ищет процессы по имени исполняемого файла (без учёта регистра),
    напр. find_processes_by_name('firefox.exe')."""
    exe_name = exe_name.lower()
    return [p for p in list_processes() if p.name.lower() == exe_name]


def get_children(pid: int, all_procs: list[ProcInfo] | None = None) -> list[int]:
    """Возвращает PID всех потомков (рекурсивно) данного процесса —
    аналог get_descendant_pids() из линуксовой версии, но по снимку
    Toolhelp32 вместо обхода /proc."""
    procs = all_procs if all_procs is not None else list_processes()
    by_ppid: dict[int, list[int]] = {}
    for p in procs:
        by_ppid.setdefault(p.ppid, []).append(p.pid)

    result: list[int] = []
    stack = [pid]
    seen = {pid}
    while stack:
        current = stack.pop()
        for child_pid in by_ppid.get(current, []):
            if child_pid not in seen:
                seen.add(child_pid)
                result.append(child_pid)
                stack.append(child_pid)
    return result


def get_process_exe_path(pid: int) -> str | None:
    """Возвращает полный путь к исполняемому файлу процесса через
    QueryFullProcessImageNameW — аналог чтения /proc/<pid>/exe в
    линуксовой версии. Возвращает None, если процесс уже завершился или
    доступ запрещён (например, процесс запущен от другого пользователя)."""
    _require_windows()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf_len = wintypes.DWORD(MAX_PATH)
        buf = ctypes.create_unicode_buffer(MAX_PATH)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len))
        if not ok:
            return None
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def is_pid_alive(pid: int) -> bool:
    """Работает ли процесс СЕЙЧАС.

    Раньше здесь было «OpenProcess удался -> жив», но это неверно: пока кто-то
    держит открытый handle на уже завершившийся процесс (а geckodriver держит
    handle на firefox.exe), OpenProcess продолжает успешно открываться, и
    закрывшийся Firefox выглядел «живым». Теперь дополнительно проверяется
    код завершения (STILL_ACTIVE = ещё работает)."""
    _require_windows()
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # узнать не удалось — считаем живым
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


class ProcessWatch:
    """Держит открытый handle на процесс, чтобы в любой момент узнать, жив ли
    он, и — после завершения — его КОД ВЫХОДА (по нему видно: закрыли штатно
    (0) или процесс упал (0xC0000005 и т.п.)). Handle, открытый заранее,
    также защищает от путаницы при повторном использовании PID."""

    def __init__(self, pid: int) -> None:
        _require_windows()
        self.pid = pid
        self._exit_code: int | None = None
        self._handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) or None

    def exit_code(self) -> int | None:
        """None — процесс ещё работает (или узнать не удалось)."""
        if self._exit_code is not None:
            return self._exit_code
        if not self._handle:
            return None
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            return None
        if code.value == STILL_ACTIVE:
            return None
        self._exit_code = int(code.value)
        return self._exit_code

    def is_running(self) -> bool:
        if not self._handle:
            return is_pid_alive(self.pid)
        return self.exit_code() is None

    def close(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


def kill_process_tree(pid: int, timeout: float = 10.0) -> None:
    """Завершает процесс и всё его дерево потомков через 'taskkill /T /F'
    — штатная и самая надёжная на Windows команда для этого (сама находит
    и убивает все дочерние процессы по дереву, без необходимости обходить
    его вручную через win_process.get_children()). Используется как
    аварийная подчистка (например, если GeckoSession.quit() через
    WebDriver не сработал) — обычный путь остановки Firefox остаётся
    штатным session.quit()."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
