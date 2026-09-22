#!/usr/bin/env python3
"""
gui_profiles.py

Хранение пользовательских профилей настроек recorder_gui.py на диске —
чтобы можно было сохранить свой набор (сервис, контейнер, папка
сохранения, URL, бэкенд захвата экрана, язык интерфейса) и потом
загружать его одним кликом вместо того, чтобы каждый раз настраивать
всё заново.

Формат хранения — по одному JSON-файлу на профиль в
~/.config/auto_record_gui/profiles/<имя>.json. Никакой базы данных не
нужно — профилей обычно единицы, а не сотни.

Дополнительно хранится указатель на последний СОХРАНЁННЫЙ (Save/Update)
профиль — ~/.config/auto_record_gui/last_profile.txt — recorder_gui.py
применяет его автоматически при следующем запуске (см. set_last_profile
/ get_last_profile).
"""

from __future__ import annotations

import json
from pathlib import Path

PROFILES_DIR = Path.home() / ".config" / "auto_record_gui" / "profiles"
LAST_PROFILE_FILE = PROFILES_DIR.parent / "last_profile.txt"


def _ensure_dir() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_filename(name: str) -> str:
    safe = "".join(c for c in name.strip() if c.isalnum() or c in (" ", "-", "_")).strip()
    if not safe:
        raise ValueError("empty profile name")
    return safe


def _path_for(name: str) -> Path:
    return PROFILES_DIR / f"{_safe_filename(name)}.json"


def list_profiles() -> list[str]:
    _ensure_dir()
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def save_profile(name: str, data: dict) -> Path:
    _ensure_dir()
    path = _path_for(name)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_profile(name: str) -> dict:
    path = _path_for(name)
    return json.loads(path.read_text(encoding="utf-8"))


def delete_profile(name: str) -> None:
    _path_for(name).unlink(missing_ok=True)


def set_last_profile(name: str) -> None:
    """Запоминает имя последнего СОХРАНЁННОГО (Save/Update) профиля —
    при следующем запуске recorder_gui.py именно он будет применён
    автоматически (см. get_last_profile)."""
    _ensure_dir()
    LAST_PROFILE_FILE.write_text(name, encoding="utf-8")


def get_last_profile() -> str | None:
    """Возвращает имя последнего сохранённого профиля, если оно есть и
    файл-указатель ещё существует; иначе None (например, при самом
    первом запуске, когда профилей ещё не было сохранено ни разу)."""
    try:
        name = LAST_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None
