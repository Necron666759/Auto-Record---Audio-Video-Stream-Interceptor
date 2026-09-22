#!/usr/bin/env python3
"""
gui_profiles.py (Windows редакция)

Хранение пользовательских профилей настроек recorder_gui.py на диске —
чтобы можно было сохранить свой набор (контейнер, папка сохранения, URL,
аварийный режим захвата, язык интерфейса) и потом загружать его одним
кликом вместо того, чтобы каждый раз настраивать всё заново.

Формат хранения — по одному JSON-файлу на профиль в
%APPDATA%\\auto_record_gui\\profiles\\<имя>.json (единственное отличие
от Linux-версии — расположение папки конфигурации: там был
~/.config/auto_record_gui, здесь — стандартная Windows-папка данных
приложения).

Дополнительно хранится указатель на последний СОХРАНЁННЫЙ (Save/Update)
профиль — %APPDATA%\\auto_record_gui\\last_profile.txt — recorder_gui.py
применяет его автоматически при следующем запуске (см. set_last_profile
/ get_last_profile).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_APPDATA = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))

PROFILES_DIR = _APPDATA / "auto_record_gui" / "profiles"
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
    _ensure_dir()
    LAST_PROFILE_FILE.write_text(name, encoding="utf-8")


def get_last_profile() -> str | None:
    try:
        name = LAST_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None
