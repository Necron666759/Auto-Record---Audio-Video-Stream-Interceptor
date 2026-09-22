#!/usr/bin/env python3
"""
format_options.py

Единый источник правды о том, какие контейнеры доступны для какого
сервиса (Suno / YouTube), как они выглядят в блочном меню (подпись,
цвет — под стиль скриншота "Free Audio Video Pack"), и какими
конкретно аргументами ffmpeg кодируется каждый из них.

Ничего не запускает само — просто описывает данные и умеет строить
ffmpeg-аргументы. Используется:
  - recorder_gui.py            — рисует блочное меню из CONTAINERS_BY_SERVICE
  - auto_record_suno.py     — берёт audio-контейнер по имени, зовёт build_audio_encode_args()
  - auto_record_youtube.py     — то же для audio, плюс build_video_encode_args() для видео

Требования по качеству (заданы пользователем):
  - Исходная (сырая) запись всегда идёт в максимальном разумном качестве:
    96 kHz / 24 bit PCM (WAV) — см. RAW_CAPTURE_RATE / RAW_CAPTURE_SAMPLE_FMT.
  - Если пользователь НЕ выбрал ни один контейнер в меню — итоговый файл
    так и остаётся WAV 96kHz/24bit (без перекодирования).
  - Lossy-контейнеры (mp3/aac/ogg) кодируются из этого сырья с "потолком"
    качества, который они реально поддерживают: 320 kbit CBR, 48 kHz.
    ВАЖНО: у mp3 (и вообще у lossy-кодеков) нет понятия "битность" в
    привычном PCM-смысле — это психоакустический кодек, а не PCM-контейнер.
    "24 бита" технически неприменимо к mp3-потоку; максимум, что можно
    сделать — кодировать из 24-битного источника с максимальным битрейтом,
    что и делается.
  - FLAC — lossless, поэтому сохраняет полные 96kHz/24bit (даунсемплинг
    ему не нужен и только вредит).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------
# Качество "сырой" записи (до любого выбора контейнера пользователем)
# --------------------------------------------------------------------------

RAW_CAPTURE_RATE = 96000          # Hz
RAW_CAPTURE_BIT_DEPTH = 24        # bit
RAW_CAPTURE_PAREC_FORMAT = "s24le"     # формат для parec --format=
RAW_CAPTURE_FFMPEG_PCM_FMT = "s24le"   # формат для ffmpeg -f (сырой поток с parec)
RAW_CAPTURE_WAV_CODEC = "pcm_s24le"    # кодек для итогового wav-контейнера
RAW_CAPTURE_CHANNELS = 2               # кол-во каналов (parec --channels=)
RAW_CAPTURE_BYTES_PER_SAMPLE = RAW_CAPTURE_BIT_DEPTH // 8  # 24 bit -> 3 байта/сэмпл
# Размер одного сэмпл-фрейма сырого PCM-потока в байтах (3 байта * 2 канала
# = 6 для s24le/стерео). Любое чтение/запись/отбрасывание байт этого потока
# (см. _relay_audio_to_encoder в auto_record_suno.py) должно идти строго
# кратно этому значению — иначе поток "съезжает по фазе" и ffmpeg падает
# с ошибками декодирования вроде "Invalid PCM packet".
RAW_CAPTURE_FRAME_SIZE = RAW_CAPTURE_BYTES_PER_SAMPLE * RAW_CAPTURE_CHANNELS

# Частота/битность, до которых имеет смысл кодировать lossy-форматы
# (выше — они всё равно не дадут прироста качества, только раздутый файл)
LOSSY_TARGET_RATE = 48000
LOSSY_TARGET_BITRATE = "320k"


@dataclass(frozen=True)
class ContainerOption:
    id: str                 # 'mp3', 'flac', 'mkv', ... — используется в --container/--video-container
    label: str               # подпись на блоке в GUI, напр. "WAV -> MP3"
    color: str                # цвет блока (hex), в духе скриншота Free Audio Video Pack
    kind: str                 # 'audio' или 'video'
    extension: str             # '.mp3', '.mkv', ...
    services: tuple[str, ...]  # ('suno',) / ('youtube',) / ('suno', 'youtube')
    # Функция, которая по (ffmpeg_bin, has_libfdk_aac) возвращает список
    # аргументов кодека аудио для ffmpeg (без входа/выхода/путей).
    audio_args_fn: Callable[[], list[str]] | None = None
    # Для видео-контейнеров: аргументы видеокодека + аудиокодека отдельно,
    # т.к. видео и звук в youtube-бэкенде кодируются в одном финальном
    # проходе (мультиплексирование двух источников), см. auto_record_youtube.py
    video_args_fn: Callable[[], list[str]] | None = None


def _mp3_args() -> list[str]:
    # Настоящий CBR (не VBR/ABR): для libmp3lame это достигается заданием
    # -b:a БЕЗ -qscale:a. 320k — максимальный битрейт, который поддерживает
    # формат mp3 в принципе.
    return [
        "-c:a", "libmp3lame", "-b:a", LOSSY_TARGET_BITRATE,
        "-ar", str(LOSSY_TARGET_RATE), "-ac", "2",
    ]


def _aac_args() -> list[str]:
    # libfdk_aac заметно качественнее встроенного aac-энкодера ffmpeg на
    # тех же битрейтах, но лицензионно не входит в большинство сборок
    # ffmpeg по умолчанию. Выбор энкодера делает aac_encoder_name()
    # в encode_helpers.py в момент кодирования (проверяет, что реально
    # доступно в конкретном бинарнике ffmpeg).
    return [
        "-c:a", "AAC_ENCODER_PLACEHOLDER", "-b:a", LOSSY_TARGET_BITRATE,
        "-ar", str(LOSSY_TARGET_RATE), "-ac", "2",
    ]


def _ogg_args() -> list[str]:
    # libvorbis: -q:a 10 — максимальное доступное качество (это выше, чем
    # дал бы -b:a 320k в среднем; т.к. пользователь просил "не ниже" 320k,
    # берём максимум формата, а не жёстко фиксированный битрейт).
    return [
        "-c:a", "libvorbis", "-q:a", "10",
        "-ar", str(LOSSY_TARGET_RATE), "-ac", "2",
    ]


def _flac_args() -> list[str]:
    # Lossless — сохраняем родные 96kHz/24bit, даунсемплинг не делаем.
    return [
        "-c:a", "flac", "-compression_level", "8",
        "-sample_fmt", "s32", "-ar", str(RAW_CAPTURE_RATE), "-ac", "2",
    ]


AUDIO_CONTAINERS: list[ContainerOption] = [
    ContainerOption(
        id="mp3", label="WAV -> MP3", color="#4a4a4a", kind="audio",
        extension=".mp3", services=("suno", "youtube"), audio_args_fn=_mp3_args,
    ),
    ContainerOption(
        id="aac", label="WAV -> AAC", color="#d9a52a", kind="audio",
        extension=".m4a", services=("suno", "youtube"), audio_args_fn=_aac_args,
    ),
    ContainerOption(
        id="ogg", label="WAV -> OGG", color="#7a4fae", kind="audio",
        extension=".ogg", services=("suno", "youtube"), audio_args_fn=_ogg_args,
    ),
    ContainerOption(
        id="flac", label="WAV -> FLAC", color="#3f8f4f", kind="audio",
        extension=".flac", services=("suno", "youtube"), audio_args_fn=_flac_args,
    ),
]


# Видео-кодеки для каждого видео-контейнера. Захват экрана (x11grab под
# X11 / wf-recorder под Wayland, см. screen_capture.py) кодируется этими
# аргументами сразу в целевой видеокодек (без промежуточного лишнего
# перекодирования); отдельно записанный звук (см. ниже) сшивается с этим
# видео в один финальный файл на отдельном шаге (-c:v copy на этом шаге,
# т.е. видео просто копируется как есть, без повторного кодирования).
_VIDEO_CODEC_ARGS: dict[str, list[str]] = {
    "mp4": ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p"],
    "mkv": ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "18", "-b:v", "0"],
    "avi": ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p"],
    "mov": ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p"],
    "flv": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p"],
    # 3GP исторически мобильный формат с жёсткими ограничениями кодеков у
    # некоторых плееров; ffmpeg допускает h264 baseline, что даёт максимум
    # практически достижимого качества в этом контейнере.
    "3gp": ["-c:v", "libx264", "-profile:v", "baseline", "-preset", "slow",
            "-crf", "20", "-pix_fmt", "yuv420p"],
}

# ffmpeg-имя мьюксера (формата контейнера, флаг -f) для каждого
# видео-контейнера. Задаётся ЯВНО на финальном шаге сшивания видео+звука
# (см. auto_record_youtube.py) вместо того, чтобы полагаться только на
# автоопределение по расширению файла — так выбранный пользователем
# контейнер гарантированно не может "подмениться" каким-то другим
# (например, mp4 внезапно сохраниться как matroska/mkv).
FFMPEG_MUXER_NAME: dict[str, str] = {
    "mp4": "mp4", "mkv": "matroska", "webm": "webm", "avi": "avi",
    "mov": "mov", "flv": "flv", "3gp": "3gp",
}


def video_codec_args(container_id: str) -> list[str]:
    if container_id not in _VIDEO_CODEC_ARGS:
        raise ValueError(f"Неизвестный видео-контейнер: {container_id}")
    return list(_VIDEO_CODEC_ARGS[container_id])


# --------------------------------------------------------------------------
# Звук ВНУТРИ видео-контейнера
# --------------------------------------------------------------------------
#
# Требования пользователя:
#   1. Перед началом записи видео пользователь должен иметь возможность
#      явно выбрать audio-контейнер (mp3/aac/ogg/flac) отдельно для звука
#      внутри видео — независимо от выбора видео-контейнера.
#   2. Если для видео явный audio-контейнер НЕ выбран — звук в готовом
#      видео должен остаться WAV 96kHz/24bit (как и в "чистом" аудио-
#      сценарии без выбора контейнера вообще).
#
# Правило (2) в точности выполнимо только для тех видео-контейнеров,
# которые физически способны нести сырой PCM-звук — это проверено
# эмпирически через сам ffmpeg (пробным мультиплексированием), а не
# предполагается "на слово":
#   mp4/mkv/avi/mov -> pcm_s24le 96kHz/24bit принимается ffmpeg-мьюксером
#                       без единой жалобы.
#   webm  -> ffmpeg: "Only VP8 or VP9 or AV1 video and Vorbis or Opus
#            audio ... are supported for WebM" — сырой PCM в принципе
#            запрещён спецификацией контейнера.
#   flv   -> ffmpeg: "FLV does not support sample rate 96000, choose from
#            (44100, 22050, 11025)" — raw PCM в FLV урезан до низких
#            частот и не имеет понятия "24 бита" вовсе.
#   3gp   -> ffmpeg: "Could not find tag for codec pcm_s24le ... codec not
#            currently supported in container" — 3GP как мобильный
#            профиль поддерживает только AMR/AAC.
# Для этих трёх (webm/flv/3gp) используется лучший практически доступный
# вариант вместо WAV — это единственное отступление от правила (2), оно
# продиктовано форматом, а не выбором скрипта, и явно объясняется
# пользователю (см. video_default_audio_reason).

def _webm_fallback_audio_args() -> list[str]:
    # WebM: только Vorbis или Opus (см. пояснение выше). Opus качественнее
    # Vorbis на сопоставимом битрейте — берём его с тем же "потолком"
    # битрейта, что и у явно выбираемых lossy-контейнеров.
    return ["-c:a", "libopus", "-b:a", LOSSY_TARGET_BITRATE, "-ar", "48000", "-ac", "2"]


def _flv_fallback_audio_args() -> list[str]:
    # FLV: raw PCM ограничен низкими частотами дискретизации и не имеет
    # 24-битного режима, Vorbis/FLAC в FLV не поддерживаются вовсе
    # (ffmpeg: "Audio codec 'vorbis'/'flac' not compatible with FLV").
    # AAC — лучший практически доступный в FLV вариант.
    return ["-c:a", "AAC_ENCODER_PLACEHOLDER", "-b:a", LOSSY_TARGET_BITRATE,
             "-ar", str(LOSSY_TARGET_RATE), "-ac", "2"]


def _3gp_fallback_audio_args() -> list[str]:
    # 3GP (мобильный профиль MP4): поддерживает только AMR-NB/AMR-WB/AAC
    # (ffmpeg отказывает mp3/vorbis/flac/pcm: "codec not currently
    # supported in container"). AAC — лучший практически доступный
    # вариант; битрейт ограничен разумным для мобильного профиля.
    return ["-c:a", "AAC_ENCODER_PLACEHOLDER", "-b:a", "128k",
             "-ar", str(LOSSY_TARGET_RATE), "-ac", "2"]


# Контейнеры, физически способные нести сырой PCM-звук 96kHz/24bit без
# каких-либо жалоб со стороны ffmpeg-мьюксера (проверено эмпирически).
VIDEO_PCM_CAPABLE: frozenset[str] = frozenset({"mp4", "mkv", "avi", "mov"})

_VIDEO_AUDIO_FALLBACK_FNS: dict[str, Callable[[], list[str]]] = {
    "webm": _webm_fallback_audio_args,
    "flv": _flv_fallback_audio_args,
    "3gp": _3gp_fallback_audio_args,
}

_VIDEO_AUDIO_FALLBACK_REASON: dict[str, str] = {
    "webm": ("WebM по спецификации допускает для звука только Vorbis или "
             "Opus — сырой WAV/PCM в нём технически невозможен. Записываю "
             f"звук в Opus {LOSSY_TARGET_BITRATE} (лучшее доступное в этом "
             "контейнере качество) вместо WAV 96kHz/24bit."),
    "flv": ("FLV не поддерживает ни 96kHz/24bit PCM (raw-PCM в FLV "
            "урезан максимум до 44100Hz и не имеет 24-битного режима), "
            "ни Vorbis/FLAC — из практически доступных в FLV кодеков "
            f"лучший это AAC. Записываю звук в AAC {LOSSY_TARGET_BITRATE} "
            "вместо WAV 96kHz/24bit."),
    "3gp": ("3GP (мобильный профиль) поддерживает только AMR или AAC для "
            "звука — PCM/Vorbis/FLAC в нём не поддерживаются. Записываю "
            "звук в AAC 128k (лучшее доступное в этом профиле) вместо "
            "WAV 96kHz/24bit."),
}

# Какие из выбираемых пользователем audio-контейнеров (mp3/aac/ogg/flac)
# можно явно выбрать в качестве звука для конкретного видео-контейнера —
# проверено эмпирически через ffmpeg (пробным мультиплексированием каждой
# пары видео-контейнер/аудио-кодек):
#   mov + flac  -> ffmpeg: "flac only supported in MP4" (запрещено мьюксером mov)
#   webm + *    -> см. _webm_fallback_audio_args (только vorbis/opus)
#   flv + ogg/flac -> ffmpeg: "Audio codec 'vorbis'/'flac' not compatible with FLV"
#   3gp + mp3/ogg/flac -> ffmpeg: "codec not currently supported in container"
VIDEO_AUDIO_COMPAT: dict[str, tuple[str, ...]] = {
    "mp4": ("mp3", "aac", "ogg", "flac"),
    "mkv": ("mp3", "aac", "ogg", "flac"),
    "avi": ("mp3", "aac", "ogg", "flac"),
    "mov": ("mp3", "aac", "ogg"),
    "webm": ("ogg",),
    "flv": ("mp3", "aac"),
    "3gp": ("aac",),
}


def video_pcm_capable(video_container_id: str) -> bool:
    return video_container_id in VIDEO_PCM_CAPABLE


def compatible_audio_choices(video_container_id: str) -> tuple[str, ...]:
    """Список audio-контейнеров (mp3/aac/ogg/flac), которые пользователь
    МОЖЕТ явно выбрать для звука внутри данного видео-контейнера."""
    return VIDEO_AUDIO_COMPAT.get(video_container_id, ())


def video_default_audio_args(video_container_id: str) -> list[str]:
    """Аргументы ffmpeg для звука, когда пользователь НЕ выбрал явный
    audio-контейнер для видео. См. пояснение над VIDEO_PCM_CAPABLE:
    WAV 96kHz/24bit без перекодирования там, где контейнер это физически
    поддерживает; иначе — задокументированный fallback (см.
    video_default_audio_reason)."""
    if video_container_id in VIDEO_PCM_CAPABLE:
        return ["-c:a", RAW_CAPTURE_WAV_CODEC, "-ar", str(RAW_CAPTURE_RATE),
                 "-ac", str(RAW_CAPTURE_CHANNELS)]
    fallback_fn = _VIDEO_AUDIO_FALLBACK_FNS.get(video_container_id)
    if fallback_fn is None:
        raise ValueError(f"Неизвестный видео-контейнер: {video_container_id}")
    return fallback_fn()


def video_default_audio_reason(video_container_id: str) -> str | None:
    """None для PCM-совместимых контейнеров (звук остаётся WAV 96/24bit
    как и требуется, без каких-либо оговорок)."""
    return _VIDEO_AUDIO_FALLBACK_REASON.get(video_container_id)


VIDEO_CONTAINERS: list[ContainerOption] = [
    ContainerOption(id="mp4", label="Запись -> MP4", color="#4a4a4a", kind="video",
                     extension=".mp4", services=("youtube",)),
    ContainerOption(id="mkv", label="Запись -> MKV", color="#d9a52a", kind="video",
                     extension=".mkv", services=("youtube",)),
    ContainerOption(id="webm", label="Запись -> WebM", color="#3f8f4f", kind="video",
                     extension=".webm", services=("youtube",)),
    ContainerOption(id="avi", label="Запись -> AVI", color="#7a4fae", kind="video",
                     extension=".avi", services=("youtube",)),
    ContainerOption(id="mov", label="Запись -> MOV", color="#2f7fae", kind="video",
                     extension=".mov", services=("youtube",)),
    ContainerOption(id="flv", label="Запись -> FLV", color="#ae6f2f", kind="video",
                     extension=".flv", services=("youtube",)),
    ContainerOption(id="3gp", label="Запись -> 3GP", color="#5f9f6f", kind="video",
                     extension=".3gp", services=("youtube",)),
]

ALL_CONTAINERS: list[ContainerOption] = AUDIO_CONTAINERS + VIDEO_CONTAINERS
_BY_ID = {c.id: c for c in ALL_CONTAINERS}


def get_container(container_id: str | None) -> ContainerOption | None:
    if not container_id:
        return None
    opt = _BY_ID.get(container_id)
    if opt is None:
        raise ValueError(f"Неизвестный контейнер: {container_id!r}")
    return opt


def containers_for_service(service: str) -> dict[str, list[ContainerOption]]:
    """Возвращает {'audio': [...], 'video': [...]} — то, что должно быть
    показано в блочном меню для данного сервиса.

    - suno: только audio-контейнеры (mp3/aac/ogg/flac).
    - youtube: video-контейнеры (mp4/mkv/webm/avi/mov/flv/3gp) ПЛЮС те же
      audio-контейнеры — чтобы можно было записать чисто звук из ролика,
      без видео.
    """
    audio = [c for c in AUDIO_CONTAINERS if service in c.services]
    video = [c for c in VIDEO_CONTAINERS if service in c.services]
    return {"audio": audio, "video": video}
