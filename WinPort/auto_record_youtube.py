"""
auto_record_youtube.py (Windows редакция)

Аналог auto_record_suno.py для YouTube: та же схема запуска отдельного
изолированного Firefox через geckodriver и та же изоляция звука (WASAPI
Process Loopback — только звук этого firefox.exe и его дочерних процессов, либо
аварийный режим --capture-mode device), но добавлена запись ВИДЕО через захват
экрана (ffmpeg gdigrab, см. screen_capture.py), синхронно со звуком, и
кодирование в выбранный пользователем видео-контейнер (mp4/mkv/webm/avi/mov/
flv/3gp) или, если выбран audio-контейнер — запись только звука (без видео
вообще), как в Suno.

Специально НЕ дублирует код запуска Firefox/изоляции звука — импортирует эти
функции и классы напрямую из auto_record_suno.py (файл должен лежать рядом).
Это гарантирует, что оба бэкенда используют одну и ту же проверенную логику
для этой части.

Портировано из Linux-версии без изменения логики. Отличия — только там, где
этого требует платформа:
  - звук: вместо parec/PulseAudio — WASAPI-захват (win_audio_loopback.py) ->
    ffmpeg (stdin) -> WAV 96kHz/24bit, как в Windows-версии Suno;
  - видео: вместо x11grab / wf-recorder — ffmpeg gdigrab (screen_capture.py);
    выбора «сервера отображения» (--display-server) на Windows нет;
  - остановка захвата видео — командой 'q' ffmpeg (а не SIGTERM);
  - управление из GUI — строкой STOP в stdin (--control-stdin), см. win_stdio.py;
  - поддержан --capture-mode device (аварийный режим захвата звука без
    изоляции по процессу — та же функция, что и у Suno);
  - при закрытии/падении Firefox браузер перезапускается (как в Windows-Suno).

--------------------------------------------------------------------
ВАЖНОЕ ОГРАНИЧЕНИЕ (прочитайте перед использованием):
--------------------------------------------------------------------
Запись видео сделана через захват экрана (gdigrab), а не через перехват самого
видеопотока YouTube. Это значит: скрипт записывает то, что
ФИЗИЧЕСКИ ОТОБРАЖАЕТСЯ на экране в области плеера — в тех же пикселях,
в которых показывает Firefox. Если окно/плеер маленький, а YouTube отдаёт
видео в 1080p/4K — на экране оно всё равно отрисовывается в размер
плеера, и захват экрана физически не может "восстановить" пиксели,
которых на экране никогда не было.

Поэтому для реально максимального качества:
  - Скрипт САМ по возможности разворачивает окно Firefox на весь экран
    (WebDriver "maximize window") и пытается перевести видео в
    полноэкранный режим плеера перед записью.
  - Если это не удалось (нет прав на fullscreen без жеста пользователя
    и т.п.) — скрипт печатает ПРЕДУПРЕЖДЕНИЕ с фактическим разрешением
    захвата против нативного разрешения видео (videoWidth/videoHeight),
    чтобы вы это увидели и могли вручную развернуть плеер на весь экран
    перед записью.

Пауза/продолжение (как это физически бесшовно сделано для Suno в
RecordingGate) для видео сознательно НЕ реализована: гейтинг видео на
уровне отдельных кадров экрана намного сложнее и рискует рассинхроном
звука с видео, что хуже, чем просто включить в запись короткие паузы.
Для youtube запись идёт от начала воспроизведения до события 'ended'
целиком, без вырезания пауз.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

# --- переиспользуем проверенную логику запуска Firefox/изоляции звука ---
from auto_record_suno import (
    ensure_geckodriver,           # noqa: F401 (used indirectly via GeckoSession)
    resolve_firefox_launch_plan,
    clear_firefox_cache,
    GeckoSession,
    BrowserGoneError,
    LoopbackCaptureError,
    ProcessLoopbackCapture,
    DefaultDeviceLoopbackCapture,
    RecordingGate,
    _relay_audio_to_encoder,
    list_audio_devices,
    navigate_with_retry,
    wait_for_internet,
    _looks_like_network_error,
    sanitize_filename,
    setup_control_channel,
    resolve_ffmpeg_bin as resolve_ffmpeg_bin_audio,
    resolve_ffmpeg_bin_for_encoder,
    _ffmpeg_has_encoder,
    ensure_ffmpeg_with_mp3,
)
from format_options import (
    RAW_CAPTURE_RATE,
    RAW_CAPTURE_BIT_DEPTH,
    FFMPEG_MUXER_NAME,
    compatible_audio_choices,
    get_container,
)
from encode_helpers import (
    ffmpeg_raw_wav_args_dynamic,
    encode_final_audio,
    output_suffix_for_container,
    resolve_video_mux_audio_args,
)
from screen_capture import (
    start_video_capture,
    request_video_stop,
    wait_video_capture,
    ensure_dpi_aware,
)


# --------------------------------------------------------------------------
# Видео-кодек: доступность libx264 / libvpx-vp9 / libopus в системном ffmpeg
# --------------------------------------------------------------------------

_VIDEO_CONTAINER_REQUIRED_ENCODER = {
    "mp4": "libx264", "mkv": "libx264", "avi": "libx264",
    "mov": "libx264", "flv": "libx264", "3gp": "libx264",
    "webm": "libvpx-vp9",
}

# Энкодер, дополнительно требуемый для ЗВУКА внутри видео (помимо
# видеокодека из _VIDEO_CONTAINER_REQUIRED_ENCODER выше) — в зависимости
# от того, выбрал ли пользователь явный audio-контейнер для видео или нет
# (см. format_options.video_default_audio_args / VIDEO_AUDIO_COMPAT).
# None означает "кодек встроен в ffmpeg (aac/flac) или это сырой PCM —
# отдельно проверять наличие библиотеки не нужно".
_AUDIO_CONTAINER_REQUIRED_ENCODER = {
    "mp3": "libmp3lame", "aac": None, "ogg": "libvorbis", "flac": None,
}
_VIDEO_DEFAULT_AUDIO_REQUIRED_ENCODER = {
    # mp4/mkv/avi/mov по умолчанию пишут сырой PCM (pcm_s24le, встроен в
    # ffmpeg) — для них здесь намеренно нет записи (требования нет).
    "webm": "libopus", "flv": None, "3gp": None,
}


def _required_audio_encoder(video_container_id: str, audio_container_id: str | None) -> str | None:
    if audio_container_id:
        return _AUDIO_CONTAINER_REQUIRED_ENCODER.get(audio_container_id)
    return _VIDEO_DEFAULT_AUDIO_REQUIRED_ENCODER.get(video_container_id)


def resolve_ffmpeg_bin_for_container(
    container_id: str | None,
    audio_container_id: str | None = None,
    auto_download: bool = True,
) -> str:
    """Подбирает ffmpeg с нужными кодеками для выбранного видео-контейнера
    И для звука внутри него (либо явно выбранного audio_container_id,
    либо того, что понадобится по умолчанию — см. _required_audio_encoder).

    Раньше для видео-контейнеров здесь проверялся ТОЛЬКО системный ffmpeg
    (через shutil.which) — в отличие от аудио-контейнеров, которые уже
    умели переиспользовать закешированную/автоскачанную статическую
    сборку ffmpeg (см. resolve_ffmpeg_bin_audio /
    resolve_ffmpeg_bin_for_encoder в auto_record_suno.py). Из-за этого,
    если у системного ffmpeg не было, например, libx264 (частая ситуация
    в некоторых дистрибутивах/сборках), скрипт сразу падал с ошибкой,
    даже если в кэше (папка кэша auto_record_suno, ffmpeg.exe) уже лежала
    полная сборка с libx264/libvpx-vp9 — она просто никогда не
    проверялась и не использовалась для видео.

    Теперь видео-контейнеры используют ту же самую общую логику
    подбора/автозагрузки ffmpeg, что и аудио: сначала системный ffmpeg,
    затем (если разрешено auto_download) закешированная или заново
    скачанная Windows-сборка ffmpeg (gyan.dev essentials) — она
    включает libx264/libvpx-vp9 наравне с libmp3lame/aac/vorbis/flac/
    libopus. strict=True сохраняет прежнее поведение: явная понятная
    ошибка, если ни системный, ни автоскачанный ffmpeg не подходят,
    вместо тихого отката на заведомо нерабочий кодек.
    """
    container = get_container(container_id)
    if container is None or container.kind == "audio":
        # Чисто аудио-запись (или дефолтный wav) — переиспользуем ту же
        # логику подбора ffmpeg, что и Suno-бэкенд.
        return resolve_ffmpeg_bin_audio(container_id, auto_download=auto_download)

    required_video = _VIDEO_CONTAINER_REQUIRED_ENCODER[container_id]
    ffmpeg_bin = resolve_ffmpeg_bin_for_encoder(required_video, auto_download=auto_download, strict=True)

    required_audio = _required_audio_encoder(container_id, audio_container_id)
    if required_audio is not None and not _ffmpeg_has_encoder(ffmpeg_bin, required_audio):
        # У ffmpeg, подобранного под видеокодек, нет нужного аудиокодека
        # (частая ситуация: системный ffmpeg собран с libx264, но без
        # libopus/libmp3lame/libvorbis). Пробуем автоскачанную сборку —
        # она содержит все кодеки разом; если и там нет/автозагрузка
        # отключена — явная ошибка вместо того, чтобы дать ffmpeg упасть
        # без объяснений уже посреди записи.
        if auto_download:
            try:
                downloaded = ensure_ffmpeg_with_mp3()
                if _ffmpeg_has_encoder(downloaded, required_audio):
                    return downloaded
            except Exception as exc:
                raise RuntimeError(
                    f"Для звука в этом контейнере нужен кодек '{required_audio}', "
                    f"которого нет в {ffmpeg_bin}, а автозагрузка полной сборки "
                    f"ffmpeg не удалась ({exc})."
                ) from exc
        raise RuntimeError(
            f"Для звука в этом контейнере нужен кодек '{required_audio}', "
            f"которого нет в {ffmpeg_bin} (автозагрузка отключена флагом --no-auto-ffmpeg)."
        )
    return ffmpeg_bin


# --------------------------------------------------------------------------
# Определение геометрии видео-элемента на экране (для gdigrab)
# --------------------------------------------------------------------------

_ELEMENT_GEOMETRY_SCRIPT = """
const el = document.querySelector('video');
if (!el) return null;
const rect = el.getBoundingClientRect();
let title = '';
try { title = (document.title || '').replace(/\\s*-\\s*YouTube\\s*$/, ''); } catch (_) {}
return {
    duration: Number.isFinite(el.duration) ? el.duration : -1,
    readyState: el.readyState,
    paused: !!el.paused,
    ended: !!el.ended,
    src: el.currentSrc || el.src || '',
    title: title,
    videoWidth: el.videoWidth,
    videoHeight: el.videoHeight,
    rectLeft: rect.left,
    rectTop: rect.top,
    rectWidth: rect.width,
    rectHeight: rect.height,
    innerScreenX: window.mozInnerScreenX || 0,
    innerScreenY: window.mozInnerScreenY || 0,
    devicePixelRatio: window.devicePixelRatio || 1,
};
"""


def try_maximize_and_fullscreen(session: GeckoSession) -> None:
    """Наилучшее усилие: разворачиваем окно Firefox на весь экран и
    пытаемся перевести video-элемент в fullscreen (может не сработать без
    настоящего пользовательского жеста — тогда просто печатаем предупреждение
    позже, когда сравним rect-размер с videoWidth/videoHeight)."""
    try:
        # через session._call (а не голый urllib): локальный HTTP-клиент воркера
        # не ходит через системный прокси и переводит сбои в BrowserGoneError
        session._call("POST", "/window/maximize", {}, timeout=10.0)
    except Exception as exc:
        print(f"WARNING: не удалось развернуть окно Firefox на весь экран ({exc}).", file=sys.stderr)

    try:
        session.execute_script("""
            const el = document.querySelector('video');
            if (el && el.requestFullscreen) { el.requestFullscreen().catch(() => {}); }
        """)
    except Exception:
        pass
    time.sleep(0.5)


def get_video_screen_geometry(session: GeckoSession) -> dict:
    info = session.execute_script(_ELEMENT_GEOMETRY_SCRIPT)
    if info is None:
        raise RuntimeError("Не найден <video> элемент на странице.")

    dpr = info["devicePixelRatio"] or 1
    abs_x = round((info["innerScreenX"] + info["rectLeft"]) * dpr)
    abs_y = round((info["innerScreenY"] + info["rectTop"]) * dpr)
    width = round(info["rectWidth"] * dpr)
    height = round(info["rectHeight"] * dpr)
    # gdigrab/x264 требуют чётные размеры для большинства видеокодеков
    width -= width % 2
    height -= height % 2

    info.update(capture_x=abs_x, capture_y=abs_y, capture_width=width, capture_height=height)

    native_w, native_h = info.get("videoWidth") or 0, info.get("videoHeight") or 0
    if native_w and native_h and (width < native_w * 0.95 or height < native_h * 0.95):
        print(
            f"WARNING: захватываемая область плеера ({width}x{height}) заметно "
            f"меньше нативного разрешения видео ({native_w}x{native_h}). Экранный "
            f"захват физически не может дать пикселей больше, чем реально "
            f"отрисовано на экране — разверните плеер на весь экран/окно для "
            f"максимального качества записи.",
            file=sys.stderr,
        )
    return info


# --------------------------------------------------------------------------
# Ожидание конца видео (без гейтинга пауз — см. пояснение в шапке файла)
# --------------------------------------------------------------------------

def wait_for_video_end(session: GeckoSession, timeout: float, poll_interval: float = 0.5) -> str:
    script = """
    const el = document.querySelector('video');
    if (!el) return null;
    return { ended: !!el.ended, paused: !!el.paused };
    """
    deadline = time.monotonic() + timeout + 10
    while time.monotonic() < deadline:
        state = session.execute_script(script)
        if state is not None and state.get("ended"):
            return "ended"
        time.sleep(poll_interval)
    print("WARNING: таймаут ожидания окончания видео — останавливаю запись по расчётному времени.",
          file=sys.stderr)
    return "timeout"


# --------------------------------------------------------------------------
# Запись одного ролика
# --------------------------------------------------------------------------

def record_and_save_video(
    session: GeckoSession,
    args: argparse.Namespace,
    ffmpeg_bin: str,
    dest_dir: Path,
    capture_factory,
    stop_event: threading.Event | None = None,
) -> Path:
    print("Автообнаружение видео (ждите начала воспроизведения)...")
    deadline = time.monotonic() + 600
    info = None
    while time.monotonic() < deadline:
        candidate = session.execute_script(_ELEMENT_GEOMETRY_SCRIPT)
        if candidate is not None and candidate.get("readyState", 0) >= 1 and not candidate.get("paused", True):
            info = candidate
            break
        time.sleep(0.3)
    if info is None:
        raise TimeoutError("Не дождался начала воспроизведения видео.")

    container_id = args.container
    container = get_container(container_id)
    is_video = container is not None and container.kind == "video"

    out_suffix = output_suffix_for_container(container_id) if not is_video else container.extension
    out_name = args.out_name or sanitize_filename(info.get("title", ""))
    print(f"Обнаружено видео: \"{info.get('title', '')}\"")

    duration = info.get("duration", -1)
    expected_time = duration if duration and duration > 0 else 3600
    if duration and duration > 0:
        print(f"Длительность: {duration:.1f}s")
    else:
        print("WARNING: не удалось определить длительность видео заранее.", file=sys.stderr)

    if is_video:
        try_maximize_and_fullscreen(session)
        geom = get_video_screen_geometry(session)
        print(f"Область захвата экрана: {geom['capture_width']}x{geom['capture_height']} "
              f"@ ({geom['capture_x']},{geom['capture_y']})  "
              f"(нативное разрешение видео: {geom.get('videoWidth')}x{geom.get('videoHeight')})")

    raw_wav, final_video_only, final_out = _build_paths(dest_dir, out_name, is_video, container_id)

    capture = ffmpeg_audio_proc = ffmpeg_video_proc = relay_thread = None
    relay_stop_event = threading.Event()
    gate = RecordingGate(initially_open=True)   # без гейтинга пауз (см. шапку) — всегда открыт
    try:
        # --- аудио: WASAPI-захват -> ffmpeg -> сырой WAV 96kHz/24bit, без гейтинга пауз (см. шапку) ---
        capture = capture_factory()
        print(
            f"Запускаю запись звука (WASAPI [{capture.description}] -> ffmpeg -> "
            f"WAV {RAW_CAPTURE_RATE}Hz/{RAW_CAPTURE_BIT_DEPTH}bit)..."
        )
        ffmpeg_audio_proc = subprocess.Popen(
            ffmpeg_raw_wav_args_dynamic(
                ffmpeg_bin, raw_wav, capture.sample_rate, capture.channels, capture.sample_fmt,
            ),
            stdin=subprocess.PIPE,
        )
        relay_thread = threading.Thread(
            target=_relay_audio_to_encoder,
            args=(capture.stdout, ffmpeg_audio_proc.stdin, gate, relay_stop_event, capture.bytes_per_frame),
            daemon=True,
        )
        relay_thread.start()

        # --- видео: захват экрана прямо в целевой видеокодек (если выбран
        # видео-контейнер) — ffmpeg gdigrab, см. screen_capture.py ---
        if is_video:
            ffmpeg_video_proc = start_video_capture(
                ffmpeg_bin, container_id, geom, args.fps, final_video_only,
            )

        print("Запись идёт — жду события окончания видео (Ctrl+C для отмены)...")
        # ВАЖНО: Ctrl+C / "Стоп" из GUI (в Windows-версии — команда STOP через stdin,
        # которая тоже превращается в KeyboardInterrupt главного потока) прилетает
        # СЮДА, пока мы ждём естественного конца видео. Раньше KeyboardInterrupt
        # отсюда просто пролетал насквозь через этот try/finally (finally
        # корректно останавливал захват — сырые .mkv/.wav оставались на
        # диске, — но САМО ИСКЛЮЧЕНИЕ продолжало лететь дальше и выходило
        # из функции целиком, минуя ВЕСЬ код кодирования/сшивания звука
        # ниже). Именно поэтому ручная остановка через GUI никогда не
        # доходила до конвертации/запаковки: функция обрывалась раньше,
        # чем успевала до этого дойти. Ловим здесь и считаем это таким же
        # полноправным "концом записи", как естественное окончание видео,
        # — код кодирования/сшивания ниже выполняется в точности так же.
        try:
            reason = wait_for_video_end(session, expected_time)
        except KeyboardInterrupt:
            reason = "interrupted"
            if stop_event is not None:
                stop_event.set()
        print(f"Событие окончания: {reason}")

    finally:
        print("Останавливаю захват...")
        # Остановку видео (команда 'q' — ffmpeg дописывает контейнер) и звука
        # начинаем одновременно, затем ждём каждого.
        request_video_stop(ffmpeg_video_proc)
        relay_stop_event.set()
        if capture is not None:
            try:
                capture.stop(timeout=10)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: не удалось штатно остановить захват звука ({exc}).", file=sys.stderr)
            if getattr(capture, "error", None) is not None:
                print(f"WARNING: захват звука прервался с ошибкой: {capture.error}", file=sys.stderr)
        if relay_thread is not None:
            relay_thread.join(timeout=10)
        if ffmpeg_audio_proc is not None:
            try:
                ffmpeg_audio_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ffmpeg_audio_proc.terminate()
                try:
                    ffmpeg_audio_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    ffmpeg_audio_proc.kill()
        wait_video_capture(ffmpeg_video_proc, timeout=15)

    if ffmpeg_audio_proc is not None and ffmpeg_audio_proc.returncode not in (0, None):
        raise RuntimeError(
            f"ffmpeg завершился с ошибкой при захвате звука (код {ffmpeg_audio_proc.returncode}) "
            "— запись не удалась, см. вывод ffmpeg выше."
        )

    if not raw_wav.exists() or raw_wav.stat().st_size == 0:
        raise RuntimeError("Аудиофайл записи пуст или не создан.")

    if capture is not None and getattr(capture, "total_frames", 0) > 0 and getattr(capture, "audible_frames", 1) == 0:
        print(
            "WARNING: в записи только тишина — Firefox не отдал звук выбранному захвату. "
            "Проверьте, что видео действительно играет и не заглушено в микшере громкости; "
            "если повторяется — попробуйте аварийный режим (--capture-mode device).",
            file=sys.stderr,
        )

    if not is_video:
        # Чисто звук: тот же путь кодирования, что и в Suno-бэкенде.
        encode_final_audio(ffmpeg_bin, raw_wav, final_out, container_id)
        print(f"Готово (только звук): {final_out}")
        return final_out

    if not final_video_only.exists() or final_video_only.stat().st_size == 0:
        raise RuntimeError("Видеофайл записи пуст или не создан.")

    audio_container_id = getattr(args, "audio_container", None)
    audio_mux_args, audio_note = resolve_video_mux_audio_args(ffmpeg_bin, container_id, audio_container_id)
    if audio_note:
        print(f"WARNING: {audio_note}", file=sys.stderr)

    audio_desc = audio_container_id or ("WAV 96kHz/24bit" if not audio_note else "fallback (см. предупреждение выше)")
    print(f"Кодирую звук ({audio_desc}) и сшиваю с видео в {container_id} ({final_out.name})...")
    # H.264 (все видео-контейнеры, кроме webm, который использует VP9)
    # записан video-only захватом в length-prefixed (avcC) форме, как её
    # хранит промежуточный matroska-контейнер. AVI ожидает "сырую"
    # Annex-B форму (со стартовыми кодами) — без конвертации ffmpeg прямо
    # отказывается копировать поток корректно ("H.264 bitstream
    # malformed, no startcode found" — проверено эмпирически) и результат
    # получается ПОВРЕЖДЁННЫМ, даже когда какой-то файл всё же
    # записывается. h264_mp4toannexb — штатный ffmpeg-фильтр именно для
    # этого преобразования; он безопасен (no-op) и для контейнеров, где
    # эта конвертация не обязательна (mp4/mkv/mov/flv/3gp — проверено
    # эмпирически на каждом), поэтому применяется единообразно для всех
    # h264-контейнеров. Для webm (VP9) фильтр НЕ применяется — он
    # специфичен для H.264 и к VP9-потоку неприменим.
    video_copy_args = ["-c:v", "copy"]
    if _VIDEO_CONTAINER_REQUIRED_ENCODER[container_id] == "libx264":
        video_copy_args += ["-bsf:v", "h264_mp4toannexb"]
    try:
        subprocess.run(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-y",
                "-i", str(final_video_only),
                "-i", str(raw_wav),
                *video_copy_args,
                *audio_mux_args,
                "-shortest",
                "-f", FFMPEG_MUXER_NAME[container_id],
                str(final_out),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        # НЕ удаляем видео-только и wav-файлы при неудаче: если бы мы
        # молча стёрли их, пользователь остался бы вообще без записи.
        # Вместо этого явно сообщаем, что итоговый файл В ВЫБРАННОМ
        # КОНТЕЙНЕРЕ не был создан, и что сырые видео/звук всё ещё лежат
        # на диске — это как раз тот случай, который раньше выглядел как
        # "почему-то сохранилось в mkv вместо выбранного контейнера": на
        # самом деле сшивание в конечный контейнер просто не удавалось, а
        # промежуточный _video_only.mkv молча оставался единственным
        # файлом в папке назначения.
        raise RuntimeError(
            f"Не удалось сшить звук и видео в {container_id} ({exc}). "
            f"Исходники сохранены отдельно и НЕ удалены: видео без звука — "
            f"{final_video_only}, звук — {raw_wav}."
        ) from exc

    raw_wav.unlink(missing_ok=True)
    final_video_only.unlink(missing_ok=True)
    print(f"Готово: {final_out}")
    return final_out


def _build_paths(dest_dir: Path, out_name: str, is_video: bool, container_id: str | None):
    """Подбирает пути под ОДНИМ общим уникальным именем ('candidate'), так
    чтобы raw_wav / video_only / final_out для одной записи всегда
    совпадали по базовому имени (и не "разъезжались" на _01/_02, если,
    например, .wav с таким именем уже существует, а итоговый файл — ещё
    нет). Уникальность проверяется по ИТОГОВОМУ файлу — это то, что
    реально должно не перезаписаться."""
    base = sanitize_filename(out_name)
    final_suffix = get_container(container_id).extension if is_video else output_suffix_for_container(container_id)

    candidate = base
    counter = 0
    while (dest_dir / f"{candidate}{final_suffix}").exists():
        counter += 1
        candidate = f"{base}_{counter:02d}"

    raw_wav = dest_dir / f"{candidate}_capture.wav"
    final_out = dest_dir / f"{candidate}{final_suffix}"
    if is_video:
        video_only = dest_dir / f"{candidate}_video_only.mkv"
        return raw_wav, video_only, final_out
    return raw_wav, None, final_out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="https://www.youtube.com",
                     help="Ссылка на YouTube (видео или главная страница; конкретное видео "
                          "определяется автоматически по факту начала воспроизведения)")
    ap.add_argument("out_name", nargs="?", default=None,
                     help="Имя выходного файла. Если не указано — берётся из заголовка вкладки")
    ap.add_argument("-d", "--destination", default=".", help="Папка сохранения (по умолчанию текущая)")
    ap.add_argument("--profile", default=None, help="Путь к профилю Firefox")
    ap.add_argument("--firefox-binary", default=None, help="Путь к firefox.exe (если не задан — определяется автоматически)")
    ap.add_argument("--headless", action="store_true",
                     help="ВНИМАНИЕ: с headless видео-захват экрана (gdigrab) не "
                          "работает (нечего захватывать без реального окна) — используйте "
                          "только для audio-контейнеров.")
    ap.add_argument("--no-profile-copy", action="store_true")
    ap.add_argument("--no-firefox-cache", action="store_true",
                     help="Не использовать сохранённую с прошлого запуска копию профиля/путь "
                          "к firefox.exe — определить их заново и обновить кэш результатом")
    ap.add_argument("--reset-firefox-cache", action="store_true",
                     help="Стереть сохранённую копию профиля и путь к firefox.exe, затем выйти")
    ap.add_argument("--fps", type=int, default=60, help="Частота кадров захвата экрана (по умолчанию 60)")
    ap.add_argument(
        "--container", default=None,
        choices=["mp4", "mkv", "webm", "avi", "mov", "flv", "3gp", "mp3", "aac", "ogg", "flac"],
        help="Видео- или audio-контейнер (audio-контейнер = записать только звук, без видео). "
             "Если не задано — поведение как в Suno: WAV 96kHz/24bit без видео.",
    )
    ap.add_argument(
        "--audio-container", default=None, choices=["mp3", "aac", "ogg", "flac"],
        help="Контейнер ЗВУКА ВНУТРИ видео (независимо от --container для видео). "
             "Имеет смысл только вместе с видео-контейнером (--container mp4/mkv/webm/"
             "avi/mov/flv/3gp) — не все видео-контейнеры поддерживают все audio-"
             "контейнеры (см. format_options.VIDEO_AUDIO_COMPAT), несовместимая пара "
             "будет отклонена с понятным сообщением. Если не задано — звук в видео "
             "остаётся WAV 96kHz/24bit там, где контейнер это физически поддерживает "
             "(mp4/mkv/avi/mov); для webm/flv/3gp, чья спецификация в принципе не "
             "допускает сырой WAV/PCM, используется лучший практически доступный "
             "вариант (см. предупреждение в выводе).",
    )
    ap.add_argument("--capture-mode", default="process", choices=["process", "device"],
                     help="'process' (по умолчанию) — изолированный захват только звука Firefox "
                          "(WASAPI Process Loopback, нужна Windows 10 2004+/11). 'device' — аварийный "
                          "fallback без изоляции: пишет ВЕСЬ звук системы (используйте, только если "
                          "'process' не работает на вашей машине).")
    ap.add_argument("--list-audio-devices", action="store_true",
                     help="Диагностика звука: устройства Windows + проверка запуска захвата, затем выйти")
    ap.add_argument("--no-auto-ffmpeg", action="store_true",
                     help="Не скачивать сборку ffmpeg автоматически, даже если у системного "
                          "ffmpeg нет кодека, нужного для выбранного --container (libx264/"
                          "libvpx-vp9 для видео, libmp3lame/libvorbis/flac для аудио)")
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

    container = get_container(args.container)
    is_video = container is not None and container.kind == "video"
    if is_video and args.headless:
        print("ERROR: видео-запись несовместима с --headless (нечего захватывать "
              "захватом экрана — gdigrab).", file=sys.stderr)
        return 1

    if args.audio_container and not is_video:
        print(
            "ERROR: --audio-container имеет смысл только вместе с видео-"
            "контейнером (--container mp4/mkv/webm/avi/mov/flv/3gp) — для "
            "чисто аудио-записи используйте сам --container "
            "(mp3/aac/ogg/flac).",
            file=sys.stderr,
        )
        return 1
    if args.audio_container and is_video:
        compat = compatible_audio_choices(args.container)
        if args.audio_container not in compat:
            readable = ", ".join(compat) if compat else "(нет совместимых audio-контейнеров)"
            print(
                f"ERROR: audio-контейнер '{args.audio_container}' несовместим с "
                f"видео-контейнером '{args.container}'. Совместимые варианты для "
                f"{args.container}: {readable}.",
                file=sys.stderr,
            )
            return 1

    if is_video:
        # чтобы координаты страницы и размеры экрана мерились в одних физических пикселях
        ensure_dpi_aware()

    profile_path, firefox_binary, use_profile_copy = resolve_firefox_launch_plan(
        args.profile, args.firefox_binary, use_cache=not args.no_firefox_cache,
    )
    if args.no_profile_copy:
        use_profile_copy = False

    dest_dir = Path(args.destination).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = resolve_ffmpeg_bin_for_container(
        args.container, audio_container_id=args.audio_container,
        auto_download=not args.no_auto_ffmpeg,
    )
    print(f"Использую ffmpeg: {ffmpeg_bin}")
    print(f"Контейнер: {args.container or 'не выбран -> WAV 96kHz/24bit, без видео'}")
    if is_video:
        print(f"Аудио внутри видео: {args.audio_container or 'не выбран -> WAV 96kHz/24bit (где это поддерживает контейнер)'}")

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
            f"Захват звука изолирован: только звук firefox.exe (pid={session.firefox_pid}) "
            "и его дочерних процессов (WASAPI Process Loopback Capture)."
        )

        def capture_factory():
            # session — переменная main(): после перезапуска браузера здесь
            # автоматически используется PID НОВОГО firefox.exe.
            cap = ProcessLoopbackCapture(session.firefox_pid, include_tree=True)
            cap.start()
            return cap

    saved: list[Path] = []
    stop_event = threading.Event()
    capture_failures = 0        # подряд идущие сбои ЗАПУСКА захвата
    MAX_CAPTURE_FAILURES = 3
    fatal_capture_error = False

    MAX_BROWSER_RESTARTS = 5    # подряд перезапусков браузера без единого сохранённого видео
    restart_streak = 0
    MAX_CONSECUTIVE_ERRORS = 5  # подряд необъяснимых ошибок при живом браузере -> перезапуск
    consecutive_errors = 0
    fatal_browser_error = False
    session_broken: str | None = None   # причина, по которой браузер нужно перезапустить
    needs_open = True                   # нужно (заново) открыть стартовую страницу

    def restart_session(reason: str) -> bool:
        """Перезапускает Firefox после закрытия/падения (как в Windows-Suno). False — сдаёмся."""
        nonlocal session, restart_streak
        print(f"ВНИМАНИЕ: браузер недоступен ({reason}).", file=sys.stderr)
        print(session.describe_failure(), file=sys.stderr)
        if time.monotonic() - session.started_at > 600:
            restart_streak = 0   # прошлый запуск проработал долго — это не «цикл падений»
        restart_streak += 1
        if restart_streak > MAX_BROWSER_RESTARTS:
            print(
                f"ERROR: Firefox закрывается {MAX_BROWSER_RESTARTS} раз подряд без единого "
                "сохранённого файла — дальнейшие перезапуски бессмысленны. Проверьте отчёт выше "
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
                    print("Нажмите play на нужном видео. Скрипт работает непрерывно — Ctrl+C для остановки.")
                    needs_open = False

                final_out = record_and_save_video(
                    session, args, ffmpeg_bin, dest_dir, capture_factory, stop_event,
                )
                saved.append(final_out)
                capture_failures = 0
                consecutive_errors = 0
                restart_streak = 0
                if stop_event.is_set():
                    # Остановлено пользователем (Ctrl+C / кнопка "Стоп" в
                    # GUI) ВО ВРЕМЯ этой записи — она уже полностью
                    # доведена до конца (кодирование звука + сшивание с
                    # видео в выбранный контейнер выполнены как обычно,
                    # см. record_and_save_video), поэтому дальше просто
                    # завершаем сессию, а не ждём следующее видео.
                    print("\nОстановлено пользователем — запись сохранена, завершаю сессию.")
                    break
                if not session.is_alive():
                    session_broken = "Firefox закрылся во время записи"
                    continue
                print("Жду следующее видео (откройте и нажмите play)...")
            except KeyboardInterrupt:
                # Ctrl+C ДО начала воспроизведения (пока ждём автообнаружение
                # видео) — записывать ещё нечего, отменяем как раньше.
                raise
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
                    print(f"Похоже, пропал интернет ({exc}).", file=sys.stderr)
                    wait_for_internet()
                else:
                    print(f"ERROR при обработке видео: {exc}", file=sys.stderr)
                    time.sleep(2)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    consecutive_errors = 0
                    session_broken = f"{MAX_CONSECUTIVE_ERRORS} ошибок подряд, браузер не отвечает как надо"
                continue
    except KeyboardInterrupt:
        print("\nОстановлено пользователем (Ctrl+C).")
    finally:
        session.quit()

    if not saved:
        print("ERROR: не удалось сохранить ни одного файла.", file=sys.stderr)
        return 2 if fatal_capture_error else (3 if fatal_browser_error else 1)
    print(f"Всего сохранено: {len(saved)}")
    for p in saved:
        print(f"  - {p}")
    return 3 if fatal_browser_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
