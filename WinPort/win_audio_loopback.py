#!/usr/bin/env python3
"""
win_audio_loopback.py  (чистый ctypes, БЕЗ comtypes)

Замена изоляции звука через PulseAudio (см. FirefoxAudioIsolator в Linux-версии)
на WASAPI "Process Loopback Capture" (Windows 10 2004+ / Windows 11): захват
звука ТОЛЬКО заданного процесса и его дерева потомков.

Что исправлено по сравнению с прежней (comtypes) реализацией
------------------------------------------------------------
1. Ошибка [WinError -2147483634] = 0x8000000E (E_ILLEGAL_METHOD_CALL) при
   ActivateAudioInterfaceAsync. Причина: обработчик завершения активации
   ОБЯЗАН быть "agile" (отвечать на QueryInterface(IAgileObject)), иначе
   система пытается маршалить его между апартаментами и активация
   отвергается. comtypes.COMObject такого ответа не давал. Теперь обработчик
   — собственный COM-объект на ctypes-vtable, отвечающий на IUnknown,
   IActivateAudioInterfaceCompletionHandler и IAgileObject.
2. Все вызовы COM идут через явную vtable с возвратом HRESULT как обычного
   числа: любая ошибка превращается в LoopbackCaptureError с именем этапа и
   кодом 0x........ (а не в "голый" OSError, который вызывающий код путал
   с потерей интернета).
3. Формат захвата — 96 кГц / 2 канала (как на Linux). Движок Windows сам
   конвертирует поток (AUTOCONVERTPCM) из своего микс-формата. Захват идёт в
   float32 (родной формат движка, без потерь); в 24-битный WAV его превращает
   ffmpeg (см. encode_helpers.ffmpeg_raw_wav_args_dynamic). Если система не
   принимает 96 кГц, пробуются запасные форматы ("лестница"), каждый — на
   свежей активации, и реально выбранный формат сообщается вызывающему коду.
4. Событийный режим (EVENTCALLBACK), как в официальном сэмпле Microsoft
   ApplicationLoopback, с запасным опросным режимом.
5. Заполнение тишиной: в режиме loopback Windows не присылает пакеты, пока
   нет звука. Чтобы тайминг файла совпадал с реальным (как у parec в Linux),
   простои дополняются нулями по настенным часам.
6. Внутренний буфер вместо os.pipe() (у pipe в Windows буфер всего 4 КБ).

Модуль импортируется и на не-Windows (для тестов логики), но захват
работает только на Windows.
"""

from __future__ import annotations

import collections
import ctypes
import sys
import threading
import time
import uuid
from ctypes import POINTER, byref, c_int, c_long, c_longlong, c_uint16, c_uint32, c_ubyte, c_void_p, c_wchar_p, sizeof
from dataclasses import dataclass
from typing import Callable

_IS_WIN = sys.platform == "win32"
_FUNCTYPE = ctypes.WINFUNCTYPE if _IS_WIN else ctypes.CFUNCTYPE  # type: ignore[attr-defined]


class LoopbackCaptureError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# GUID и константы
# --------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]


def make_guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUnknown = make_guid("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = make_guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IAudioClient = make_guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = make_guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_ICompletionHandler = make_guid("{41D949AB-9862-444A-80F6-C261334DA5EB}")
CLSID_MMDeviceEnumerator = make_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = make_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
KSDATAFORMAT_SUBTYPE_PCM = make_guid("{00000001-0000-0010-8000-00AA00389B71}")

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
VT_BLOB = 65
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0


def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


E_NOINTERFACE = _s32(0x80004002)

_HR_NAMES = {
    0x8000000E: "E_ILLEGAL_METHOD_CALL", 0x80004002: "E_NOINTERFACE", 0x80070057: "E_INVALIDARG",
    0x80004001: "E_NOTIMPL", 0x80010106: "RPC_E_CHANGED_MODE", 0x8001010E: "RPC_E_WRONG_THREAD",
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED", 0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890003: "AUDCLNT_E_WRONG_ENDPOINT_TYPE", 0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT", 0x8889000A: "AUDCLNT_E_DEVICE_IN_USE",
    0x88890013: "AUDCLNT_E_INVALID_STREAM_FLAG?", 0x8889000F: "AUDCLNT_E_EVENTHANDLE_NOT_SET",
    0x88890021: "AUDCLNT_E_INVALID_DEVICE_PERIOD?", 0x88890027: "AUDCLNT_E_EXCLUSIVE_MODE_ONLY?",
}


def hr_text(hr: int) -> str:
    code = hr & 0xFFFFFFFF
    name = _HR_NAMES.get(code)
    return f"0x{code:08X}" + (f" ({name})" if name else "")


def _check(hr: int, stage: str) -> int:
    if hr < 0:
        raise LoopbackCaptureError(f"{stage}: ошибка {hr_text(hr)}")
    return hr


# --------------------------------------------------------------------------
# Структуры
# --------------------------------------------------------------------------

class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_uint16), ("nChannels", c_uint16), ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32), ("nBlockAlign", c_uint16), ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX), ("wValidBitsPerSample", c_uint16),
        ("dwChannelMask", c_uint32), ("SubFormat", GUID),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("TargetProcessId", c_uint32), ("ProcessLoopbackMode", c_int)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", c_uint16), ("wReserved1", c_uint16), ("wReserved2", c_uint16), ("wReserved3", c_uint16),
        ("cbSize", c_uint32), ("pBlobData", c_void_p),
    ]


