"""
timpoc — пользовательские настройки (не путать с оформлением субтитров).

Здесь живут параметры, которые влияют на поведение программы, а не на вид
субтитров: язык распознавания, устройство, автономный режим, эффекты панели.

Приватность — отдельный блок, потому что это требование заказчика:
  offline_mode = True  → движок не делает НИКАКИХ сетевых запросов,
                         включая проверку обновлений и скачивание моделей.
По умолчанию False, но скачивание модели всё равно происходит только по
явному нажатию кнопки. Панель обязана показывать размер загрузки заранее.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SETTINGS_VERSION = 1


@dataclass
class PrivacySettings:
    offline_mode: bool = False            # полностью отключить сеть
    telemetry: bool = False               # по умолчанию ВЫКЛЮЧЕНО: данные не собираем
    allow_model_download: bool = True     # разрешение на загрузку модели
    keep_temp_audio: bool = False         # хранить временные WAV для отладки

    def to_dict(self, safe: bool = False) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UiSettings:
    language: str = "ru"                  # язык интерфейса
    effects_level: str = "full"           # full | reduced | off  (упрощённый режим)
    reduce_motion: bool = False           # уменьшить анимации
    panel_scale: float = 1.0              # масштаб интерфейса панели
    show_safe_zones: bool = True
    preview_aspect: str = "comp"          # см. layout.ASPECT_PRESETS

    def validate(self) -> List[str]:
        warns: List[str] = []
        if self.effects_level not in ("full", "reduced", "off"):
            warns.append("Неизвестный уровень эффектов — использован «reduced».")
        if not (0.5 <= self.panel_scale <= 2.0):
            warns.append("Масштаб панели вне диапазона 0.5–2.0 — сброшен к 1.0.")
        return warns


@dataclass
class RecognitionDefaults:
    """Значения по умолчанию для распознавания, которые видит пользователь."""

    language: str = "ru"                  # ru | en | auto
    model: str = "medium"
    device: str = "auto"                  # auto | cpu | cuda
    vad_filter: bool = True
    word_timestamps: bool = True
    beam_size: int = 5
    range_mode: str = "work_area"         # work_area | full_comp | custom | file
    initial_prompt: str = ""              # подсказка движку (термины, имена)


@dataclass
class AppSettings:
    version: int = SETTINGS_VERSION
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    ui: UiSettings = field(default_factory=UiSettings)
    recognition: RecognitionDefaults = field(default_factory=RecognitionDefaults)
    last_used: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ API

    def to_dict(self, safe: bool = False) -> Dict[str, Any]:
        return {
            "version": self.version,
            "privacy": self.privacy.to_dict(safe=safe),
            "ui": asdict(self.ui),
            "recognition": asdict(self.recognition),
            "last_used": {} if safe else dict(self.last_used),
        }

    def merged(self, patch: Dict[str, Any]) -> "AppSettings":
        """Применить частичное обновление, не теряя остальных значений."""
        current = self.to_dict()
        for section, values in (patch or {}).items():
            if isinstance(values, dict) and isinstance(current.get(section), dict):
                for k, v in values.items():
                    if k in current[section]:
                        current[section][k] = v
            elif section in current:
                current[section] = values
        return _from_full_dict(current)

    def validate(self) -> List[str]:
        return self.ui.validate()


def _from_full_dict(d: Dict[str, Any]) -> AppSettings:
    def sub(cls, data):
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    s = AppSettings()
    s.privacy = sub(PrivacySettings, d.get("privacy"))
    s.ui = sub(UiSettings, d.get("ui"))
    s.recognition = sub(RecognitionDefaults, d.get("recognition"))
    s.last_used = dict(d.get("last_used") or {})
    if s.ui.panel_scale:
        s.ui.panel_scale = max(0.5, min(2.0, float(s.ui.panel_scale)))
    return s


def settings_path() -> str:
    from .logger import data_dir

    return os.path.join(data_dir(), "settings.json")


def load_settings(path: Optional[str] = None) -> AppSettings:
    path = path or settings_path()
    if not os.path.isfile(path):
        return AppSettings()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _from_full_dict(json.load(fh))
    except (OSError, ValueError):
        # Битый файл настроек не должен мешать запуску: начинаем с чистого.
        return AppSettings()


def save_settings(settings: AppSettings, path: Optional[str] = None) -> str:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # атомарно: не оставляем половинчатый файл при сбое
    return path
