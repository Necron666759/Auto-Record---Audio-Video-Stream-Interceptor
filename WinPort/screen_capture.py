#!/usr/bin/env python3
"""
screen_capture.py (Windows редакция)

Захват экрана для видеозаписи (используется auto_record_youtube.py). Тот же
интерфейс, что у Linux-версии (start_video_capture(...) -> Popen), но вместо
X11/Wayland-бэкендов (ffmpeg x11grab / wf-recorder) на Windows есть ровно один
штатный вариант — ffmpeg -f gdigrab. Поэтому выбор бэкенда (--display-server,
переключатель «Захват экрана: Авто/X11/Wayland» в GUI) в Windows-версии не
нужен и отсутствует.

Как и в Linux-версии, захватывается ПРЯМОУГОЛЬНИК экрана с областью плеера
(его геометрию считает auto_record_youtube.get_video_screen_geometry), и
картинка сразу кодируется в целевой видеокодек (format_options.video_codec_args),
без промежуточного лишнего перекодирования.

Два Windows-специфичных момента:

1. DPI-масштабирование. При масштабе экрана 125%/150% координаты, которые видит
   не-DPI-aware процесс, «виртуализированы» — gdigrab взял бы не ту область.
   Поэтому ffmpeg запускается с __COMPAT_LAYER=HIGHDPIAWARE (стандартный способ
   сделать чужой exe DPI-aware без манифеста), а сам воркер вызывает
   ensure_dpi_aware() — тогда и GetSystemMetrics, и геометрия страницы
   (mozInnerScreenX * devicePixelRatio) измеряются в одних и тех же физических
   пикселях.

2. Остановка. В Linux процесс захвата останавливается SIGTERM (ffmpeg корректно
   дописывает файл). На Windows terminate() — это TerminateProcess: файл остался
   бы недописанным. Поэтому процесс запускается со stdin=PIPE, а остановка —
   стандартная команда ffmpeg 'q' (см. stop_video_capture).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from format_options import video_codec_args

# GetSystemMetrics: границы ВИРТУАЛЬНОГО экрана (все мониторы вместе)
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79


def ensure_dpi_aware() -> None:
    """Делает ТЕКУЩИЙ процесс (воркер; окон у него нет) DPI-aware, чтобы
    GetSystemMetrics возвращал физические пиксели. Безопасно вызывать повторно."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def virtual_screen_rect() -> "tuple[int, int, int, int] | None":
    """(x, y, width, height) виртуального экрана в физических пикселях или None."""
    if sys.platform != "win32":
        return None
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics  # type: ignore[attr-defined]
        x, y = gsm(_SM_XVIRTUALSCREEN), gsm(_SM_YVIRTUALSCREEN)
        w, h = gsm(_SM_CXVIRTUALSCREEN), gsm(_SM_CYVIRTUALSCREEN)
        return (x, y, w, h) if w > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


def clamp_geometry(geom: dict, screen: "tuple[int, int, int, int] | None") -> dict:
    """Вписывает область захвата в границы экрана (gdigrab отказывается
    работать с прямоугольником, выходящим за экран) и делает размеры чётными
    (требование большинства видеокодеков). Возвращает НОВЫЙ словарь; если
    область пришлось изменить, ставит в нём ключ 'clamped' = True."""
    result = dict(geom)
    x, y = int(geom["capture_x"]), int(geom["capture_y"])
    w, h = int(geom["capture_width"]), int(geom["capture_height"])
    if screen is not None:
        sx, sy, sw, sh = screen
        x2, y2 = min(x + w, sx + sw), min(y + h, sy + sh)
        x, y = max(x, sx), max(y, sy)
        w, h = x2 - x, y2 - y
    w -= w % 2
    h -= h % 2
    if w <= 0 or h <= 0:
        raise RuntimeError(
            f"Область видео ({geom['capture_width']}x{geom['capture_height']} @ "
            f"{geom['capture_x']},{geom['capture_y']}) лежит вне экрана — нечего захватывать. "
            "Разверните окно Firefox на экран и повторите."
        )
    if (x, y, w, h) != (int(geom["capture_x"]), int(geom["capture_y"]),
                        int(geom["capture_width"]), int(geom["capture_height"])):
        result["clamped"] = True
    result.update(capture_x=x, capture_y=y, capture_width=w, capture_height=h)
    return result


def build_gdigrab_command(ffmpeg_bin: str, container_id: str, geom: dict, fps: int, out_path: Path) -> "list[str]":
    """Команда ffmpeg для захвата прямоугольника экрана в целевой видеокодек."""
    return [
        ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "gdigrab", "-framerate", str(fps),
        "-offset_x", str(geom["capture_x"]), "-offset_y", str(geom["capture_y"]),
        "-video_size", f"{geom['capture_width']}x{geom['capture_height']}",
        "-i", "desktop",
        *video_codec_args(container_id),
        str(out_path),
    ]


def start_video_capture(
    ffmpeg_bin: str,
    container_id: str,
    geom: dict,
    fps: int,
    out_path: Path,
) -> subprocess.Popen:
    """Запускает захват заданной прямоугольной области экрана в отдельном
    процессе, пишущем видео (без звука) в out_path. Возвращает Popen —
    вызывающий код останавливает его через stop_video_capture()."""
    geom = clamp_geometry(geom, virtual_screen_rect())
    cmd = build_gdigrab_command(ffmpeg_bin, container_id, geom, fps, out_path)
    env = {**os.environ, "__COMPAT_LAYER": "HIGHDPIAWARE"}
    print(f"Захват видео запущен (gdigrab / Windows, {fps}fps, "
          f"{geom['capture_width']}x{geom['capture_height']} @ {geom['capture_x']},{geom['capture_y']})...")
    # stdin=PIPE — для команды 'q' (см. stop_video_capture). stdout/stderr наследуются:
    # предупреждения ffmpeg попадают в лог GUI.
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)


def request_video_stop(proc: "subprocess.Popen | None") -> None:
    """Просит ffmpeg завершить захват: 'q' в stdin (не ждёт завершения). Так остановку
    видео и звука можно начать одновременно, как это делает Linux-версия."""
    if proc is not None and proc.poll() is None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
        except (OSError, ValueError):
            pass


def wait_video_capture(proc: "subprocess.Popen | None", timeout: float = 15.0) -> None:
    """Ждёт завершения процесса захвата; если не уложился — принудительно убивает."""
    if proc is None:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def stop_video_capture(proc: "subprocess.Popen | None", timeout: float = 15.0) -> None:
    """Корректно останавливает процесс захвата: 'q' в stdin -> ffmpeg дописывает
    контейнер и выходит. Если не ответил за timeout — принудительно убивает."""
    request_video_stop(proc)
    wait_video_capture(proc, timeout)
