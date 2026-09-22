#!/usr/bin/env python3
"""
recorder_gui.py

Блочный графический интерфейс поверх auto_record_suno.py /
auto_record_youtube.py — (сетка цветных прямоугольных кнопок-контейнеров в одном окне) + под
основным меню лог-консоль, дублирующая то, что скрипт печатает в
терминал при обычном запуске без GUI.

Запуск:
    python3 recorder_gui.py

Зависимости: только стандартная библиотека (tkinter). Если tkinter не
установлен: sudo apt install python3-tk

ВАЖНО: этот файл сам ничего не записывает — он строит команду
(auto_record_suno.py / auto_record_youtube.py с нужными флагами) и
запускает её отдельным процессом, построчно перекачивая его stdout/stderr
в лог-консоль в реальном времени. Вся логика записи/кодирования остаётся
в тех двух скриптах — GUI только собирает выбор пользователя в CLI-флаги.

Дополнительные возможности этой версии:
  - Локализация интерфейса (русский/английский), переключается прямо
    в шапке окна без перезапуска — см. i18n.py.
  - Профили настроек (сервис, контейнер, папка, URL, захват экрана,
    язык), которые можно сохранять/загружать/удалять через GUI —
    см. gui_profiles.py.
  - Разделы "Во что конвертировать" (аудио/видео) и "Логи" регулируются
    по высоте пользователем (тянущийся разделитель между ними); раздел
    выбора сервиса высоту не меняет.
  - При подъёме раздела "Логи" (и, соответственно, уменьшении высоты,
    оставленной под "Аудио/Видео") блоки-контейнеры не обрезаются и не
    пропадают из виду: сетка автоматически добавляет колонки вправо,
    заполняя освободившуюся по горизонтали пустоту, и уменьшает число
    рядов — вплоть до ограничения по ширине/количеству колонок; при
    возврате высоты сетка так же автоматически возвращается к исходному
    виду. См. _adjust_container_columns / _on_container_area_configure.
  - Выбор бэкенда захвата экрана для видео (Авто/X11/Wayland).
"""

from __future__ import annotations

import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import gui_profiles
from format_options import containers_for_service, compatible_audio_choices, get_container
from i18n import DEFAULT_LANGUAGE, LANGUAGES, container_label, tr

SCRIPT_DIR = Path(__file__).resolve().parent
SUNO_SCRIPT = SCRIPT_DIR / "auto_record_suno.py"
YOUTUBE_SCRIPT = SCRIPT_DIR / "auto_record_youtube.py"

BG = "#e9e7e0"          # фон окна, под стиль скриншота (светло-бежевый)
LOG_BG = "#101418"
LOG_FG = "#c9d1d9"

DISPLAY_SERVER_VALUES = ("auto", "x11", "wayland")


class RecorderGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.configure(bg=BG)
        self.minsize(760, 640)

        self.lang_var = tk.StringVar(value=DEFAULT_LANGUAGE)
        self.service_var = tk.StringVar(value="suno")
        self.container_var = tk.StringVar(value="")   # '' = не выбран -> WAV по умолчанию
        # Контейнер ЗВУКА внутри видео (независимо от container_var,
        # который для видео задаёт контейнер видео) — '' = не выбран ->
        # WAV 96kHz/24bit (там, где выбранный видео-контейнер это
        # физически поддерживает, см. format_options.VIDEO_PCM_CAPABLE).
        self.audio_container_var = tk.StringVar(value="")
        self.dest_var = tk.StringVar(value=str(Path.home()))
        self.url_var = tk.StringVar(value="")
        self.display_server_var = tk.StringVar(value="auto")
        self.profile_var = tk.StringVar(value="")

        self.proc: subprocess.Popen | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.container_buttons: dict[str, tk.Button] = {}

        # --- авто-перестройка сетки блоков "Аудио/Видео" при подъёме
        # раздела "Логи" (см. _on_container_area_configure /
        # _adjust_container_columns): вместо того, чтобы блоки обрезались
        # и пропадали из виду, когда пользователь тянет разделитель вверх,
        # сетка сама уменьшает число рядов, добавляя колонки вправо, пока
        # всё содержимое не уместится по высоте — и наоборот, возвращается
        # к исходному виду, как только высота снова позволяет.
        self._container_min_cols = 2      # исходное/минимальное число колонок
        self._container_max_cols = 5      # "определённый момент" — дальше не растим
        self._container_col_count = self._container_min_cols
        self._container_resize_job: str | None = None
        # Скрытый (никогда не .pack()/.grid() наружу) вспомогательный
        # фрейм — на нём меряем, сколько места займёт сетка при разном
        # числе колонок, НЕ трогая реальные видимые кнопки. Это устраняет
        # мигание, которое было бы при переборе вариантов на самом
        # container_frame (destroy/create по несколько раз подряд на глазах
        # у пользователя).
        self._measure_frame: tk.Frame | None = None

        self._build_layout()
        self._poll_log_queue()

        # Запуск программы должен применять последний СОХРАНЁННЫЙ профиль
        # (сервис/контейнер/папку/URL/бэкенд/язык/размер окна). Если
        # сохранённых профилей ещё вообще не было — подбираем компактный
        # стартовый размер окна, чтобы его низ не прятался под нижнюю
        # панель окружения (см. _apply_initial_geometry).
        self._apply_startup_profile_or_initial_geometry()

        # Закрытие окна через "крестик" должно вести себя так же, как кнопка
        # "Стоп" — если запись/бэкенд ещё работает, сперва корректно его
        # останавливаем (см. _stop/_on_close_window), чтобы Firefox не
        # оставался висеть, и только потом реально закрываем окно.
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

    # ------------------------------------------------------------------
    # Локализация
    # ------------------------------------------------------------------

    def _t(self, key: str, **kwargs) -> str:
        return tr(self.lang_var.get(), key, **kwargs)

    def _on_language_changed(self) -> None:
        # Проще и надёжнее всего просто перестроить весь интерфейс —
        # переменные (StringVar) не уничтожаются, так что выбор
        # пользователя (сервис/контейнер/папка/URL и т.п.) сохраняется.
        for child in self.winfo_children():
            child.destroy()
        self._build_layout()

    # ------------------------------------------------------------------
    # UI построение
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        self.title(tr(self.lang_var.get(), "app_title"))

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(header, text=tr(self.lang_var.get(), "app_title"), bg=BG,
                 font=("Sans", 14, "bold")).pack(side="left")

        lang_frame = tk.Frame(header, bg=BG)
        lang_frame.pack(side="right")
        tk.Label(lang_frame, text=self._t("language_label"), bg=BG).pack(side="left", padx=(0, 6))
        lang_box = ttk.Combobox(
            lang_frame, state="readonly", width=10,
            values=[LANGUAGES[code] for code in LANGUAGES],
        )
        lang_box.set(LANGUAGES[self.lang_var.get()])
        lang_box.pack(side="left")
        lang_box.bind("<<ComboboxSelected>>", lambda e: self._set_language(lang_box.get()))

        # --- выбор сервиса (высота фиксирована, не регулируется) ---
        service_frame = tk.LabelFrame(self, text=self._t("service_group"), bg=BG, padx=10, pady=8)
        service_frame.pack(fill="x", padx=14, pady=6)
        for value, key in (("suno", "service_suno"), ("youtube", "service_youtube")):
            tk.Radiobutton(
                service_frame, text=self._t(key), variable=self.service_var, value=value,
                bg=BG, font=("Sans", 10), command=self._on_service_changed,
            ).pack(side="left", padx=10)

        # --- профили настроек ---
        self._build_profile_bar()

        # --- источник / папка сохранения / бэкенд захвата экрана ---
        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=14, pady=6)

        tk.Label(opts, text=self._t("url_label"), bg=BG).grid(row=0, column=0, sticky="w")
        tk.Entry(opts, textvariable=self.url_var, width=50).grid(row=0, column=1, sticky="we", padx=6)

        tk.Label(opts, text=self._t("dest_label"), bg=BG).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(opts, textvariable=self.dest_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        tk.Button(opts, text=self._t("choose_btn"), command=self._choose_dest).grid(
            row=1, column=2, padx=4, pady=(6, 0))

        tk.Label(opts, text=self._t("display_server_label"), bg=BG).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ds_frame = tk.Frame(opts, bg=BG)
        ds_frame.grid(row=2, column=1, sticky="w", pady=(6, 0))
        for value, key in (("auto", "display_server_auto"), ("x11", "display_server_x11"),
                            ("wayland", "display_server_wayland")):
            tk.Radiobutton(
                ds_frame, text=self._t(key), variable=self.display_server_var, value=value,
                bg=BG, font=("Sans", 9),
            ).pack(side="left", padx=(0, 10))

        # --- контейнер ЗВУКА внутри видео (отдельно от контейнера видео) ---
        # Активен и заполнен только когда выбран видео-контейнер (см.
        # _refresh_audio_container_choices) — набор вариантов зависит от
        # того, какие audio-контейнеры вообще совместимы с выбранным
        # видео-контейнером (format_options.compatible_audio_choices).
        tk.Label(opts, text=self._t("video_audio_label"), bg=BG).grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.audio_container_box = ttk.Combobox(opts, state="disabled", width=30)
        self.audio_container_box.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        self.audio_container_box.bind("<<ComboboxSelected>>", lambda e: self._on_audio_container_selected())

        opts.columnconfigure(1, weight=1)

        # --- кнопки старт/стоп + диагностика звука ---
        controls = tk.Frame(self, bg=BG)
        controls.pack(fill="x", padx=14, pady=8)
        self.start_btn = tk.Button(controls, text=self._t("start_btn"), bg="#3f8f4f", fg="white",
                                    font=("Sans", 11, "bold"), command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(controls, text=self._t("stop_btn"), bg="#a03f3f", fg="white",
                                   font=("Sans", 11, "bold"), command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        tk.Button(controls, text=self._t("check_audio_btn"),
                  command=self._check_audio_devices).pack(side="left", padx=8)
        tk.Button(controls, text=self._t("reset_firefox_cache_btn"),
                  command=self._reset_firefox_cache).pack(side="left", padx=8)

        # --- регулируемые по высоте разделы: контейнеры (аудио/видео) и логи ---
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        self.paned = paned  # нужно для расчёта компактного стартового размера окна

        self.container_frame = tk.LabelFrame(paned, text=self._t("containers_group"), bg=BG, padx=10, pady=10)
        paned.add(self.container_frame, weight=1)
        self._render_container_grid()
        # Пересчитываем число колонок при каждом изменении высоты этого
        # раздела — в т.ч. при перетаскивании разделителя PanedWindow,
        # который поднимает/опускает раздел "Логи" ниже.
        self.container_frame.bind("<Configure>", self._on_container_area_configure)

        log_frame = tk.LabelFrame(paned, text=self._t("logs_group"), bg=BG, padx=6, pady=6)
        paned.add(log_frame, weight=2)
        self.log_text = tk.Text(log_frame, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                                 font=("Monospace", 10), wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Лог накапливается между перестроениями интерфейса (напр. при
        # смене языка) — восстанавливаем его содержимое в новый виджет.
        if getattr(self, "_log_backlog", None):
            self._append_log(self._log_backlog)

    def _set_language(self, display_name: str) -> None:
        for code, name in LANGUAGES.items():
            if name == display_name:
                if code != self.lang_var.get():
                    self.lang_var.set(code)
                    self._on_language_changed()
                return

    # ------------------------------------------------------------------
    # Профили настроек
    # ------------------------------------------------------------------

    def _build_profile_bar(self) -> None:
        profile_frame = tk.LabelFrame(self, text=self._t("profile_group"), bg=BG, padx=10, pady=6)
        profile_frame.pack(fill="x", padx=14, pady=6)

        self.profile_box = ttk.Combobox(profile_frame, state="readonly", width=28)
        self.profile_box.pack(side="left", padx=(0, 8))
        self._refresh_profile_list()
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self._load_selected_profile())

        tk.Button(profile_frame, text=self._t("profile_save_btn"),
                  command=self._save_profile_as).pack(side="left", padx=4)
        tk.Button(profile_frame, text=self._t("profile_update_btn"),
                  command=self._update_selected_profile).pack(side="left", padx=4)
        tk.Button(profile_frame, text=self._t("profile_delete_btn"),
                  command=self._delete_selected_profile).pack(side="left", padx=4)

    def _refresh_profile_list(self) -> None:
        names = gui_profiles.list_profiles()
        values = [self._t("profile_placeholder")] + names
        self.profile_box["values"] = values
        current = self.profile_var.get()
        self.profile_box.set(current if current in names else values[0])

    def _collect_profile_data(self) -> dict:
        return {
            "service": self.service_var.get(),
            "container": self.container_var.get(),
            "audio_container": self.audio_container_var.get(),
            "dest": self.dest_var.get(),
            "url": self.url_var.get(),
            "display_server": self.display_server_var.get(),
            "language": self.lang_var.get(),
            # Размер окна на момент сохранения — восстанавливается при
            # загрузке этого профиля (в т.ч. автоматически при следующем
            # запуске программы, см. _apply_startup_profile_or_initial_geometry).
            "window_width": self.winfo_width(),
            "window_height": self.winfo_height(),
        }

    def _apply_profile_data(self, data: dict) -> None:
        self.service_var.set(data.get("service", self.service_var.get()))
        self.dest_var.set(data.get("dest", self.dest_var.get()))
        self.url_var.set(data.get("url", self.url_var.get()))
        self.display_server_var.set(data.get("display_server", "auto"))
        self._render_container_grid()
        self.container_var.set(data.get("container", ""))
        self._highlight_selected()
        # audio_container восстанавливается ПОСЛЕ container — _refresh_
        # audio_container_choices() (вызывается из _highlight_selected)
        # уже перестроила список вариантов под восстановленный видео-
        # контейнер, так что сохранённое значение будет корректно найдено
        # в списке совместимых, если оно всё ещё совместимо.
        self.audio_container_var.set(data.get("audio_container", ""))
        self._refresh_audio_container_choices()
        width = data.get("window_width")
        height = data.get("window_height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            self.geometry(f"{width}x{height}")

    def _save_profile_as(self) -> None:
        name = simpledialog.askstring(self._t("profile_save_title"), self._t("profile_save_prompt"), parent=self)
        if not name or not name.strip():
            return
        self._write_profile(name.strip())

    def _update_selected_profile(self) -> None:
        name = self.profile_box.get()
        if not name or name == self._t("profile_placeholder"):
            messagebox.showinfo(self._t("profile_group"), self._t("profile_none_selected"))
            return
        self._write_profile(name)

    def _write_profile(self, name: str) -> None:
        try:
            gui_profiles.save_profile(name, self._collect_profile_data())
        except ValueError:
            messagebox.showerror(self._t("error_title"), self._t("profile_empty_name"))
            return
        # Именно "последний СОХРАНЁННЫЙ" профиль — используется при
        # следующем запуске программы (см. _apply_startup_profile_or_initial_geometry).
        gui_profiles.set_last_profile(name)
        self.profile_var.set(name)
        self._refresh_profile_list()
        self.profile_box.set(name)
        self._append_log(self._t("profile_saved_log", name=name))

    def _load_selected_profile(self) -> None:
        name = self.profile_box.get()
        if not name or name == self._t("profile_placeholder"):
            return
        try:
            data = gui_profiles.load_profile(name)
        except (OSError, ValueError):
            messagebox.showerror(self._t("error_title"), self._t("profile_none_selected"))
            return
        self.profile_var.set(name)
        loaded_language = data.get("language")
        self._apply_profile_data(data)
        self._append_log(self._t("profile_loaded_log", name=name))
        if loaded_language and loaded_language != self.lang_var.get() and loaded_language in LANGUAGES:
            self.lang_var.set(loaded_language)
            self._on_language_changed()

    def _apply_startup_profile_or_initial_geometry(self) -> None:
        """Вызывается один раз при запуске программы. Если есть последний
        СОХРАНЁННЫЙ (Save/Update) профиль — применяем его целиком, включая
        сохранённый в нём размер окна. Если сохранённых профилей нет
        вообще (самый первый запуск) — подбираем компактный стартовый
        размер окна вместо "естественного" (см. _apply_initial_geometry)."""
        last_name = gui_profiles.get_last_profile()
        if last_name and last_name in gui_profiles.list_profiles():
            try:
                data = gui_profiles.load_profile(last_name)
            except (OSError, ValueError):
                data = None
            if data is not None:
                self.profile_var.set(last_name)
                self._refresh_profile_list()
                loaded_language = data.get("language")
                self._apply_profile_data(data)
                self._append_log(self._t("profile_loaded_log", name=last_name))
                if loaded_language and loaded_language != self.lang_var.get() and loaded_language in LANGUAGES:
                    self.lang_var.set(loaded_language)
                    self._on_language_changed()
                return
        self._apply_initial_geometry()

    def _apply_initial_geometry(self) -> None:
        """Подбирает компактный стартовый размер окна для самого первого
        запуска (когда ещё нет ни одного сохранённого профиля, значит и
        восстанавливать нечего). "Естественный" размер окна (все блоки
        Аудио/Видео в 2 колонки + полная высота логов) на многих экранах
        превышает высоту рабочей области, и низ окна (кнопки Старт/Стоп и
        т.п.) прячется под нижнюю панель окружения (например, XFCE).
        Поэтому гарантируем видимость органов управления НАД
        разделяемым по высоте разделом "Аудио/Видео"/"Логи" целиком, а
        сам этот раздел (пользователь может растянуть его перетаскиванием
        разделителя в любой момент, и он уже умеет сам перестраивать
        сетку контейнеров под доступную высоту — см.
        _adjust_container_columns) на первом запуске может быть невысоким."""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        # Запас на панель окружения (например, нижнюю панель XFCE) и
        # рамку/заголовок окна — winfo_screenheight() их не учитывает.
        margin = 80
        available_h = max(400, screen_h - margin)
        available_w = max(700, screen_w - 60)

        natural_h = self.winfo_reqheight()
        natural_w = self.winfo_reqwidth()

        # Высота всего, что идёт ДО разделяемого раздела (шапка, выбор
        # сервиса, профили, URL/папка/бэкенд, кнопки Старт/Стоп) — тот
        # самый минимум, который обязан остаться виден целиком.
        fixed_h = self.paned.winfo_y() if hasattr(self, "paned") else natural_h
        min_paned_visible = 160  # немного видимой части раздела Аудио/Видео+Логи

        target_h = min(natural_h, available_h)
        target_h = max(target_h, min(fixed_h + min_paned_visible, available_h))
        target_w = min(natural_w, available_w)

        self.geometry(f"{int(target_w)}x{int(target_h)}")
        self.minsize(700, min(640, available_h))

    def _delete_selected_profile(self) -> None:
        name = self.profile_box.get()
        if not name or name == self._t("profile_placeholder"):
            messagebox.showinfo(self._t("profile_group"), self._t("profile_none_selected"))
            return
        if not messagebox.askyesno(self._t("profile_delete_confirm_title"),
                                    self._t("profile_delete_confirm", name=name)):
            return
        gui_profiles.delete_profile(name)
        self.profile_var.set("")
        self._refresh_profile_list()
        self._append_log(self._t("profile_deleted_log", name=name))

    # ------------------------------------------------------------------
    # Блочное меню контейнеров
    # ------------------------------------------------------------------

    def _render_container_grid(self, col_count: int | None = None) -> None:
        for child in self.container_frame.winfo_children():
            child.destroy()
        self.container_buttons.clear()
        selected_before = self.container_var.get()
        self.container_var.set("")

        service = self.service_var.get()
        groups = containers_for_service(service)
        lang = self.lang_var.get()

        # Число колонок: явное значение (используется при авто-перестройке
        # из-за нехватки высоты) или последнее применённое/исходное.
        col_count = col_count if col_count is not None else self._container_col_count
        self._container_col_count = col_count
        row = 0
        for kind_label_key, options in (("group_audio", groups["audio"]), ("group_video", groups["video"])):
            if not options:
                continue
            tk.Label(self.container_frame, text=self._t(kind_label_key), bg=BG, font=("Sans", 9, "bold")) \
                .grid(row=row, column=0, columnspan=col_count, sticky="w", pady=(4, 2))
            row += 1
            col = 0
            for opt in options:
                label = container_label(lang, opt.id, opt.label)
                btn = tk.Button(
                    self.container_frame, text=label, bg=opt.color, fg="white",
                    activebackground=opt.color, font=("Sans", 10, "bold"),
                    relief="raised", bd=2, width=22, height=2,
                    command=lambda cid=opt.id: self._select_container(cid),
                )
                btn.grid(row=row, column=col, padx=6, pady=4, sticky="we")
                self.container_buttons[opt.id] = btn
                col += 1
                if col >= col_count:
                    col = 0
                    row += 1
            if col != 0:
                row += 1

        # Кнопка "ничего не выбирать" — явно показывает дефолтное поведение
        default_btn = tk.Button(
            self.container_frame, text=self._t("container_default"), bg="#555555", fg="white",
            font=("Sans", 9, "italic"), relief="sunken", bd=2, width=46, height=1,
            command=lambda: self._select_container(""),
        )
        default_btn.grid(row=row + 1, column=0, columnspan=col_count, pady=(8, 0), sticky="we")
        self.container_buttons[""] = default_btn

        # При смене сервиса раньше выбранный контейнер мог не относиться
        # к новому сервису — сохраняем выбор, только если он всё ещё валиден.
        if selected_before in self.container_buttons:
            self.container_var.set(selected_before)
        self._highlight_selected()

    def _select_container(self, container_id: str) -> None:
        self.container_var.set(container_id)
        self._highlight_selected()

    def _refresh_audio_container_choices(self) -> None:
        """Перестраивает список вариантов в audio_container_box под ТЕКУЩИЙ
        выбор видео-контейнера (container_var). Если выбран не видео-, а
        audio-контейнер (или вообще ничего не выбрано) — блок неактивен:
        выбор звука внутри видео просто не имеет смысла вне видеозаписи."""
        cid = self.container_var.get()
        container = get_container(cid) if cid else None
        is_video = container is not None and container.kind == "video"
        default_label = self._t("audio_container_default")

        if not is_video:
            self.audio_container_var.set("")
            self.audio_container_box.configure(state="readonly")
            self.audio_container_box["values"] = [default_label]
            self.audio_container_box.set(default_label)
            self.audio_container_box.configure(state="disabled")
            return

        lang = self.lang_var.get()
        compat = compatible_audio_choices(cid)
        labels = [default_label] + [
            container_label(lang, aid, get_container(aid).label) for aid in compat
        ]
        self.audio_container_box.configure(state="readonly")
        self.audio_container_box["values"] = labels

        current = self.audio_container_var.get()
        if current and current in compat:
            self.audio_container_box.current(compat.index(current) + 1)
        else:
            self.audio_container_var.set("")
            self.audio_container_box.current(0)

    def _on_audio_container_selected(self) -> None:
        idx = self.audio_container_box.current()
        cid = self.container_var.get()
        compat = compatible_audio_choices(cid) if cid else ()
        if idx <= 0 or idx - 1 >= len(compat):
            self.audio_container_var.set("")
        else:
            self.audio_container_var.set(compat[idx - 1])

    def _highlight_selected(self) -> None:
        selected = self.container_var.get()
        for cid, btn in self.container_buttons.items():
            btn.configure(relief="sunken" if cid == selected else "raised",
                          bd=4 if cid == selected else 2)
        self._refresh_audio_container_choices()

    def _on_service_changed(self) -> None:
        self._render_container_grid()

    # ------------------------------------------------------------------
    # Авто-перестройка сетки контейнеров при подъёме раздела "Логи"
    # ------------------------------------------------------------------

    def _on_container_area_configure(self, event=None) -> None:
        # Событий <Configure> во время перетаскивания разделителя приходит
        # много подряд — откладываем реальный пересчёт до момента, когда
        # размер на короткое время "устаканился", чтобы не пересобирать
        # сетку на каждый промежуточный пиксель перетаскивания.
        if self._container_resize_job is not None:
            try:
                self.after_cancel(self._container_resize_job)
            except (ValueError, tk.TclError):
                pass
        self._container_resize_job = self.after(60, self._adjust_container_columns)

    def _measure_container_height(self, col_count: int) -> int:
        """Строит сетку контейнеров с данным числом колонок на скрытом
        self._measure_frame (никогда не показывается на экране — не
        упакован ни в один geometry manager) и возвращает требуемую по
        содержимому высоту. Используется только для подбора col_count —
        реальные видимые кнопки при этом не трогаются, поэтому подбор не
        вызывает мигания."""
        if self._measure_frame is None or not self._measure_frame.winfo_exists():
            self._measure_frame = tk.Frame(self)
        probe = self._measure_frame
        for child in probe.winfo_children():
            child.destroy()

        groups = containers_for_service(self.service_var.get())
        lang = self.lang_var.get()

        row = 0
        for kind_label_key, options in (("group_audio", groups["audio"]), ("group_video", groups["video"])):
            if not options:
                continue
            tk.Label(probe, text=self._t(kind_label_key), font=("Sans", 9, "bold")) \
                .grid(row=row, column=0, columnspan=col_count, sticky="w", pady=(4, 2))
            row += 1
            col = 0
            for opt in options:
                label = container_label(lang, opt.id, opt.label)
                tk.Button(probe, text=label, font=("Sans", 10, "bold"),
                          relief="raised", bd=2, width=22, height=2) \
                    .grid(row=row, column=col, padx=6, pady=4, sticky="we")
                col += 1
                if col >= col_count:
                    col = 0
                    row += 1
            if col != 0:
                row += 1
        tk.Button(probe, text=self._t("container_default"), font=("Sans", 9, "italic"),
                  relief="sunken", bd=2, width=46, height=1) \
            .grid(row=row + 1, column=0, columnspan=col_count, pady=(8, 0), sticky="we")

        probe.update_idletasks()
        height = probe.winfo_reqheight()
        for child in probe.winfo_children():
            child.destroy()
        return height

    def _adjust_container_columns(self) -> None:
        self._container_resize_job = None
        if not self.container_frame.winfo_exists():
            return

        available_h = self.container_frame.winfo_height()
        available_w = self.container_frame.winfo_width()
        if available_h <= 1 or available_w <= 1:
            return

        # Верхняя граница числа колонок — не только "по вкусу"
        # (self._container_max_cols), но и по ширине раздела: не даём
        # блокам сжиматься практически до нечитаемой ширины.
        min_button_px = 150
        max_cols_by_width = max(self._container_min_cols, available_w // min_button_px)
        max_cols = min(self._container_max_cols, max_cols_by_width)

        # "Хром" — то, что LabelFrame добавляет к высоте сверх самого
        # содержимого (рамка, заголовок, внутренние padx/pady=10). Он
        # одинаков независимо от col_count, поэтому меряем его один раз
        # по текущему реальному состоянию и дальше просто прибавляем к
        # высоте, посчитанной на скрытом фрейме для каждого кандидата.
        self.container_frame.update_idletasks()
        current_content_h = self._measure_container_height(self._container_col_count)
        chrome = max(0, self.container_frame.winfo_reqheight() - current_content_h)

        # Ищем МИНИМАЛЬНОЕ число колонок (начиная с исходного), при
        # котором содержимое раздела помещается по высоте целиком — блоки
        # не обрезаются и не пропадают из виду, а перестраиваются вправо,
        # заполняя освободившуюся по горизонтали пустоту. Как только места
        # по вертикали снова достаточно, сетка сама возвращается к
        # исходному (минимальному) числу колонок.
        # Если ни один вариант не уместится (высота раздела уменьшена
        # экстремально сильно), просто останемся на max_cols — это и есть
        # "определённый момент", дальше которого мы блоки не перестраиваем
        # (дальше уже неизбежна обрезка, как и раньше).
        chosen = max_cols
        for cols in range(self._container_min_cols, max_cols + 1):
            if chrome + self._measure_container_height(cols) <= available_h:
                chosen = cols
                break

        # Реальные видимые кнопки перестраиваем только если число колонок
        # действительно изменилось, и только один раз — это и убирает
        # мигание (раньше здесь был перебор кандидатов на самих видимых
        # кнопках).
        if chosen != self._container_col_count:
            self._render_container_grid(col_count=chosen)

    def _choose_dest(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()))
        if chosen:
            self.dest_var.set(chosen)

    # ------------------------------------------------------------------
    # Запуск/остановка бэкенда
    # ------------------------------------------------------------------

    def _backend_script(self) -> Path:
        return SUNO_SCRIPT if self.service_var.get() == "suno" else YOUTUBE_SCRIPT

    def _build_command(self) -> list[str]:
        script = self._backend_script()
        if not script.exists():
            raise FileNotFoundError(self._t("script_not_found", name=script.name, path=str(script)))
        cmd = [sys.executable, str(script)]
        if self.url_var.get().strip():
            cmd.append(self.url_var.get().strip())
        cmd += ["-d", self.dest_var.get().strip() or "."]
        if self.container_var.get():
            cmd += ["--container", self.container_var.get()]
        # --audio-container имеет смысл только для видео-контейнера
        # youtube-бэкенда — combobox и так неактивен/сброшен для audio-
        # контейнеров и suno (см. _refresh_audio_container_choices), но
        # проверяем явно на всякий случай, чтобы не передать бэкенду
        # бессмысленный флаг.
        if self.service_var.get() == "youtube" and self.audio_container_var.get():
            cmd += ["--audio-container", self.audio_container_var.get()]
        # --display-server понимает только youtube-бэкенд (захват видео с
        # экрана); suno-бэкенд пишет только звук, ему это не нужно.
        if self.service_var.get() == "youtube" and self.display_server_var.get() != "auto":
            cmd += ["--display-server", self.display_server_var.get()]
        return cmd

    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showinfo(self._t("already_running_title"), self._t("already_running_msg"))
            return
        try:
            cmd = self._build_command()
        except FileNotFoundError as exc:
            messagebox.showerror(self._t("error_title"), str(exc))
            return

        self._append_log(f"$ {' '.join(cmd)}\n")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            messagebox.showerror(self._t("start_failed_title"), str(exc))
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._reader_thread, args=(self.proc,), daemon=True).start()

    def _reader_thread(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(line)
        proc.wait()
        self.log_queue.put(self._t("process_ended_log", code=proc.returncode))
        self.log_queue.put("__PROCESS_ENDED__")

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._append_log(self._t("stopping_log"))
            # ВАЖНО: не proc.terminate() (SIGTERM) — оба бэкенда закрывают
            # Firefox (session.quit() в geckodriver-сессии) только внутри
            # своего `finally`, который выполняется при перехвате
            # KeyboardInterrupt, т.е. по SIGINT. SIGTERM в Python валит
            # процесс мгновенно, минуя этот `finally`, — из-за этого после
            # "Стоп" в GUI Firefox оставался открытым. Поэтому сперва шлём
            # SIGINT (то же самое, что Ctrl+C в терминале), давая скрипту
            # самому корректно закрыть браузер; если он не уложится в
            # разумное время — принудительно добиваем процесс отдельным
            # потоком, чтобы не подвешивать GUI.
            try:
                self.proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
            threading.Thread(target=self._force_stop_if_needed, args=(self.proc,), daemon=True).start()
        self.stop_btn.configure(state="disabled")

    def _force_stop_if_needed(self, proc: subprocess.Popen, grace_period: float = 20.0) -> None:
        """Ждём в фоновом потоке, пока процесс сам корректно завершится
        после SIGINT (закрыв Firefox через session.quit()); если не
        уложился в grace_period — принудительно добиваем."""
        try:
            proc.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _on_close_window(self) -> None:
        """Обработчик закрытия окна через "крестик". Ведёт себя так же, как
        кнопка "Стоп": если бэкенд ещё работает — сперва даём ему шанс
        корректно завершиться (SIGINT -> его собственный finally закрывает
        Firefox), и только когда процесс реально завершится (сам или будет
        принудительно добит фоновым потоком из _stop/_force_stop_if_needed),
        закрываем само окно. Так браузер не остаётся висеть, если программу
        закрыли крестиком, а не кнопкой "Стоп"."""
        if self.proc is not None and self.proc.poll() is None:
            self._stop()
            self._wait_for_shutdown_then_close()
        else:
            self.destroy()

    def _wait_for_shutdown_then_close(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.destroy()
            return
        self.after(150, self._wait_for_shutdown_then_close)

    # ------------------------------------------------------------------
    # Лог-консоль
    # ------------------------------------------------------------------

    def _append_log(self, text: str) -> None:
        self._log_backlog = (getattr(self, "_log_backlog", "") + text)[-200_000:]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "__PROCESS_ENDED__":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    continue
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _check_audio_devices(self) -> None:
        script = self._backend_script()
        if not script.exists():
            messagebox.showerror(self._t("error_title"), self._t("script_not_found",
                                                                   name=script.name, path=str(script)))
            return
        self._append_log(f"\n$ {sys.executable} {script.name} --list-audio-devices\n")
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--list-audio-devices"],
                capture_output=True, text=True, timeout=15,
            )
            self._append_log(result.stdout + result.stderr)
        except Exception as exc:
            self._append_log(self._t("audio_check_error", exc=exc))

    def _reset_firefox_cache(self) -> None:
        """Стирает сохранённую копию профиля Firefox и запомненный путь к
        firefox-bin (см. resolve_firefox_launch_plan в auto_record_suno.py)
        — пригождается, например, если сессия в кэше протухла и нужно
        заново подхватить свежий залогиненный профиль из основного
        Firefox."""
        script = self._backend_script()
        if not script.exists():
            messagebox.showerror(self._t("error_title"), self._t("script_not_found",
                                                                   name=script.name, path=str(script)))
            return
        if not messagebox.askyesno(self._t("reset_firefox_cache_confirm_title"),
                                    self._t("reset_firefox_cache_confirm")):
            return
        self._append_log(f"\n$ {sys.executable} {script.name} --reset-firefox-cache\n")
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--reset-firefox-cache"],
                capture_output=True, text=True, timeout=15,
            )
            self._append_log(result.stdout + result.stderr)
            self._append_log(self._t("reset_firefox_cache_done_log"))
        except Exception as exc:
            self._append_log(self._t("reset_firefox_cache_error", exc=exc))


if __name__ == "__main__":
    app = RecorderGUI()
    app.mainloop()
