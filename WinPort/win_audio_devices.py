#!/usr/bin/env python3
"""
win_audio_devices.py — диагностика звуковых устройств Windows.

Реализует кнопку «Проверить звуковые устройства» в GUI (и флаг
--list-audio-devices у auto_record_suno.py). Аналог Linux-версии, где
печатается default sink и список sink'ов PulseAudio.

Что делает
----------
1. Показывает версию Windows и пригодность для Process Loopback (сборка >= 19041).
2. Перечисляет звуковые устройства через Core Audio (IMMDeviceEnumerator,
   чистый ctypes, как и остальной WASAPI-код проекта):
     - устройства ВЫВОДА (динамики/наушники/HDMI) — с состоянием (активно /
       отключено / не подключено), типом, форматом микшера Windows и пометкой,
       какое из них «по умолчанию» (именно его слушает аварийный режим);
     - устройства ВВОДА (микрофоны) — списком, для записи они не используются.
   Устройства в состоянии «отсутствует» (NOTPRESENT — давно удалённые) не
   показываются, чтобы не засорять вывод.
3. РЕАЛЬНО проверяет, что захват можно запустить — теми же классами, которыми
   пользуется запись (win_audio_loopback):
     - изоляция по процессу (WASAPI Process Loopback) — на PID самой утилиты
       (звука у неё нет, но активация и Initialize проходят/не проходят так же,
       как и для Firefox);
     - аварийный режим (loopback устройства вывода по умолчанию).
   Проверяется, что захват ИНИЦИАЛИЗИРУЕТСЯ и в каком формате; идёт ли реально
   звук — проверяется отдельно: python win_audio_loopback.py <PID_firefox> 5.
4. Печатает итог с понятной рекомендацией.

Модуль безопасно импортируется и на не-Windows (для тестов логики), но
перечисление устройств работает только на Windows.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from ctypes import POINTER, byref, c_int, c_uint16, c_uint32, c_void_p
from dataclasses import dataclass, field
from typing import Callable

import win_audio_loopback as wal
from win_audio_loopback import (
    CLSCTX_ALL, CLSID_MMDeviceEnumerator, COINIT_MULTITHREADED, GUID,
    IID_IAudioClient, IID_IMMDeviceEnumerator, LoopbackCaptureError,
    WAVEFORMATEX, WAVEFORMATEXTENSIBLE, WAVE_FORMAT_EXTENSIBLE, WAVE_FORMAT_IEEE_FLOAT,
    _call, _check, _release, hr_text, make_guid,
)

# --------------------------------------------------------------------------
# Константы Core Audio
# --------------------------------------------------------------------------

E_RENDER, E_CAPTURE = 0, 1
ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS = 0, 1, 2
ROLE_LABELS = {ROLE_CONSOLE: "консоль", ROLE_MULTIMEDIA: "мультимедиа", ROLE_COMMUNICATIONS: "связь"}

DEVICE_STATE_ACTIVE, DEVICE_STATE_DISABLED, DEVICE_STATE_NOTPRESENT, DEVICE_STATE_UNPLUGGED = 1, 2, 4, 8
# NOTPRESENT (давно удалённые «призраки») намеренно не запрашиваем.
SHOWN_STATES = DEVICE_STATE_ACTIVE | DEVICE_STATE_DISABLED | DEVICE_STATE_UNPLUGGED
STATE_LABELS = {
    DEVICE_STATE_ACTIVE: "активно",
    DEVICE_STATE_DISABLED: "ОТКЛЮЧЕНО в настройках Windows",
    DEVICE_STATE_UNPLUGGED: "НЕ ПОДКЛЮЧЕНО (кабель/разъём)",
    DEVICE_STATE_NOTPRESENT: "отсутствует",
}

STGM_READ = 0
VT_UI4, VT_LPWSTR = 19, 31
E_NOTFOUND = 0x80070490
MIN_PROCESS_LOOPBACK_BUILD = 19041   # Windows 10 версии 2004

FORM_FACTORS = {
    0: "удалённое сетевое устройство", 1: "колонки", 2: "линейный вход/выход", 3: "наушники",
    4: "микрофон", 5: "гарнитура", 6: "телефонная трубка", 7: "цифровой passthrough",
    8: "S/PDIF", 9: "HDMI / DisplayPort", 10: "тип неизвестен",
}

KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = make_guid("{00000003-0000-0010-8000-00AA00389B71}")

# индексы методов vtable
_ENUM_ENUM_ENDPOINTS, _ENUM_GET_DEFAULT = 3, 4
_COLL_GET_COUNT, _COLL_ITEM = 3, 4
_DEV_ACTIVATE, _DEV_OPEN_PROPS, _DEV_GET_ID, _DEV_GET_STATE = 3, 4, 5, 6
_PS_GET_VALUE = 5
_AC_GET_MIX_FORMAT = 8


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_uint32)]


class _PropVariantOut(ctypes.Structure):
    """PROPVARIANT для ЧТЕНИЯ: 8 байт заголовка + объединение значений
    (24 байта на x64 / 16 на x86 — у нас два поля c_void_p дают ровно столько)."""
    _fields_ = [("vt", c_uint16), ("r1", c_uint16), ("r2", c_uint16), ("r3", c_uint16),
                ("value", c_void_p), ("value2", c_void_p)]


PKEY_DEVICE_FRIENDLY_NAME = PROPERTYKEY(make_guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
PKEY_DEVICE_DESC = PROPERTYKEY(make_guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 2)
PKEY_ENDPOINT_FORM_FACTOR = PROPERTYKEY(make_guid("{1DA5D803-D492-4EDD-8E23-E0C0FFEE7F0E}"), 0)


@dataclass
class Endpoint:
    flow: int                          # E_RENDER / E_CAPTURE
    dev_id: str
    name: str
    state: int
    form_factor: "int | None" = None
    mix_format: "str | None" = None
    mix_error: "str | None" = None
    default_roles: tuple = ()          # подмножество ROLE_LABELS (только для устройств вывода/ввода по умолчанию)

    @property
    def is_active(self) -> bool:
        return self.state == DEVICE_STATE_ACTIVE


# --------------------------------------------------------------------------
# Низкоуровневые помощники
# --------------------------------------------------------------------------

def _ole32():
    return wal._ole32  # type: ignore[attr-defined]


def _co_task_free(ptr) -> None:
    if ptr:
        try:
            _ole32().CoTaskMemFree(ptr)
        except Exception:  # noqa: BLE001
            pass


def _read_prop(store: int, key: PROPERTYKEY):
    """Читает значение свойства: str (VT_LPWSTR), int (VT_UI4) или None."""
    pv = _PropVariantOut()
    hr = _call(store, _PS_GET_VALUE, (POINTER(PROPERTYKEY), POINTER(_PropVariantOut)), byref(key), byref(pv))
    if hr < 0:
        return None
    try:
        if pv.vt == VT_LPWSTR and pv.value:
            return ctypes.wstring_at(pv.value)
        if pv.vt == VT_UI4:
            return (pv.value or 0) & 0xFFFFFFFF
        return None
    finally:
        try:
            _ole32().PropVariantClear(byref(pv))
        except Exception:  # noqa: BLE001
            pass


def _device_id(device: int) -> str:
    p = c_void_p()
    if _call(device, _DEV_GET_ID, (POINTER(c_void_p),), byref(p)) < 0 or not p.value:
        return ""
    try:
        return ctypes.wstring_at(p.value)
    finally:
        _co_task_free(p.value)


def _describe_wfx(ptr: int) -> str:
    wfx = WAVEFORMATEX.from_address(ptr)
    is_float = wfx.wFormatTag == WAVE_FORMAT_IEEE_FLOAT
    bits = wfx.wBitsPerSample
    if wfx.wFormatTag == WAVE_FORMAT_EXTENSIBLE and wfx.cbSize >= 22:
        ext = WAVEFORMATEXTENSIBLE.from_address(ptr)
        is_float = bytes(ext.SubFormat) == bytes(KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
    kind = f"{bits} бит float" if is_float else f"{bits} бит"
    return f"{wfx.nSamplesPerSec} Гц, {wfx.nChannels} кан., {kind}"


def _mix_format(device: int) -> str:
    """Формат, в котором Windows микширует поток этого устройства."""
    client = c_void_p()
    _check(_call(device, _DEV_ACTIVATE, (POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p)),
                 byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client)), "IMMDevice::Activate")
    try:
        pwfx = c_void_p()
        _check(_call(client.value, _AC_GET_MIX_FORMAT, (POINTER(c_void_p),), byref(pwfx)), "GetMixFormat")
        try:
            return _describe_wfx(pwfx.value)
        finally:
            _co_task_free(pwfx.value)
    finally:
        _release(client.value)


def _default_endpoint_id(enumerator: int, flow: int, role: int) -> "str | None":
    device = c_void_p()
    hr = _call(enumerator, _ENUM_GET_DEFAULT, (c_int, c_int, POINTER(c_void_p)), flow, role, byref(device))
    if hr < 0 or not device.value:
        return None          # обычно E_NOTFOUND — нет ни одного устройства
    try:
        return _device_id(device.value)
    finally:
        _release(device.value)


def _read_endpoint(device: int, flow: int) -> Endpoint:
    dev_id = _device_id(device)
    state = c_uint32()
    _check(_call(device, _DEV_GET_STATE, (POINTER(c_uint32),), byref(state)), "IMMDevice::GetState")
    name, form = None, None
    store = c_void_p()
    if _call(device, _DEV_OPEN_PROPS, (c_uint32, POINTER(c_void_p)), STGM_READ, byref(store)) >= 0 and store.value:
        try:
            name = _read_prop(store.value, PKEY_DEVICE_FRIENDLY_NAME) or _read_prop(store.value, PKEY_DEVICE_DESC)
            form = _read_prop(store.value, PKEY_ENDPOINT_FORM_FACTOR)
        finally:
            _release(store.value)
    ep = Endpoint(flow=flow, dev_id=dev_id, name=name if isinstance(name, str) and name else "(без названия)",
                  state=state.value, form_factor=form if isinstance(form, int) else None)
    if flow == E_RENDER and ep.is_active:
        try:
            ep.mix_format = _mix_format(device)
        except Exception as exc:  # noqa: BLE001
            ep.mix_error = str(exc)
    return ep


def enumerate_endpoints() -> "list[Endpoint]":
    """Все устройства вывода и ввода (кроме давно удалённых). Бросает
    LoopbackCaptureError, если Core Audio недоступен."""
    if sys.platform != "win32":
        raise LoopbackCaptureError("перечисление устройств доступно только на Windows.")
    coinit = _ole32().CoInitializeEx(None, COINIT_MULTITHREADED)
    result: "list[Endpoint]" = []
    try:
        enum_ptr = c_void_p()
        _check(_ole32().CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                         byref(IID_IMMDeviceEnumerator), byref(enum_ptr)),
               "CoCreateInstance(MMDeviceEnumerator)")
        try:
            for flow in (E_RENDER, E_CAPTURE):
                defaults: "dict[str, list[int]]" = {}
                for role in (ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS):
                    dev_id = _default_endpoint_id(enum_ptr.value, flow, role)
                    if dev_id:
                        defaults.setdefault(dev_id, []).append(role)
                coll = c_void_p()
                _check(_call(enum_ptr.value, _ENUM_ENUM_ENDPOINTS, (c_int, c_uint32, POINTER(c_void_p)),
                             flow, SHOWN_STATES, byref(coll)), "EnumAudioEndpoints")
                try:
                    count = c_uint32()
                    _check(_call(coll.value, _COLL_GET_COUNT, (POINTER(c_uint32),), byref(count)), "GetCount")
                    for index in range(count.value):
                        device = c_void_p()
                        if _call(coll.value, _COLL_ITEM, (c_uint32, POINTER(c_void_p)), index, byref(device)) < 0:
                            continue
                        try:
                            ep = _read_endpoint(device.value, flow)
                        finally:
                            _release(device.value)
                        ep.default_roles = tuple(defaults.get(ep.dev_id, ()))
                        result.append(ep)
                finally:
                    _release(coll.value)
        finally:
            _release(enum_ptr.value)
    finally:
        if coinit >= 0:
            _ole32().CoUninitialize()
    return result


# --------------------------------------------------------------------------
# Проверка захвата
# --------------------------------------------------------------------------

@dataclass
class ProbeResult:
    ok: bool
    text: str                                  # формат (успех) или причина (сбой)
    tried: "list[str]" = field(default_factory=list)   # журнал попыток форматов


def probe_capture(kind: str) -> ProbeResult:
    """kind: 'process' (изоляция по процессу, на PID этой утилиты) или 'device'
    (весь звук устройства по умолчанию). Запускает и сразу останавливает захват."""
    cap = None
    try:
        if kind == "process":
            cap = wal.ProcessLoopbackCapture(os.getpid(), include_tree=True)
        else:
            cap = wal.DefaultDeviceLoopbackCapture()
        cap.start(ready_timeout=40.0)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(False, str(exc), list(getattr(cap, "attempt_log", []) or []))
    try:
        return ProbeResult(True, cap.description, list(cap.attempt_log))
    finally:
        try:
            cap.stop(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _audio_service_state() -> "str | None":
    """Состояние службы «Windows Audio» (RUNNING/STOPPED/...), None — не удалось узнать."""
    try:
        out = subprocess.run(["sc", "query", "Audiosrv"], capture_output=True, timeout=8).stdout
    except Exception:  # noqa: BLE001
        return None
    match = re.search(rb"\b(RUNNING|STOPPED|START_PENDING|STOP_PENDING|PAUSED|CONTINUE_PENDING|PAUSE_PENDING)\b", out)
    return match.group(1).decode("ascii") if match else None


def _windows_build() -> "tuple[int, int, int]":
    ver = getattr(sys, "getwindowsversion", None)
    if ver is None:
        return (0, 0, 0)
    v = ver()
    return (v.major, v.minor, v.build)


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------

def _endpoint_lines(index: int, ep: Endpoint) -> "list[str]":
    marks = ""
    if ep.default_roles:
        marks = "  [ПО УМОЛЧАНИЮ: " + ", ".join(ROLE_LABELS[r] for r in ep.default_roles) + "]"
    lines = [f"  {index}. {ep.name}{marks}"]
    parts = [f"состояние: {STATE_LABELS.get(ep.state, hex(ep.state))}"]
    if ep.form_factor is not None:
        parts.insert(0, f"тип: {FORM_FACTORS.get(ep.form_factor, ep.form_factor)}")
    lines.append("     " + "; ".join(parts))
    if ep.mix_format:
        lines.append(f"     формат микшера Windows: {ep.mix_format}")
    elif ep.mix_error:
        lines.append(f"     формат микшера узнать не удалось: {ep.mix_error}")
    if ep.dev_id:
        lines.append(f"     ID: {ep.dev_id}")
    return lines


def run_audio_diagnostics(out: "Callable[[str], None]" = print) -> int:
    """Печатает отчёт через out(). Код возврата: 0 — захват звука возможен
    (хотя бы в одном режиме), 2 — нет."""
    major, minor, build = _windows_build()
    win_name = "Windows 11" if build >= 22000 else ("Windows 10" if major == 10 else f"Windows {major}.{minor}")
    loopback_supported = build >= MIN_PROCESS_LOOPBACK_BUILD
    out("=== Диагностика звука Windows ===")
    out(f"Система: {win_name}, сборка {build}. Изоляция звука по процессу (Process Loopback): "
        + ("поддерживается." if loopback_supported
           else f"НЕ поддерживается (нужна сборка {MIN_PROCESS_LOOPBACK_BUILD}+ / Windows 10 версии 2004 или новее)."))

    # --- устройства ---
    endpoints: "list[Endpoint]" = []
    enum_error: "str | None" = None
    try:
        endpoints = enumerate_endpoints()
    except Exception as exc:  # noqa: BLE001
        enum_error = str(exc)

    out("")
    if enum_error is not None:
        out(f"Не удалось получить список устройств: {enum_error}")
    else:
        renders = [e for e in endpoints if e.flow == E_RENDER]
        captures = [e for e in endpoints if e.flow == E_CAPTURE]
        out("Устройства ВЫВОДА звука (динамики, наушники, HDMI) — именно их звук записывается:")
        if renders:
            for i, ep in enumerate(renders, 1):
                for line in _endpoint_lines(i, ep):
                    out(line)
        else:
            out("  (не найдено ни одного устройства вывода)")
        out("")
        out("Устройства ВВОДА (микрофоны) — для записи не используются:")
        if captures:
            for i, ep in enumerate(captures, 1):
                out(f"  {i}. {ep.name}  — {STATE_LABELS.get(ep.state, hex(ep.state))}"
                    + ("  [ПО УМОЛЧАНИЮ]" if ep.default_roles else ""))
        else:
            out("  (не найдено)")

    # --- проверка захвата ---
    out("")
    out("Проверка запуска захвата звука (запускается и сразу останавливается):")
    out("  ... изоляция по процессу (Process Loopback) ...")
    proc = probe_capture("process")
    if proc.ok:
        out(f"  [OK] Изоляция по процессу работает: {proc.text}")
        if len(proc.tried) > 1:
            out("       (первые форматы Windows отклонила, использован запасной; журнал попыток:)")
            for line in proc.tried:
                out(f"         {line}")
    else:
        out("  [СБОЙ] Изоляция по процессу не запускается:")
        for line in proc.text.splitlines():
            out(f"         {line}")
    out("  ... аварийный режим (весь звук устройства по умолчанию) ...")
    dev = probe_capture("device")
    if dev.ok:
        out(f"  [OK] Аварийный режим работает: {dev.text}")
    else:
        out("  [СБОЙ] Аварийный режим не запускается:")
        for line in dev.text.splitlines():
            out(f"         {line}")

    # --- итог ---
    out("")
    active_renders = [e for e in endpoints if e.flow == E_RENDER and e.is_active]
    if proc.ok:
        out("ИТОГ: всё готово — запись звука только из Firefox должна работать.")
        out("Что эта проверка НЕ показывает: идёт ли реально звук. Проверить на живом треке:")
        out("  python win_audio_loopback.py <PID_firefox> 5")
    elif dev.ok:
        out("ИТОГ: изоляция звука по процессу недоступна, но аварийный режим работает.")
        out("Включите галочку «Аварийный режим» — будет записываться ВЕСЬ звук системы.")
        if not loopback_supported:
            out(f"Причина, скорее всего, в версии Windows (сборка {build}, нужна {MIN_PROCESS_LOOPBACK_BUILD}+).")
    else:
        out("ИТОГ: захват звука не запускается ни в одном режиме.")
        if enum_error is None and not active_renders:
            out("  • В Windows нет ни одного АКТИВНОГО устройства вывода: подключите наушники/колонки "
                "или включите устройство (Параметры -> Система -> Звук, либо mmsys.cpl).")
        state = _audio_service_state()
        if state is not None and state != "RUNNING":
            out(f"  • Служба «Windows Audio» (Audiosrv) сейчас: {state}. Запустите её (services.msc) и повторите проверку.")
        elif state == "RUNNING":
            out("  • Служба «Windows Audio» запущена — смотрите коды ошибок выше.")
        out("  • Если устройство есть и служба работает — пришлите этот отчёт целиком.")
    return 0 if (proc.ok or dev.ok) else 2


if __name__ == "__main__":
    raise SystemExit(run_audio_diagnostics())