# --------------------------------------------------------------------------
# Формат захвата и "лестница" вариантов
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureFormat:
    rate: int
    channels: int
    bits: int          # 16 / 24 / 32
    is_float: bool

    @property
    def sample_fmt(self) -> str:      # в терминах ffmpeg -f
        return "f32le" if self.is_float else f"s{self.bits}le"

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * (self.bits // 8)

    def label(self) -> str:
        kind = "float32" if self.is_float else f"{self.bits}bit PCM"
        return f"{self.rate} Гц / {kind} / {self.channels}ch"

    def to_wfx(self):
        """WAVEFORMATEX (float32/16бит) либо WAVEFORMATEXTENSIBLE (24 бита)."""
        block = self.bytes_per_frame
        if self.bits == 24:
            wfe = WAVEFORMATEXTENSIBLE()
            wfe.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE
            wfe.Format.nChannels = self.channels
            wfe.Format.nSamplesPerSec = self.rate
            wfe.Format.nAvgBytesPerSec = self.rate * block
            wfe.Format.nBlockAlign = block
            wfe.Format.wBitsPerSample = 24
            wfe.Format.cbSize = 22
            wfe.wValidBitsPerSample = 24
            wfe.dwChannelMask = 0x3  # FL | FR
            wfe.SubFormat = KSDATAFORMAT_SUBTYPE_PCM
            return wfe
        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT if self.is_float else WAVE_FORMAT_PCM
        wfx.nChannels = self.channels
        wfx.nSamplesPerSec = self.rate
        wfx.nAvgBytesPerSec = self.rate * block
        wfx.nBlockAlign = block
        wfx.wBitsPerSample = self.bits
        wfx.cbSize = 0
        return wfx


@dataclass(frozen=True)
class Attempt:
    fmt: CaptureFormat
    event_driven: bool
    autoconvert: bool
    buffer_hns: int

    def flags(self) -> int:
        f = AUDCLNT_STREAMFLAGS_LOOPBACK
        if self.event_driven:
            f |= AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        if self.autoconvert:
            f |= AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        return f

    def label(self) -> str:
        return (f"{self.fmt.label()}, {'события' if self.event_driven else 'опрос'}, "
                f"{'autoconvert' if self.autoconvert else 'без autoconvert'}, буфер {self.buffer_hns // 10000} мс")


def build_attempts(channels: int = 2, target_rate: int = 96000) -> list[Attempt]:
    hi_f32 = CaptureFormat(target_rate, channels, 32, True)
    hi_s24 = CaptureFormat(target_rate, channels, 24, False)
    mid_f32 = CaptureFormat(48000, channels, 32, True)
    lo_s16 = CaptureFormat(44100, channels, 16, False)   # формат из сэмпла Microsoft
    buf = 200 * 10_000
    return [
        Attempt(hi_f32, True, True, buf),
        Attempt(hi_f32, True, True, 0),
        Attempt(hi_f32, True, False, buf),
        Attempt(hi_f32, False, True, buf),
        Attempt(hi_s24, True, True, buf),
        Attempt(mid_f32, True, True, buf),
        Attempt(mid_f32, False, True, buf),
        Attempt(lo_s16, True, False, buf),
        Attempt(lo_s16, False, False, buf),
    ]


# --------------------------------------------------------------------------
# Внутренний буфер (замена os.pipe)
# --------------------------------------------------------------------------

class ByteStream:
    """Потокобезопасный буфер с файловым интерфейсом read(n): возвращает
    до n байт, блокируется пока данных нет, b'' — после close() и опустошения."""

    def __init__(self) -> None:
        self._chunks: collections.deque[bytes] = collections.deque()
        self._cv = threading.Condition()
        self._closed = False

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._cv:
            if self._closed:
                return
            self._chunks.append(data)
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def read(self, n: int = 4096) -> bytes:
        with self._cv:
            while not self._chunks and not self._closed:
                self._cv.wait(0.5)
            if not self._chunks:
                return b""
            chunk = self._chunks.popleft()
            if len(chunk) > n:
                self._chunks.appendleft(chunk[n:])
                chunk = chunk[:n]
            return chunk


# --------------------------------------------------------------------------
# Универсальный цикл выкачивания пакетов (тестируется без Windows)
# --------------------------------------------------------------------------

def pump_packets(
    next_packet: Callable[[], "bytes | None"],
    wait: Callable[[float], None],
    sink: Callable[[bytes], None],
    stop_event: threading.Event,
    rate: int,
    bytes_per_frame: int,
    on_packet: "Callable[[bytes], None] | None" = None,
    clock: Callable[[], float] = time.monotonic,
    gap_threshold: float = 0.25,
    gap_margin: float = 0.10,
) -> None:
    """Читает пакеты, пока не сработает stop_event. Если пакетов нет дольше
    gap_threshold с (WASAPI loopback молчит при отсутствии звука), дописывает
    нули, чтобы длительность записи совпадала с настенным временем."""
    t0 = clock()
    frames = 0
    while not stop_event.is_set():
        got = False
        while not stop_event.is_set():
            pkt = next_packet()
            if pkt is None:
                break
            got = True
            frames += len(pkt) // bytes_per_frame
            if on_packet is not None:
                on_packet(pkt)
            sink(pkt)
        if not got:
            deficit = (clock() - t0) * rate - frames
            if deficit > gap_threshold * rate:
                n = int(deficit - gap_margin * rate)
                while n > 0:
                    step = min(n, rate)  # не больше 1 с за раз
                    sink(bytes(step * bytes_per_frame))
                    frames += step
                    n -= step
        wait(0.1)


# --------------------------------------------------------------------------
# Низкоуровневый COM через vtable
# --------------------------------------------------------------------------

def _com_fn(iface: int, index: int, *argtypes):
    if not iface:
        raise LoopbackCaptureError("COM: нулевой указатель на интерфейс")
    vtbl = ctypes.cast(iface, POINTER(c_void_p))[0]
    fn_addr = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return _FUNCTYPE(c_long, c_void_p, *argtypes)(fn_addr)


def _call(iface: int, index: int, argtypes: tuple, *args) -> int:
    return _com_fn(iface, index, *argtypes)(iface, *args)


def _release(iface: "int | None") -> None:
    if iface:
        try:
            _com_fn(iface, 2)(iface)
        except Exception:
            pass


# индексы методов vtable
_AC_INITIALIZE, _AC_START, _AC_STOP, _AC_SET_EVENT, _AC_GET_SERVICE = 3, 10, 11, 13, 14
_CC_GET_BUFFER, _CC_RELEASE_BUFFER, _CC_GET_NEXT_PACKET_SIZE = 3, 4, 5

if _IS_WIN:
    _ole32 = ctypes.WinDLL("ole32")
    _kernel32 = ctypes.WinDLL("kernel32")
    _mmdevapi = ctypes.WinDLL("Mmdevapi")
    _ole32.CoInitializeEx.argtypes = [c_void_p, c_uint32]
    _ole32.CoInitializeEx.restype = c_long
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None
    _ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_uint32, POINTER(GUID), POINTER(c_void_p)]
    _ole32.CoCreateInstance.restype = c_long
    _kernel32.CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
    _kernel32.CreateEventW.restype = c_void_p
    _kernel32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
    _kernel32.WaitForSingleObject.restype = c_uint32
    _kernel32.CloseHandle.argtypes = [c_void_p]
    _kernel32.CloseHandle.restype = c_int
    _ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
    _ActivateAudioInterfaceAsync.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
    _ActivateAudioInterfaceAsync.restype = c_long   # НЕ HRESULT: иначе ctypes бросит голый OSError
