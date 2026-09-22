#!/usr/bin/env python3
"""
i18n.py

Простая локализация GUI (recorder_gui.py). Поддерживаются русский ('ru')
и английский ('en') языки, переключаемые прямо в интерфейсе без
перезапуска программы.

Никакой магии: словарь "ключ -> текст" на каждый язык плюс функция
tr(lang, key, **kwargs) с подстановкой через str.format. Заголовок окна
(app_title) — это название программы, поэтому оно не переводится и
одинаково на обоих языках.
"""

from __future__ import annotations

LANGUAGES: dict[str, str] = {"ru": "Русский", "en": "English"}
DEFAULT_LANGUAGE = "ru"

APP_TITLE = "Auto Record - Audio/Video Stream Interceptor"

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "app_title": APP_TITLE,
        "language_label": "Язык:",

        "service_group": "Сервис",
        "service_suno": "Suno (аудио)",
        "service_youtube": "YouTube (видео/аудио)",

        "profile_group": "Профиль настроек",
        "profile_placeholder": "(без профиля)",
        "profile_save_btn": "Сохранить",
        "profile_update_btn": "Обновить",
        "profile_delete_btn": "Удалить",
        "profile_save_title": "Сохранить профиль",
        "profile_save_prompt": "Имя профиля:",
        "profile_saved_log": "Профиль «{name}» сохранён.\n",
        "profile_updated_log": "Профиль «{name}» обновлён.\n",
        "profile_deleted_log": "Профиль «{name}» удалён.\n",
        "profile_loaded_log": "Загружен профиль «{name}».\n",
        "profile_delete_confirm_title": "Удалить профиль",
        "profile_delete_confirm": "Удалить профиль «{name}»?",
        "profile_none_selected": "Сначала выберите профиль в списке.",
        "profile_empty_name": "Введите имя профиля.",

        "containers_group": "Во что конвертировать после записи "
                             "(ничего не выбрано = WAV 96kHz/24bit по умолчанию)",
        "group_audio": "Аудио",
        "group_video": "Видео",
        "container_default": "(не выбрано) -> WAV 96kHz/24bit",

        "url_label": "URL (необязательно):",
        "dest_label": "Папка сохранения:",
        "choose_btn": "Выбрать...",
        "display_server_label": "Захват экрана:",
        "display_server_auto": "Авто",
        "display_server_x11": "X11",
        "display_server_wayland": "Wayland",
        "video_audio_label": "Звук внутри видео:",
        "audio_container_default": "(не выбрано) -> WAV 96kHz/24bit, где контейнер это поддерживает",

        "start_btn": "▶ Запустить запись",
        "stop_btn": "■ Остановить",
        "check_audio_btn": "Проверить звуковые устройства",
        "reset_firefox_cache_btn": "Сбросить кэш профиля Firefox",
        "reset_firefox_cache_confirm_title": "Сбросить кэш профиля Firefox",
        "reset_firefox_cache_confirm": "Сохранённая копия профиля Firefox и путь к firefox-bin "
                                        "будут удалены. При следующем запуске записи снова "
                                        "потребуется, чтобы был открыт основной Firefox. Продолжить?",
        "reset_firefox_cache_done_log": "\n[кэш профиля Firefox сброшен]\n",
        "reset_firefox_cache_error": "Не удалось сбросить кэш профиля Firefox: {exc}\n",

        "logs_group": "Логи",

        "already_running_title": "Уже запущено",
        "already_running_msg": "Запись уже идёт.",
        "error_title": "Ошибка",
        "start_failed_title": "Не удалось запустить",
        "stopping_log": "\n[останавливаю — сигнал остановки процессу]\n",
        "process_ended_log": "\n[процесс завершился, код {code}]\n",
        "script_not_found": "Не найден {name} рядом с recorder_gui.py ({path}).",
        "audio_check_error": "Ошибка проверки звуковых устройств: {exc}\n",
    },
    "en": {
        "app_title": APP_TITLE,
        "language_label": "Language:",

        "service_group": "Service",
        "service_suno": "Suno (audio)",
        "service_youtube": "YouTube (video/audio)",

        "profile_group": "Settings profile",
        "profile_placeholder": "(no profile)",
        "profile_save_btn": "Save",
        "profile_update_btn": "Update",
        "profile_delete_btn": "Delete",
        "profile_save_title": "Save profile",
        "profile_save_prompt": "Profile name:",
        "profile_saved_log": "Profile \"{name}\" saved.\n",
        "profile_updated_log": "Profile \"{name}\" updated.\n",
        "profile_deleted_log": "Profile \"{name}\" deleted.\n",
        "profile_loaded_log": "Loaded profile \"{name}\".\n",
        "profile_delete_confirm_title": "Delete profile",
        "profile_delete_confirm": "Delete profile \"{name}\"?",
        "profile_none_selected": "Select a profile from the list first.",
        "profile_empty_name": "Enter a profile name.",

        "containers_group": "Convert to after recording "
                             "(nothing selected = WAV 96kHz/24bit by default)",
        "group_audio": "Audio",
        "group_video": "Video",
        "container_default": "(not selected) -> WAV 96kHz/24bit",

        "url_label": "URL (optional):",
        "dest_label": "Save folder:",
        "choose_btn": "Browse...",
        "display_server_label": "Screen capture:",
        "display_server_auto": "Auto",
        "display_server_x11": "X11",
        "display_server_wayland": "Wayland",
        "video_audio_label": "Audio inside video:",
        "audio_container_default": "(not selected) -> WAV 96kHz/24bit where the container supports it",

        "start_btn": "▶ Start recording",
        "stop_btn": "■ Stop",
        "check_audio_btn": "Check audio devices",
        "reset_firefox_cache_btn": "Reset Firefox profile cache",
        "reset_firefox_cache_confirm_title": "Reset Firefox profile cache",
        "reset_firefox_cache_confirm": "The saved Firefox profile copy and the remembered "
                                        "firefox-bin path will be deleted. The next recording "
                                        "will again require your main Firefox to be open. Continue?",
        "reset_firefox_cache_done_log": "\n[Firefox profile cache reset]\n",
        "reset_firefox_cache_error": "Failed to reset the Firefox profile cache: {exc}\n",

        "logs_group": "Logs",

        "already_running_title": "Already running",
        "already_running_msg": "Recording is already in progress.",
        "error_title": "Error",
        "start_failed_title": "Failed to start",
        "stopping_log": "\n[stopping - sending stop signal to the process]\n",
        "process_ended_log": "\n[process ended, exit code {code}]\n",
        "script_not_found": "{name} not found next to recorder_gui.py ({path}).",
        "audio_check_error": "Audio device check failed: {exc}\n",
    },
}

# Локализованные подписи для блоков-контейнеров из format_options.py.
# Технические (формат-нейтральные) подписи вроде "WAV -> MP3" одинаковы на
# обоих языках и не нуждаются в переопределении — только "Запись -> ..."
# для видео-контейнеров переведено отдельно.
_CONTAINER_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "mp4": "Recording -> MP4",
        "mkv": "Recording -> MKV",
        "webm": "Recording -> WebM",
        "avi": "Recording -> AVI",
        "mov": "Recording -> MOV",
        "flv": "Recording -> FLV",
        "3gp": "Recording -> 3GP",
    },
}


def tr(lang: str, key: str, **kwargs) -> str:
    table = _TRANSLATIONS.get(lang, _TRANSLATIONS[DEFAULT_LANGUAGE])
    text = table.get(key, _TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key))
    return text.format(**kwargs) if kwargs else text


def container_label(lang: str, container_id: str, default_label: str) -> str:
    return _CONTAINER_LABELS.get(lang, {}).get(container_id, default_label)
