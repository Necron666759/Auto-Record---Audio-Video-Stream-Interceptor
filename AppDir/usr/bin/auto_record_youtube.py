#!/usr/bin/env python3
"""
auto_record_youtube.py

Аналог auto_record_suno.py для YouTube: та же схема запуска второго
изолированного Firefox через geckodriver и та же изоляция звука
(FirefoxAudioIsolator), но добавлена запись ВИДЕО через захват экрана
(x11grab под X11, wf-recorder под Wayland — см. screen_capture.py),
синхронно с звуком, и кодирование в выбранный пользователем
видео-контейнер (mp4/mkv/webm/avi/mov/flv/3gp) или, если выбран
audio-контейнер — запись только звука (без видео вообще), как в Suno.

Специально НЕ дублирует код запуска Firefox/изоляции звука — импортирует
эти функции и классы напрямую из auto_record_suno.py (файл должен
лежать рядом). Это гарантирует, что оба бэкенда используют одну и ту же
проверенную логику для этой части.

--------------------------------------------------------------------
ВАЖНОЕ ОГРАНИЧЕНИЕ (прочитайте перед использованием):
--------------------------------------------------------------------
Запись видео сделана через захват экрана (x11grab под X11, wf-recorder
под Wayland), а не через перехват самого видеопотока YouTube. Это
значит: скрипт записывает то, что
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

Поддерживает и X11, и Wayland: под X11 (в т.ч. XWayland) захват идёт как
раньше через ffmpeg x11grab; под Wayland — через 'wf-recorder' (работает
без диалога портала на wlroots-композиторах: Sway, Hyprland, River,
Wayfire и т.п.; нужно установить его отдельно, см. screen_capture.py).
Бэкенд выбирается автоматически (по XDG_SESSION_TYPE/WAYLAND_DISPLAY) или
явно флагом --display-server {auto,x11,wayland}. На GNOME/KDE Wayland
(без wlroots) wf-recorder не работает — там либо запускайте с
--display-server x11 (обычно работает через XWayland), либо пишите
только звук.

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
import platform
import shutil
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
    FirefoxAudioIsolator,
    ensure_parec,
    get_default_sink_name,
    list_audio_devices,
    navigate_with_retry,
    wait_for_internet,
    _looks_like_network_error,
    sanitize_filename,
    start_silence_keepalive,
    resolve_ffmpeg_bin as resolve_ffmpeg_bin_audio,
    resolve_ffmpeg_bin_for_encoder,
    _ffmpeg_has_encoder,
    ensure_ffmpeg_with_mp3,
)
from format_options import (
    RAW_CAPTURE_RATE,
    RAW_CAPTURE_WAV_CODEC,
    FFMPEG_MUXER_NAME,
    compatible_audio_choices,
    get_container,
)
from encode_helpers import (
    parec_raw_capture_args,
    ffmpeg_raw_wav_args,
    encode_final_audio,
    output_suffix_for_container,
    resolve_video_mux_audio_args,
)
from screen_capture import start_video_capture


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
    даже если в кэше (~/.cache/auto_record_suno/ffmpeg/ffmpeg) уже лежала
    полная сборка с libx264/libvpx-vp9 — она просто никогда не
    проверялась и не использовалась для видео.

    Теперь видео-контейнеры используют ту же самую общую логику
    подбора/автозагрузки ffmpeg, что и аудио: сначала системный ffmpeg,
    затем (если разрешено auto_download) закешированная или заново
    скачанная статическая GPL-сборка ffmpeg с johnvansickle.com — она
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
        if auto_download and platform.system() == "Linux":
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
            f"которого нет в {ffmpeg_bin} (автозагрузка отключена или не на Linux)."
        )
    return ffmpeg_bin


# --------------------------------------------------------------------------
# Определение геометрии видео-элемента на экране (для x11grab)
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
        session_id = session.session_id
        import urllib.request as _u  # локальный импорт, чтобы не тянуть лишнее в основной код
        req_body = json.dumps({}).encode()
        req = _u.Request(f"{session.base_url}/session/{session_id}/window/maximize",
                          data=req_body, method="POST", headers={"Content-Type": "application/json"})
        _u.urlopen(req, timeout=10)
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
    # ffmpeg x11grab требует чётные размеры для большинства видеокодеков
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
    monitor_source: str,
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

    parec_proc = ffmpeg_audio_proc = ffmpeg_video_proc = None
    try:
        # --- аудио: сырой захват в 96kHz/24bit, без гейтинга пауз (см. шапку) ---
        parec_proc = subprocess.Popen(parec_raw_capture_args(monitor_source), stdout=subprocess.PIPE)
        ffmpeg_audio_proc = subprocess.Popen(ffmpeg_raw_wav_args(ffmpeg_bin, raw_wav), stdin=parec_proc.stdout)
        parec_proc.stdout.close()  # чтобы SIGPIPE корректно доходил при завершении ffmpeg

        # --- видео: захват экрана прямо в целевой видеокодек (если выбран
        # видео-контейнер) — бэкенд (x11grab / wf-recorder под Wayland)
        # выбирается автоматически или явно флагом --display-server ---
        if is_video:
            ffmpeg_video_proc = start_video_capture(
                ffmpeg_bin, container_id, geom, args.fps, final_video_only,
                display_server=getattr(args, "display_server", "auto"),
            )

        print("Запись идёт — жду события окончания видео (Ctrl+C для отмены)...")
        # ВАЖНО: Ctrl+C / "Стоп" из GUI (SIGINT) прилетает СЮДА, пока мы
        # ждём естественного конца видео. Раньше KeyboardInterrupt отсюда
        # просто пролетал насквозь через этот try/finally (finally
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
        for proc in (parec_proc, ffmpeg_video_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        if parec_proc is not None:
            try:
                parec_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                parec_proc.kill()
        if ffmpeg_audio_proc is not None:
            try:
                ffmpeg_audio_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ffmpeg_audio_proc.terminate()
                ffmpeg_audio_proc.wait(timeout=10)
        if ffmpeg_video_proc is not None:
            try:
                ffmpeg_video_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                ffmpeg_video_proc.kill()

    if not raw_wav.exists() or raw_wav.stat().st_size == 0:
        raise RuntimeError("Аудиофайл записи пуст или не создан.")

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
    ap.add_argument("--firefox-binary", default=None, help="Путь к бинарнику firefox-bin")
    ap.add_argument("--firefox-wait-timeout", type=float, default=60.0)
    ap.add_argument("--headless", action="store_true",
                     help="ВНИМАНИЕ: с headless видео-захват экрана (x11grab/wf-recorder) не "
                          "работает (нечего захватывать без реального окна) — используйте "
                          "только для audio-контейнеров.")
    ap.add_argument("--no-profile-copy", action="store_true")
    ap.add_argument("--no-firefox-cache", action="store_true",
                     help="Не использовать сохранённую с прошлого запуска копию профиля/путь "
                          "к firefox-bin — определить их заново (нужен запущенный основной "
                          "firefox-bin), и обновить кэш результатом")
    ap.add_argument("--reset-firefox-cache", action="store_true",
                     help="Стереть сохранённую копию профиля и путь к firefox-bin, затем выйти")
    ap.add_argument("--fps", type=int, default=60, help="Частота кадров захвата экрана (по умолчанию 60)")
    ap.add_argument(
        "--display-server", default="auto", choices=["auto", "x11", "wayland"],
        help="Какой бэкенд захвата экрана использовать для видео: 'x11' "
             "(ffmpeg x11grab, в т.ч. через XWayland), 'wayland' "
             "(wf-recorder, нужен для wlroots-композиторов), или 'auto' "
             "(определить по XDG_SESSION_TYPE/WAYLAND_DISPLAY, по умолчанию).",
    )
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
    ap.add_argument("--list-audio-devices", action="store_true")
    ap.add_argument("--no-auto-ffmpeg", action="store_true",
                     help="Не скачивать статическую сборку ffmpeg, даже если у системного "
                          "ffmpeg нет кодека, нужного для выбранного --container (libx264/"
                          "libvpx-vp9 для видео, libmp3lame/libvorbis/flac для аудио)")
    args = ap.parse_args()

    if args.list_audio_devices:
        list_audio_devices()
        return 0

    if args.reset_firefox_cache:
        clear_firefox_cache()
        print("Кэш профиля/бинарника Firefox очищен.")
        return 0

    container = get_container(args.container)
    is_video = container is not None and container.kind == "video"
    if is_video and args.headless:
        print("ERROR: видео-запись несовместима с --headless (нечего захватывать "
              "захватом экрана — ни x11grab, ни wf-recorder).", file=sys.stderr)
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

    profile_path, firefox_binary, pending_close_pid, use_profile_copy = resolve_firefox_launch_plan(
        args.profile, args.firefox_binary, wait_timeout=args.firefox_wait_timeout,
        use_cache=not args.no_firefox_cache,
    )
    if args.no_profile_copy:
        use_profile_copy = False

    dest_dir = Path(args.destination).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ensure_parec()
    ffmpeg_bin = resolve_ffmpeg_bin_for_container(
        args.container, audio_container_id=args.audio_container,
        auto_download=not args.no_auto_ffmpeg,
    )
    print(f"Использую ffmpeg: {ffmpeg_bin}")
    print(f"Контейнер: {args.container or 'не выбран -> WAV 96kHz/24bit, без видео'}")
    if is_video:
        print(f"Аудио внутри видео: {args.audio_container or 'не выбран -> WAV 96kHz/24bit (где это поддерживает контейнер)'}")

    print("Запускаю geckodriver + firefox-bin...")
    session = GeckoSession(
        profile_path, headless=args.headless, firefox_binary=firefox_binary,
        pending_close_pid=pending_close_pid, use_profile_copy=use_profile_copy,
    )

    print("Настраиваю изоляцию звука (запись только из этого Firefox)...")
    audio_isolator = FirefoxAudioIsolator(session.firefox_pid)
    try:
        monitor_source = audio_isolator.start()
    except Exception:
        session.quit()
        raise
    print(f"Захват звука изолирован: {monitor_source}")

    keepalive_proc = start_silence_keepalive(audio_isolator.null_sink_name)

    saved: list[Path] = []
    stop_event = threading.Event()
    try:
        navigate_with_retry(session, args.url)
        print(f"Открыл {args.url}.")
        print("Нажмите play на нужном видео. Скрипт работает непрерывно — Ctrl+C для остановки.")
        while True:
            try:
                final_out = record_and_save_video(
                    session, args, ffmpeg_bin, dest_dir, monitor_source, stop_event,
                )
                saved.append(final_out)
                if stop_event.is_set():
                    # Остановлено пользователем (Ctrl+C / кнопка "Стоп" в
                    # GUI) ВО ВРЕМЯ этой записи — она уже полностью
                    # доведена до конца (кодирование звука + сшивание с
                    # видео в выбранный контейнер выполнены как обычно,
                    # см. record_and_save_video), поэтому дальше просто
                    # завершаем сессию, а не ждём следующее видео.
                    print("\nОстановлено пользователем — запись сохранена, завершаю сессию.")
                    break
                print("Жду следующее видео (откройте и нажмите play)...")
            except KeyboardInterrupt:
                # Ctrl+C ДО начала воспроизведения (пока ждём автообнаружение
                # видео) — записывать ещё нечего, отменяем как раньше.
                raise
            except Exception as exc:
                if _looks_like_network_error(exc):
                    print(f"Похоже, пропал интернет ({exc}).", file=sys.stderr)
                    wait_for_internet()
                else:
                    print(f"ERROR при обработке видео: {exc}", file=sys.stderr)
                    time.sleep(2)
                continue
    except KeyboardInterrupt:
        print("\nОстановлено пользователем (Ctrl+C).")
    finally:
        session.quit()
        if keepalive_proc is not None:
            keepalive_proc.stop()
        audio_isolator.stop()

    if not saved:
        print("ERROR: не удалось сохранить ни одного файла.", file=sys.stderr)
        return 1
    print(f"Всего сохранено: {len(saved)}")
    for p in saved:
        print(f"  - {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
