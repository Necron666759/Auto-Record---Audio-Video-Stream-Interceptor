#!/usr/bin/env python3
"""
screen_capture.py

Абстракция захвата экрана для видеозаписи (используется
auto_record_youtube.py). Раньше видео писалось напрямую через
ffmpeg x11grab и работало только под X11. Этот модуль добавляет второй
бэкенд — Wayland — и сам решает, какой из них использовать, чтобы
остальному коду не нужно было знать про конкретный сервер отображения.

Бэкенды:
  - x11:     ffmpeg -f x11grab, как и раньше. Требует DISPLAY (или
             XWayland-сессию, которая тоже выставляет DISPLAY).
  - wayland: 'wf-recorder' — использует протокол компоузера
             wlr-screencopy (и его преемник ext-image-copy-capture),
             который поддерживают wlroots-совместимые композиторы
             (Sway, Hyprland, River, Wayfire, Labwc и т.п.) БЕЗ
             диалога системного портала. Захватывает произвольный
             прямоугольник экрана — то, что нужно нам для записи
             конкретно области видео-плеера, как и в x11-версии.

Ограничение (честно, без замалчивания): на GNOME/KDE под Wayland (не
wlroots) wlr-screencopy не поддерживается — там захват экрана идёт
только через xdg-desktop-portal ScreenCast (диалог согласия + PipeWire),
который не даёт напрямую задать произвольный прямоугольник области без
дополнительной ручной обвязки поверх portal API. Если это ваш случай —
используйте XWayland (--display-server x11 обычно работает и в
Wayland-сессии GNOME/KDE, т.к. Firefox по умолчанию может рисоваться
через XWayland) либо запишите только звук (audio-контейнер).

Выбор бэкенда:
    detect_session_type() смотрит (по приоритету):
      1) явный override (флаг --display-server программы);
      2) XDG_SESSION_TYPE;
      3) наличие WAYLAND_DISPLAY;
      4) наличие DISPLAY;
      5) x11 как разумный дефолт.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from format_options import video_codec_args

# ffmpeg-флаг -> "ключ" приватной опции кодека для wf-recorder (-p ключ=значение)
_WFRECORDER_OPT_MAP = {
    "-preset": "preset",
    "-crf": "crf",
    "-pix_fmt": "pix_fmt",
    "-b:v": "b",
    "-profile:v": "profile",
}


def detect_session_type(override: str | None = None) -> str:
    """Возвращает 'x11' или 'wayland'. override='auto'/None -> автоопределение."""
    if override and override != "auto":
        return override

    env_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if env_type in ("x11", "wayland"):
        return env_type
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "x11"  # разумный дефолт для окружений без явных переменных сессии


def wayland_recorder_available() -> bool:
    return shutil.which("wf-recorder") is not None


def _wfrecorder_codec_flags(container_id: str) -> list[str]:
    """Переводит наши ffmpeg-аргументы видеокодека (format_options.video_codec_args)
    в эквивалентные флаги wf-recorder (-c <кодек> плюс -p ключ=значение для
    остальных приватных опций кодека — wf-recorder прокидывает их напрямую в
    libavcodec, так же как это делает сам ffmpeg)."""
    args = video_codec_args(container_id)
    flags: list[str] = []
    i = 0
    while i < len(args) - 1:
        key, value = args[i], args[i + 1]
        if key == "-c:v":
            flags += ["-c", value]
        else:
            opt_name = _WFRECORDER_OPT_MAP.get(key, key.lstrip("-"))
            flags += ["-p", f"{opt_name}={value}"]
        i += 2
    return flags


def start_video_capture(
    ffmpeg_bin: str,
    container_id: str,
    geom: dict,
    fps: int,
    out_path: Path,
    display_server: str = "auto",
) -> subprocess.Popen:
    """Запускает захват заданной прямоугольной области экрана в отдельном
    процессе, пишущем видео (без звука) в out_path. Возвращает Popen —
    вызывающий код останавливает его так же, как раньше останавливал
    x11grab-процесс (terminate()/wait()/kill()).
    """
    session_type = detect_session_type(display_server)

    if session_type == "wayland":
        if not wayland_recorder_available():
            raise RuntimeError(
                "Обнаружена Wayland-сессия, но не найден 'wf-recorder' — он "
                "нужен для захвата видео под Wayland на wlroots-композиторах "
                "(Sway, Hyprland, River, Wayfire, Labwc и т.п.). Установите "
                "его, например: 'sudo apt install wf-recorder' (Debian/"
                "Ubuntu) или 'sudo pacman -S wf-recorder' (Arch). Если у вас "
                "GNOME или KDE на Wayland — wf-recorder там не работает "
                "(нужен захват через системный портал), попробуйте запустить "
                "с --display-server x11 (часто работает через XWayland) "
                "либо выберите audio-контейнер вместо видео."
            )
        geometry = (
            f"{geom['capture_x']},{geom['capture_y']} "
            f"{geom['capture_width']}x{geom['capture_height']}"
        )
        cmd = [
            "wf-recorder",
            "-g", geometry,
            "-r", str(fps),
            *_wfrecorder_codec_flags(container_id),
            "-f", str(out_path),
        ]
        print(f"Захват видео запущен (wf-recorder / Wayland, {fps}fps)...")
        return subprocess.Popen(cmd)

    # --- x11 (в т.ч. XWayland) — как и раньше ---
    display = os.environ.get("DISPLAY", ":0")
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "x11grab", "-framerate", str(fps),
        "-video_size", f"{geom['capture_width']}x{geom['capture_height']}",
        "-i", f"{display}+{geom['capture_x']},{geom['capture_y']}",
        *video_codec_args(container_id),
        str(out_path),
    ]
    print(f"Захват видео запущен (x11grab, {fps}fps)...")
    return subprocess.Popen(cmd)