else:
    _ActivateAudioInterfaceAsync = None


class CompletionHandler:
    """COM-объект IActivateAudioInterfaceCompletionHandler + IAgileObject."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.hr_call: "int | None" = None
        self.hr_activate: "int | None" = None
        self.iface: "int | None" = None
        self.error: "BaseException | None" = None
        self._refs = 1
        self._lock = threading.Lock()
        self._accepted = {bytes(g) for g in (IID_IUnknown, IID_ICompletionHandler, IID_IAgileObject)}

        qi_t = _FUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
        ref_t = _FUNCTYPE(c_uint32, c_void_p)
        done_t = _FUNCTYPE(c_long, c_void_p, c_void_p)
        self._cb_qi = qi_t(self._query_interface)
        self._cb_addref = ref_t(self._add_ref)
        self._cb_release = ref_t(self._release_ref)
        self._cb_done = done_t(self._activate_completed)
        self._vtbl = (c_void_p * 4)(
            *[ctypes.cast(cb, c_void_p).value for cb in (self._cb_qi, self._cb_addref, self._cb_release, self._cb_done)]
        )
        self._obj = c_void_p(ctypes.addressof(self._vtbl))
        self.address = ctypes.addressof(self._obj)

    def _query_interface(self, this, riid, ppv):
        try:
            if ppv:
                if riid and bytes(riid.contents) in self._accepted:
                    ppv[0] = self.address
                    self._add_ref(this)
                    return 0
                ppv[0] = None
        except Exception:
            pass
        return E_NOINTERFACE

    def _add_ref(self, this):
        with self._lock:
            self._refs += 1
            return self._refs

    def _release_ref(self, this):
        with self._lock:
            self._refs = max(0, self._refs - 1)
            return self._refs

    def _activate_completed(self, this, operation):
        try:
            hr_act, punk = c_long(0), c_void_p()
            self.hr_call = _call(operation, 3, (POINTER(c_long), POINTER(c_void_p)), byref(hr_act), byref(punk))
            self.hr_activate = hr_act.value
            self.iface = punk.value
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.event.set()
        return 0


def activate_process_loopback(pid: int, include_tree: bool, timeout: float = 8.0) -> int:
    """Возвращает указатель IAudioClient для Process Loopback."""
    if _ActivateAudioInterfaceAsync is None:
        raise LoopbackCaptureError("ActivateAudioInterfaceAsync недоступен (не Windows).")
    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.TargetProcessId = pid
    params.ProcessLoopbackMode = (PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE if include_tree
                                  else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.cbSize = sizeof(params)
    pv.pBlobData = ctypes.addressof(params)

    handler = CompletionHandler()
    operation = c_void_p()
    hr = _ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IID_IAudioClient),
                                      byref(pv), c_void_p(handler.address), byref(operation))
    _check(hr, "ActivateAudioInterfaceAsync (нужна Windows 10 2004+ / Windows 11)")
    try:
        if not handler.event.wait(timeout):
            raise LoopbackCaptureError(f"ActivateAudioInterfaceAsync: нет ответа за {timeout:.0f}с")
        if handler.error is not None:
            raise LoopbackCaptureError(f"обработчик активации: {handler.error!r}")
        _check(handler.hr_call if handler.hr_call is not None else 0, "IActivateAudioInterfaceAsyncOperation::GetActivateResult")
        _check(handler.hr_activate if handler.hr_activate is not None else 0, "результат активации Process Loopback")
        if not handler.iface:
            raise LoopbackCaptureError("активация вернула пустой IAudioClient")
        return handler.iface
    finally:
        _release(operation.value)
        _ = params  # держим живым до конца вызова


def activate_default_render_loopback() -> int:
    """IAudioClient default render-устройства (для loopback ВСЕГО звука)."""
    enum_ptr = c_void_p()
    _check(_ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                   byref(IID_IMMDeviceEnumerator), byref(enum_ptr)), "CoCreateInstance(MMDeviceEnumerator)")
    device = c_void_p()
    client = c_void_p()
    try:
        _check(_call(enum_ptr.value, 4, (c_int, c_int, POINTER(c_void_p)), 0, 0, byref(device)),
               "GetDefaultAudioEndpoint")
        _check(_call(device.value, 3, (POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p)),
                     byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client)), "IMMDevice::Activate(IAudioClient)")
    finally:
        _release(device.value)
        _release(enum_ptr.value)
    return client.value


# --------------------------------------------------------------------------
# Захват
# --------------------------------------------------------------------------

class _BaseCapture:
    """Интерфейс, совместимый с subprocess.Popen для вызывающего кода:
    .stdout (read), poll(), terminate(), wait(), kill(), плюс start()/stop()."""

    def __init__(self, channels: int = 2, target_rate: int = 96000) -> None:
        self.channels = channels
        self.target_rate = target_rate
        self.sample_rate = target_rate
        self.sample_fmt = "f32le"
        self.bytes_per_frame = channels * 4
        self.description = ""
        self.attempt_log: list[str] = []
        self.audible_frames = 0     # кадры с реальным (не нулевым) звуком
        self.total_frames = 0
        self.error: "BaseException | None" = None
        self._stream = ByteStream()
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._start_error: "BaseException | None" = None

    @property
    def stdout(self) -> ByteStream:
        return self._stream

    def _activate(self) -> int:
        raise NotImplementedError

    # --- управление ---
    def start(self, ready_timeout: float = 60.0) -> None:
        if not _IS_WIN:
            raise LoopbackCaptureError("WASAPI-захват доступен только на Windows.")
        self._thread = threading.Thread(target=self._thread_main, name="wasapi-capture", daemon=True)
        self._thread.start()
        if not self._started.wait(ready_timeout):
            self._stop.set()
            raise LoopbackCaptureError(f"захват звука WASAPI не запустился за {ready_timeout:.0f}с")
        if self._start_error is not None:
            err, self._start_error = self._start_error, None
            self._thread.join(timeout=3)
            raise err  # type: ignore[misc]

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._stream.close()

    def poll(self):
        return None if (self._thread is not None and self._thread.is_alive()) else 0

    def terminate(self) -> None:
        self._stop.set()

    kill = terminate

    def wait(self, timeout: "float | None" = None) -> int:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return 0

    # --- рабочий поток ---
    def _open_stream(self):
        """Лестница вариантов: каждый на свежей активации. Возвращает
        (client, capture_client, attempt, event_handle|None)."""
        for attempt in build_attempts(self.channels, self.target_rate):
            client = 0
            event = None
            try:
                client = self._activate()
                wfx = attempt.fmt.to_wfx()
                _check(_call(client, _AC_INITIALIZE,
                             (c_int, c_uint32, c_longlong, c_longlong, c_void_p, c_void_p),
                             AUDCLNT_SHAREMODE_SHARED, attempt.flags(), attempt.buffer_hns, 0,
                             ctypes.addressof(wfx), None), "IAudioClient::Initialize")
                if attempt.event_driven:
                    event = _kernel32.CreateEventW(None, 0, 0, None)
                    if not event:
                        raise LoopbackCaptureError("CreateEventW не удалось")
                    _check(_call(client, _AC_SET_EVENT, (c_void_p,), event), "IAudioClient::SetEventHandle")
                cc = c_void_p()
                _check(_call(client, _AC_GET_SERVICE, (POINTER(GUID), POINTER(c_void_p)),
                             byref(IID_IAudioCaptureClient), byref(cc)), "IAudioClient::GetService(IAudioCaptureClient)")
                try:
                    _check(_call(client, _AC_START, ()), "IAudioClient::Start")
                except Exception:
                    _release(cc.value)
                    raise
                self.attempt_log.append(f"OK: {attempt.label()}")
                return client, cc.value, attempt, event
            except Exception as exc:  # noqa: BLE001
                self.attempt_log.append(f"ошибка [{attempt.label()}]: {exc}")
                if event:
                    _kernel32.CloseHandle(event)
                _release(client)
        raise LoopbackCaptureError(
            "не удалось инициализировать захват ни в одном формате:\n  " + "\n  ".join(self.attempt_log)
        )

    def _thread_main(self) -> None:
        coinit = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        client = cc = 0
        event = None
        try:
            try:
                client, cc, attempt, event = self._open_stream()
            except BaseException as exc:  # noqa: BLE001
                self._start_error = exc
                return
            fmt = attempt.fmt
            self.sample_rate, self.channels = fmt.rate, fmt.channels
            self.sample_fmt, self.bytes_per_frame = fmt.sample_fmt, fmt.bytes_per_frame
            self.description = attempt.label()
            self._started.set()
            try:
                self._pump(cc, attempt, event)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
            finally:
                try:
                    _call(client, _AC_STOP, ())
                except Exception:
                    pass
        finally:
            self._started.set()
            self._stream.close()
            if event:
                _kernel32.CloseHandle(event)
            _release(cc)
            _release(client)
            if coinit >= 0:
                _ole32.CoUninitialize()

    def _pump(self, cc: int, attempt: Attempt, event) -> None:
        bpf = attempt.fmt.bytes_per_frame
        n_t = c_uint32()

        def next_packet():
            _check(_call(cc, _CC_GET_NEXT_PACKET_SIZE, (POINTER(c_uint32),), byref(n_t)), "GetNextPacketSize")
            if n_t.value == 0:
                return None
            data, frames, flags = c_void_p(), c_uint32(), c_uint32()
            hr = _check(_call(cc, _CC_GET_BUFFER,
                              (POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), c_void_p, c_void_p),
                              byref(data), byref(frames), byref(flags), None, None), "GetBuffer")
            nframes = frames.value
            if hr != 0 or nframes == 0 or not data.value:   # AUDCLNT_S_BUFFER_EMPTY
                if nframes:
                    _call(cc, _CC_RELEASE_BUFFER, (c_uint32,), nframes)
                return None
            n_bytes = nframes * bpf
            silent = bool(flags.value & AUDCLNT_BUFFERFLAGS_SILENT)
            chunk = bytes(n_bytes) if silent else ctypes.string_at(data.value, n_bytes)
            _call(cc, _CC_RELEASE_BUFFER, (c_uint32,), nframes)
            return chunk

        def on_packet(chunk: bytes) -> None:
            frames = len(chunk) // bpf
            self.total_frames += frames
            if chunk.count(0) != len(chunk):
                self.audible_frames += frames

        if event:
            wait = lambda t: _kernel32.WaitForSingleObject(event, int(t * 1000))  # noqa: E731
        else:
            wait = lambda t: time.sleep(min(t, 0.005))  # noqa: E731

        pump_packets(next_packet, wait, self._stream.write, self._stop,
                     attempt.fmt.rate, bpf, on_packet=on_packet)


class ProcessLoopbackCapture(_BaseCapture):
    """Звук ТОЛЬКО заданного процесса (+ его дерева потомков)."""

    def __init__(self, target_pid: int, include_tree: bool = True, channels: int = 2, target_rate: int = 96000) -> None:
        super().__init__(channels, target_rate)
        self.target_pid = target_pid
        self.include_tree = include_tree

    def _activate(self) -> int:
        return activate_process_loopback(self.target_pid, self.include_tree)


class DefaultDeviceLoopbackCapture(_BaseCapture):
    """Аварийный вариант: ВЕСЬ звук default render-устройства."""

    def _activate(self) -> int:
        return activate_default_render_loopback()


# --------------------------------------------------------------------------
# Самодиагностика:  python win_audio_loopback.py [PID|device] [секунды]
# --------------------------------------------------------------------------

def _selftest(argv: list[str]) -> int:
    if not _IS_WIN:
        print("Самодиагностика работает только на Windows.")
        return 1
    target = argv[0] if argv else "device"
    seconds = float(argv[1]) if len(argv) > 1 else 5.0
    build = getattr(sys.getwindowsversion(), "build", 0)
    print(f"Windows build {build} (Process Loopback нужен >= 19041)")
    cap: _BaseCapture = (DefaultDeviceLoopbackCapture() if target == "device"
                         else ProcessLoopbackCapture(int(target), include_tree=True))
    try:
        cap.start()
    except LoopbackCaptureError as exc:
        print("ЗАХВАТ НЕ ЗАПУСТИЛСЯ:\n ", exc)
        return 2
    print("Журнал попыток:\n  " + "\n  ".join(cap.attempt_log))
    print(f"Формат: {cap.description}. Запись {seconds:.0f} с — включите звук в целевом приложении...")
    total = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        total += len(cap.stdout.read(65536))
    cap.stop()
    print(f"Получено кадров: {cap.total_frames}, со звуком: {cap.audible_frames}, "
          f"байт: {total}, ошибка: {cap.error}")
    return 0 if cap.audible_frames else 3


if __name__ == "__main__":
    raise SystemExit(_selftest(sys.argv[1:]))
