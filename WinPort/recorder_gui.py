#!/usr/bin/env python3
"""
recorder_gui.py (Windows редакция)

Блочный графический интерфейс поверх auto_record_suno.py /
auto_record_youtube.py — сетка цветных прямоугольных кнопок-контейнеров в
одном окне + лог-консоль, дублирующая то, что скрипт печатает в терминал.

Запуск:
    python recorder_gui.py

Зависимости: стандартная библиотека (tkinter) + то, что требует сам
auto_record_suno.py (внешних зависимостей нет).

ВАЖНО: этот файл сам ничего не записывает — он строит команду
(auto_record_suno.py или auto_record_youtube.py с нужными флагами) и
запускает её отдельным процессом, построчно перекачивая его stdout/stderr в лог-консоль в
реальном времени.

ВАЖНО про сборку в один .exe (PyInstaller --onefile, см. build-windows.sh):
в обычном запуске ("python recorder_gui.py") воркер запускается как
отдельный процесс "python auto_record_suno.py ..." — рядом лежит
одноимённый .py-файл. Но в собранном .exe модуль auto_record_suno
вкомпилирован ВНУТРЬ самого exe (см. --hidden-import в build-windows.sh)
и никакого auto_record_suno.py на диске не существует вообще — попытка
запустить его как файл (даже по пути внутрь временной распаковки
PyInstaller, _MEIxxxx) закончится ошибкой "не найден". Поэтому при
frozen-запуске (getattr(sys, 'frozen', False) — так PyInstaller
помечает собранный .exe) воркер запускается ПОВТОРНЫМ вызовом того же
самого .exe со спецфлагом --run-worker, который — см. блок
"if __name__ == '__main__'" в самом низу этого файла — заставляет тот
процесс не открывать GUI, а сразу импортировать auto_record_suno и
вызвать его main() в этом же процессе. См. _worker_command() ниже.

Отличия от исходной (Linux) версии — только там, где этого требует платформа:
  - нет переключателя «Захват экрана: Авто/X11/Wayland» (на Windows его нет:
    видео всегда захватывается через gdigrab, см. screen_capture.py); на его
    месте — чекбокс «Аварийный режим» (захват звука без изоляции по Firefox);
  - воркер (Suno/YouTube) запускается по-windows-ски: см. _worker_base_command.

ВАЖНО про окно консоли: программа собирается как windowed-приложение (см.
build-windows.sh, --noconsole) — второго чёрного окна нет. Воркер запускается
без окна (CREATE_NO_WINDOW), его вывод идёт в лог-панель через pipe, а команда
«Остановить» отправляется строкой STOP в его stdin (см. win_stdio.py) — у GUI
без консоли нельзя послать CTRL_BREAK_EVENT.

Дополнительные возможности:
  - Локализация интерфейса (русский/английский) — см. i18n.py.
  - Профили настроек (сервис, контейнер, звук внутри видео, папка, URL, аварийный
    режим, язык) — см. gui_profiles.py.
  - Разделы "Во что конвертировать" (аудио/видео) и "Логи" регулируются по
    высоте пользователем; при подъёме раздела "Логи" сетка контейнеров сама
    перестраивается в большее число колонок, чтобы ничего не обрезалось.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import gui_profiles
import win_stdio
from format_options import containers_for_service, compatible_audio_choices, get_container
from i18n import DEFAULT_LANGUAGE, LANGUAGES, container_label, tr

SCRIPT_DIR = Path(__file__).resolve().parent
SUNO_SCRIPT = SCRIPT_DIR / "auto_record_suno.py"
YOUTUBE_SCRIPT = SCRIPT_DIR / "auto_record_youtube.py"

BG = "#e9e7e0"          # фон окна
LOG_BG = "#101418"
LOG_FG = "#c9d1d9"

# Воркер запускается БЕЗ окна консоли (CREATE_NO_WINDOW) и в своей process
# group (CREATE_NEW_PROCESS_GROUP — чтобы Ctrl+C из терминала, если GUI запущен
# из него, не убил воркер в обход корректного закрытия Firefox).
#
# «Мягкая остановка» больше НЕ делается сигналом CTRL_BREAK_EVENT: у windowed-GUI
# нет консоли, а GenerateConsoleCtrlEvent работает только внутри одной консоли.
# Вместо этого в stdin воркера пишется строка STOP (win_stdio.STOP_COMMAND);
# воркер слушает stdin, по команде поднимает KeyboardInterrupt в главном потоке
# и штатно закрывает firefox.exe (session.quit() в finally). Если воркер не
# ответил за _STOP_GRACE_SECONDS — убивается всё его дерево процессов (taskkill).
_POPEN_CREATIONFLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)
# Разовые команды (диагностика звука, сброс кэша): без окна и без stdin.
_ONESHOT_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_STOP_GRACE_SECONDS = 20.0
# YouTube: после «Стоп» воркер ещё кодирует звук и сшивает его с видео (для часового ролика
# это заметно дольше, чем для трека Suno) — даём больше времени, прежде чем убивать процесс.
_STOP_GRACE_SECONDS_YOUTUBE = 180.0
_AUDIO_CHECK_TIMEOUT = 120.0

# Воркер (auto_record_suno.py) сам переключает свои stdout/stderr в UTF-8
# (см. этот файл, errors="replace" — иначе print() может упасть с
# UnicodeEncodeError на символе, которого нет в активной кодовой странице
# консоли, например 'charmap' codec can't encode character...) — здесь
# читаем его вывод С ТОЙ ЖЕ кодировкой, иначе получим либо ошибку
# декодирования на стороне GUI, либо "кракозябры" в лог-панели.
_WORKER_SUBPROCESS_KWARGS = dict(encoding="utf-8", errors="replace")
# PYTHONUNBUFFERED — чтобы прогресс скачивания (проценты) и обычные строки
# лога появлялись в лог-панели GUI сразу, а не пачками (когда stdout
# процесса подключён не к консоли, а к pipe, Python по умолчанию
# буферизует вывод блоками, а не по строкам).
_WORKER_ENV = {**os.environ, "PYTHONUNBUFFERED": "1"}


class RecorderGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.configure(bg=BG)
        self.minsize(760, 640)

        self.lang_var = tk.StringVar(value=DEFAULT_LANGUAGE)
        self.service_var = tk.StringVar(value="suno")
        self.container_var = tk.StringVar(value="")   # '' = не выбран -> WAV по умолчанию
        # Контейнер ЗВУКА внутри видео (независимо от container_var, который для
        # видео задаёт контейнер видео) — '' = не выбран -> WAV 96kHz/24bit (там,
        # где выбранный видео-контейнер это физически поддерживает, см.
        # format_options.VIDEO_PCM_CAPABLE).
        self.audio_container_var = tk.StringVar(value="")
        self.dest_var = tk.StringVar(value=str(Path.home()))
        self.url_var = tk.StringVar(value="")
        self.capture_fallback_var = tk.BooleanVar(value=False)  # --capture-mode device
        self.profile_var = tk.StringVar(value="")

        self.proc: subprocess.Popen | None = None
        self._audio_check_running = False
        self._proc_service = "suno"
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.container_buttons: dict[str, tk.Button] = {}

        self._container_min_cols = 2
        self._container_max_cols = 5
        self._container_col_count = self._container_min_cols
        self._container_resize_job: str | None = None
        self._measure_frame: tk.Frame | None = None

        self._build_layout()
        self._poll_log_queue()
        self._apply_startup_profile_or_initial_geometry()
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

    # ------------------------------------------------------------------
    # Локализация
    # ------------------------------------------------------------------

    def _t(self, key: str, **kwargs) -> str:
        return tr(self.lang_var.get(), key, **kwargs)

    def _on_language_changed(self) -> None:
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

        # --- источник / папка сохранения / аварийный режим захвата звука ---
        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=14, pady=6)

        tk.Label(opts, text=self._t("url_label"), bg=BG).grid(row=0, column=0, sticky="w")
        tk.Entry(opts, textvariable=self.url_var, width=50).grid(row=0, column=1, sticky="we", padx=6)

        tk.Label(opts, text=self._t("dest_label"), bg=BG).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(opts, textvariable=self.dest_var, width=50).grid(row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        tk.Button(opts, text=self._t("choose_btn"), command=self._choose_dest).grid(
            row=1, column=2, padx=4, pady=(6, 0))

        tk.Checkbutton(
            opts, text=self._t("capture_fallback_label"), variable=self.capture_fallback_var,
            bg=BG, font=("Sans", 9),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

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
        self.check_audio_btn = tk.Button(
            controls, text=self._t("check_audio_btn"), command=self._check_audio_devices,
            state="disabled" if self._audio_check_running else "normal")
        self.check_audio_btn.pack(side="left", padx=8)
        tk.Button(controls, text=self._t("reset_firefox_cache_btn"),
                  command=self._reset_firefox_cache).pack(side="left", padx=8)

        # --- регулируемые по высоте разделы: контейнеры и логи ---
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        self.paned = paned

        self.container_frame = tk.LabelFrame(paned, text=self._t("containers_group"), bg=BG, padx=10, pady=10)
        paned.add(self.container_frame, weight=1)
        self._render_container_grid()
        self.container_frame.bind("<Configure>", self._on_container_area_configure)

        log_frame = tk.LabelFrame(paned, text=self._t("logs_group"), bg=BG, padx=6, pady=6)
        paned.add(log_frame, weight=2)
        self.log_text = tk.Text(log_frame, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                                 font=("Consolas", 10), wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

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
            "capture_fallback": self.capture_fallback_var.get(),
            "language": self.lang_var.get(),
            "window_width": self.winfo_width(),
            "window_height": self.winfo_height(),
        }

    def _apply_profile_data(self, data: dict) -> None:
        self.service_var.set(data.get("service", self.service_var.get()))
        self.dest_var.set(data.get("dest", self.dest_var.get()))
        self.url_var.set(data.get("url", self.url_var.get()))
        self.capture_fallback_var.set(bool(data.get("capture_fallback", False)))
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
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        margin = 80
        available_h = max(400, screen_h - margin)
        available_w = max(700, screen_w - 60)

        natural_h = self.winfo_reqheight()
        natural_w = self.winfo_reqwidth()

        fixed_h = self.paned.winfo_y() if hasattr(self, "paned") else natural_h
        min_paned_visible = 160

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
        if self._container_resize_job is not None:
            try:
                self.after_cancel(self._container_resize_job)
            except (ValueError, tk.TclError):
                pass
        self._container_resize_job = self.after(60, self._adjust_container_columns)

    def _measure_container_height(self, col_count: int) -> int:
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

        min_button_px = 150
        max_cols_by_width = max(self._container_min_cols, available_w // min_button_px)
        max_cols = min(self._container_max_cols, max_cols_by_width)

        self.container_frame.update_idletasks()
        current_content_h = self._measure_container_height(self._container_col_count)
        chrome = max(0, self.container_frame.winfo_reqheight() - current_content_h)

        chosen = max_cols
        for cols in range(self._container_min_cols, max_cols + 1):
            if chrome + self._measure_container_height(cols) <= available_h:
                chosen = cols
                break

        if chosen != self._container_col_count:
            self._render_container_grid(col_count=chosen)

    def _choose_dest(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()))
        if chosen:
            self.dest_var.set(chosen)

    # ------------------------------------------------------------------
    # Запуск/остановка бэкенда
    # ------------------------------------------------------------------

    def _backend_script(self, service: str | None = None) -> Path:
        return SUNO_SCRIPT if (service or self.service_var.get()) == "suno" else YOUTUBE_SCRIPT

    def _worker_base_command(self, service: str | None = None) -> list[str]:
        """Базовая команда для запуска воркера выбранного сервиса (auto_record_suno
        или auto_record_youtube; без специфичных для конкретного вызова
        аргументов, см. _build_command и _check_audio_devices/
        _reset_firefox_cache) — единая точка, учитывающая разницу между обычным
        запуском ("python recorder_gui.py", рядом лежат .py-файлы) и собранным
        PyInstaller-экзешником (см. подробное объяснение в докстринге модуля
        наверху и в блоке if __name__ == '__main__' в самом низу этого файла)."""
        service = service or self.service_var.get()
        if getattr(sys, "frozen", False):
            # Тот же самый .exe, повторно запущенный со спецфлагом —
            # см. диспетчер воркера в самом низу файла.
            return [sys.executable, "--run-worker" if service == "suno" else "--run-worker-youtube"]
        script = self._backend_script(service)
        if not script.exists():
            raise FileNotFoundError(self._t("script_not_found", name=script.name, path=str(script)))
        return [sys.executable, str(script)]

    def _build_command(self) -> list[str]:
        cmd = self._worker_base_command()
        if self.url_var.get().strip():
            cmd.append(self.url_var.get().strip())
        cmd += ["-d", self.dest_var.get().strip() or "."]
        # канал управления: остановка строкой STOP в stdin (см. win_stdio.py)
        cmd += ["--control-stdin"]
        if self.container_var.get():
            cmd += ["--container", self.container_var.get()]
        # --audio-container имеет смысл только для видео-контейнера
        # youtube-бэкенда — combobox и так неактивен/сброшен для audio-
        # контейнеров и suno (см. _refresh_audio_container_choices), но
        # проверяем явно на всякий случай, чтобы не передать бэкенду
        # бессмысленный флаг.
        if self.service_var.get() == "youtube" and self.audio_container_var.get():
            cmd += ["--audio-container", self.audio_container_var.get()]
        if self.capture_fallback_var.get():
            cmd += ["--capture-mode", "device"]
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
            # stdin/stdout/stderr передаём ВСЕ явно: windowed-приложению без консоли
            # иначе нечего унаследовать (классическое «[WinError 6] неверный дескриптор»).
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, creationflags=_POPEN_CREATIONFLAGS,
                env=_WORKER_ENV, **_WORKER_SUBPROCESS_KWARGS,
            )
        except OSError as exc:
            messagebox.showerror(self._t("start_failed_title"), str(exc))
            return

        # сервис ЗАПУЩЕННОГО процесса (переключатель в окне за время записи могли сменить)
        self._proc_service = self.service_var.get()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._reader_thread, args=(self.proc,), daemon=True).start()

    def _reader_thread(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        lines = 0
        for line in proc.stdout:
            lines += 1
            self.log_queue.put(line)
        proc.wait()
        if lines == 0:
            # Ни строчки вывода: вероятно, у воркера не оказалось рабочего stdout —
            # тогда он пишет журнал в файл (см. win_stdio.ensure_std_streams).
            self.log_queue.put(self._t("worker_no_output", path=str(win_stdio.WORKER_LOG_PATH)))
        self.log_queue.put(self._t("process_ended_log", code=proc.returncode))
        self.log_queue.put("__PROCESS_ENDED__")

    def _send_stop_command(self, proc: subprocess.Popen) -> bool:
        """Отправляет воркеру команду STOP через его stdin. False — канал уже закрыт."""
        try:
            if proc.stdin is None:
                return False
            proc.stdin.write(win_stdio.STOP_COMMAND + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._append_log(self._t("stopping_log"))
            # Не proc.terminate() — auto_record_suno.py закрывает Firefox
            # (session.quit()) только в своём `finally`, который срабатывает при
            # KeyboardInterrupt. Воркер получает его по команде STOP из stdin
            # (см. комментарий у _POPEN_CREATIONFLAGS выше). Если канал уже
            # закрыт — процесс, скорее всего, и так завершается; на случай, если
            # нет, ниже сработает принудительная остановка по таймауту.
            self._send_stop_command(self.proc)
            grace = _STOP_GRACE_SECONDS_YOUTUBE if self._proc_service == "youtube" else _STOP_GRACE_SECONDS
            threading.Thread(target=self._force_stop_if_needed, args=(self.proc, grace), daemon=True).start()
        self.stop_btn.configure(state="disabled")

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        """Убивает воркер вместе со всем деревом (в onefile-сборке 'AutoRecord.exe'
        — это процесс-загрузчик + настоящий воркер + geckodriver + firefox;
        proc.kill() снёс бы только загрузчик, оставив невидимые процессы-сироты)."""
        try:
            import win_process
            win_process.kill_process_tree(proc.pid)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
        except OSError:
            pass

    def _force_stop_if_needed(self, proc: subprocess.Popen, grace_period: float = _STOP_GRACE_SECONDS) -> None:
        try:
            proc.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            self._kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _on_close_window(self) -> None:
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
                if line == "__AUDIO_CHECK_DONE__":
                    self._audio_check_running = False
                    try:
                        self.check_audio_btn.configure(state="normal")
                    except tk.TclError:   # пересобрали интерфейс при смене языка
                        pass
                    continue
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _check_audio_devices(self) -> None:
        """Диагностика звука: запускает воркер с --list-audio-devices в фоновом
        потоке (проверка захвата занимает время — окно не должно «замирать») и
        построчно выводит результат в лог."""
        if self._audio_check_running:
            return
        try:
            cmd = self._worker_base_command() + ["--list-audio-devices"]
        except FileNotFoundError as exc:
            messagebox.showerror(self._t("error_title"), str(exc))
            return
        self._append_log(f"\n$ {' '.join(cmd)}\n")
        self._audio_check_running = True
        self.check_audio_btn.configure(state="disabled")
        threading.Thread(target=self._audio_check_thread, args=(cmd, self.lang_var.get()), daemon=True).start()

    def _audio_check_thread(self, cmd: list[str], lang: str) -> None:
        proc: subprocess.Popen | None = None
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, creationflags=_ONESHOT_CREATIONFLAGS,
                env=_WORKER_ENV, **_WORKER_SUBPROCESS_KWARGS,
            )

            def on_timeout() -> None:
                timed_out.set()
                self._kill_process_tree(proc)

            timer = threading.Timer(_AUDIO_CHECK_TIMEOUT, on_timeout)
            timer.daemon = True
            timer.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log_queue.put(line)
            proc.wait()
            if timed_out.is_set():
                self.log_queue.put(tr(lang, "audio_check_timeout", sec=int(_AUDIO_CHECK_TIMEOUT)))
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(tr(lang, "audio_check_error", exc=exc))
        finally:
            if timer is not None:
                timer.cancel()
            self.log_queue.put("__AUDIO_CHECK_DONE__")

    def _reset_firefox_cache(self) -> None:
        try:
            cmd = self._worker_base_command() + ["--reset-firefox-cache"]
        except FileNotFoundError as exc:
            messagebox.showerror(self._t("error_title"), str(exc))
            return
        if not messagebox.askyesno(self._t("reset_firefox_cache_confirm_title"),
                                    self._t("reset_firefox_cache_confirm")):
            return
        self._append_log(f"\n$ {' '.join(cmd)}\n")
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                creationflags=_ONESHOT_CREATIONFLAGS,
                env=_WORKER_ENV, **_WORKER_SUBPROCESS_KWARGS,
            )
            self._append_log(result.stdout + result.stderr)
            self._append_log(self._t("reset_firefox_cache_done_log"))
        except Exception as exc:
            self._append_log(self._t("reset_firefox_cache_error", exc=exc))

    def report_callback_exception(self, exc, val, tb) -> None:
        """Ошибки в обработчиках кнопок tkinter по умолчанию печатает в stderr —
        а без консоли их никто не увидит. Показываем в лог-панели."""
        try:
            self._append_log("\n[ошибка интерфейса]\n" + "".join(traceback.format_exception(exc, val, tb)))
        except Exception:  # noqa: BLE001
            pass


def _run_as_worker(module_name: str = "auto_record_suno") -> int:
    """Точка входа воркера внутри frozen-сборки (см. подробное объяснение
    в докстринге модуля наверху): вызывается, когда этот самый .exe запущен как
    'AutoRecord.exe --run-worker <аргументы auto_record_suno>' (Suno) или
    'AutoRecord.exe --run-worker-youtube <аргументы auto_record_youtube>' (YouTube)
    (см. _worker_base_command выше) — вместо открытия GUI сразу импортирует
    нужный модуль и вызывает его main() В ЭТОМ ЖЕ процессе, передав ему
    оставшиеся аргументы через sys.argv (как если бы это было
    "python auto_record_suno.py <аргументы>").

    Неперехваченное исключение в windowed-сборке PyInstaller показывает
    модальным окном «Unhandled exception» — воркер при этом бы «завис» невидимым.
    Поэтому traceback печатается в stdout (то есть в лог GUI), код выхода 1."""
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # убираем сам '--run-worker[-youtube]'
    win_stdio.ensure_std_streams(prefer_log_file=True)
    try:
        import importlib
        return importlib.import_module(module_name).main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        return 0
    except BaseException:  # noqa: BLE001
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-worker":
        raise SystemExit(_run_as_worker("auto_record_suno"))
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-worker-youtube":
        raise SystemExit(_run_as_worker("auto_record_youtube"))
    # windowed-сборка: у GUI-процесса sys.stdout/stderr могут быть None
    win_stdio.ensure_std_streams()
    win_stdio.install_no_window_popen()   # чтобы ни один подпроцесс не открыл своё окно консоли
    app = RecorderGUI()
    app.mainloop()
