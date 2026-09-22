#!/usr/bin/env python3
"""
encode_helpers.py

Общие для suno- и youtube-бэкендов функции кодирования, не завязанные
на конкретный сервис:
  - определение лучшего доступного AAC-энкодера в данном ffmpeg
  - кодирование сырого WAV (96kHz/24bit) в выбранный пользователем
    audio-контейнер (или "оставить как есть", если контейнер не выбран)
  - запуск сырой записи через parec в 96kHz/24bit

Ничего не знает про Suno/YouTube/Firefox — только про ffmpeg/parec.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from format_options import (
    RAW_CAPTURE_PAREC_FORMAT,
    RAW_CAPTURE_FFMPEG_PCM_FMT,
    RAW_CAPTURE_RATE,
    RAW_CAPTURE_WAV_CODEC,
    RAW_CAPTURE_CHANNELS,
    ContainerOption,
    get_container,
    compatible_audio_choices,
    video_default_audio_args,
    video_default_audio_reason,
)


def _ffmpeg_encoders(ffmpeg_bin: str) -> str:
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def best_aac_encoder(ffmpeg_bin: str) -> str:
    """libfdk_aac (если есть в сборке) даёт заметно более качественный
    AAC на том же битрейте, чем встроенный 'aac'. Большинство сборок
    ffmpeg из дистрибутивов не включают libfdk_aac по лицензионным
    причинам, поэтому всегда есть fallback на встроенный 'aac'."""
    try:
        encoders = _ffmpeg_encoders(ffmpeg_bin)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "aac"
    if re.search(r"\blibfdk_aac\b", encoders):
        return "libfdk_aac"
    return "aac"


def _resolve_aac_placeholder(ffmpeg_bin: str, args: list[str]) -> list[str]:
    """Подставляет реальный AAC-энкодер вместо AAC_ENCODER_PLACEHOLDER (см.
    format_options._aac_args и video-fallback функции для flv/3gp). Если в
    аргументах плейсхолдера нет — возвращает список как есть (no-op)."""
    if "AAC_ENCODER_PLACEHOLDER" not in args:
        return args
    aac_encoder = best_aac_encoder(ffmpeg_bin)
    resolved = [aac_encoder if a == "AAC_ENCODER_PLACEHOLDER" else a for a in args]
    if aac_encoder == "libfdk_aac":
        resolved += ["-cbr", "1"]
    else:
        print(
            "WARNING: в ffmpeg нет libfdk_aac — используется встроенный 'aac' "
            "энкодер без гарантии настоящего постоянного битрейта "
            "(это ограничение конкретной сборки ffmpeg, не скрипта). "
            "Для истинного CBR AAC нужна сборка ffmpeg с libfdk_aac.",
            file=sys.stderr,
        )
    return resolved


def resolve_audio_encode_args(ffmpeg_bin: str, container: ContainerOption) -> list[str]:
    """Разворачивает audio_args_fn контейнера, подставляя реальный
    AAC-энкодер вместо плейсхолдера (см. format_options._aac_args).

    Важная особенность встроенного в ffmpeg энкодера 'aac': в отличие от
    libmp3lame, у него нет режима настоящего постоянного битрейта (CBR) —
    -b:a там задаёт целевой/средний битрейт (по факту ближе к ABR), и на
    "простом" материале фактический битрейт может оказаться заметно ниже
    320k. Если в ffmpeg доступен libfdk_aac — используем его и добавляем
    '-cbr 1', что даёт настоящий постоянный битрейт, как и требовалось.
    Если доступен только встроенный 'aac' — используем его как лучший
    практически доступный вариант и сообщаем об этом ограничении."""
    return _resolve_aac_placeholder(ffmpeg_bin, list(container.audio_args_fn()))


def resolve_video_mux_audio_args(
    ffmpeg_bin: str,
    video_container_id: str,
    audio_container_id: str | None,
) -> tuple[list[str], str | None]:
    """Аргументы ffmpeg для звуковой дорожки при сшивании видео+звука в
    один файл video_container_id, и опциональная пояснительная заметка
    (не None, только когда пришлось отступить от WAV 96kHz/24bit по
    причинам, продиктованным самим форматом контейнера — см.
    format_options.video_default_audio_reason).

    - audio_container_id задан явно (mp3/aac/ogg/flac) -> используется он,
      если совместим с video_container_id (format_options.VIDEO_AUDIO_COMPAT);
      иначе ValueError с понятным списком совместимых вариантов.
    - audio_container_id не задан -> WAV 96kHz/24bit там, где контейнер
      это физически поддерживает, иначе — задокументированный fallback.
    """
    if audio_container_id:
        compat = compatible_audio_choices(video_container_id)
        if audio_container_id not in compat:
            readable = ", ".join(compat) if compat else "(нет совместимых audio-контейнеров)"
            raise ValueError(
                f"Audio-контейнер '{audio_container_id}' несовместим с "
                f"видео-контейнером '{video_container_id}'. Совместимые "
                f"варианты для {video_container_id}: {readable} (или не "
                f"выбирать ничего -> WAV 96kHz/24bit, если контейнер это "
                f"поддерживает)."
            )
        container = get_container(audio_container_id)
        return _resolve_aac_placeholder(ffmpeg_bin, list(container.audio_args_fn())), None

    args = video_default_audio_args(video_container_id)
    note = video_default_audio_reason(video_container_id)
    return _resolve_aac_placeholder(ffmpeg_bin, args), note


def parec_raw_capture_args(monitor_source: str) -> list[str]:
    """Аргументы parec для сырого захвата в 96kHz/24bit (см. RAW_CAPTURE_*
    в format_options.py). Используется вместо старых 44100/s16le."""
    return [
        "parec",
        f"--device={monitor_source}",
        f"--format={RAW_CAPTURE_PAREC_FORMAT}",
        f"--rate={RAW_CAPTURE_RATE}",
        f"--channels={RAW_CAPTURE_CHANNELS}",
    ]


def ffmpeg_raw_wav_args(ffmpeg_bin: str, raw_wav_path: Path) -> list[str]:
    """Аргументы ffmpeg, принимающего сырой PCM-поток из parec (см.
    parec_raw_capture_args) на stdin и пишущего его как WAV 96kHz/24bit
    без какого-либо перекодирования (lossless passthrough в PCM)."""
    return [
        ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", RAW_CAPTURE_FFMPEG_PCM_FMT, "-ar", str(RAW_CAPTURE_RATE), "-ac", "2",
        "-i", "-",
        "-c:a", RAW_CAPTURE_WAV_CODEC,
        str(raw_wav_path),
    ]


def encode_final_audio(
    ffmpeg_bin: str,
    raw_wav: Path,
    final_out: Path,
    container_id: str | None,
    extra_filter_args: list[str] | None = None,
) -> Path:
    """Кодирует сырой 96kHz/24bit WAV в выбранный пользователем контейнер.

    Поведение по умолчанию (пункт 3 требований): если container_id не
    задан (пользователь не выбрал ни один блок в меню) — НИКАКОГО
    перекодирования не делаем, итоговый файл это и есть raw_wav
    (96kHz/24bit), просто переименованный/скопированный в final_out.
    """
    container = get_container(container_id)
    extra_filter_args = extra_filter_args or []

    if container is None:
        if raw_wav != final_out:
            raw_wav.replace(final_out)
        return final_out

    if container.kind != "audio":
        raise ValueError(f"Контейнер {container_id!r} не является аудио-контейнером")

    encode_args = resolve_audio_encode_args(ffmpeg_bin, container)
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
            "-i", str(raw_wav),
            *extra_filter_args,
            *encode_args,
            str(final_out),
        ],
        check=True,
    )
    # Сырой промежуточный wav больше не нужен, если кодировали в другой формат.
    try:
        if raw_wav.exists() and raw_wav != final_out:
            raw_wav.unlink()
    except OSError:
        pass
    return final_out


def output_suffix_for_container(container_id: str | None) -> str:
    container = get_container(container_id)
    if container is None:
        return ".wav"
    return container.extension
