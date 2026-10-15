"""
timpoc — состояние проекта: распознанный текст + оформление + расположение.

Зачем это нужно (прямое требование ТЗ):
  «Сохранение распознанного текста и настроек, чтобы ошибка применения не
   требовала нового распознавания.»

Файл состояния пишется на диск после каждого значимого изменения:
  * распознавание завершилось;
  * пользователь поправил текст или тайминги;
  * изменилось оформление или расположение.

Поэтому, если After Effects закрылся или применение к композиции упало,
при следующем открытии панели всё на месте, и тяжёлое распознавание
повторять не нужно.

Отдельно: файл состояния НЕ трогает проект AE и не удаляется при обновлении
timpoc. Удаление — только по явному действию пользователя.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .layout import Layout
from .logger import data_dir
from .style import TextStyle
from .types import Transcript

STATE_FILENAME = "project_state.json"


def state_path() -> str:
    return os.path.join(data_dir(), STATE_FILENAME)


@dataclass
class ProjectState:
    transcript: Optional[Transcript] = None
    style: TextStyle = field(default_factory=TextStyle)
    layout: Layout = field(default_factory=Layout)
    frame_rate: float = 25.0
    comp_width: float = 1920.0
    comp_height: float = 1080.0
    comp_name: str = ""
    applied_layer_name: str = ""       # имя слоя, созданного timpoc, — чтобы обновлять без дублей
    applied_at: str = ""               # ISO-время последнего применения
    path: str = ""

    # ------------------------------------------------------------------ API

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "style": self.style.to_dict(),
            "layout": self.layout.to_dict(),
            "frame_rate": self.frame_rate,
            "comp_width": self.comp_width,
            "comp_height": self.comp_height,
            "comp_name": self.comp_name,
            "applied_layer_name": self.applied_layer_name,
            "applied_at": self.applied_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any], path: str = "") -> "ProjectState":
        tr_raw = d.get("transcript")
        return ProjectState(
            transcript=Transcript.from_dict(tr_raw) if tr_raw else None,
            style=TextStyle.from_dict(d.get("style") or {}),
            layout=Layout.from_dict(d.get("layout") or {}),
            frame_rate=float(d.get("frame_rate") or 25.0),
            comp_width=float(d.get("comp_width") or 1920.0),
            comp_height=float(d.get("comp_height") or 1080.0),
            comp_name=str(d.get("comp_name") or ""),
            applied_layer_name=str(d.get("applied_layer_name") or ""),
            applied_at=str(d.get("applied_at") or ""),
            path=path,
        )

    @staticmethod
    def load(path: Optional[str] = None) -> "ProjectState":
        path = path or state_path()
        if not os.path.isfile(path):
            return ProjectState(path=path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return ProjectState.from_dict(json.load(fh), path=path)
        except (OSError, ValueError):
            # Битое состояние — начинаем заново, но старый файл сохраняем как .bak,
            # чтобы ничего не потерять безвозвратно.
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
            return ProjectState(path=path)

    def save(self, path: Optional[str] = None) -> str:
        path = path or self.path or state_path()
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Атомарная запись: сначала во временный файл, потом переименование.
        # При обрыве питания пользователь не теряет предыдущее состояние.
        fd, tmp = tempfile.mkstemp(prefix=".timpoc_state_", suffix=".json", dir=directory or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return path

    def mark_applied(self, layer_name: str) -> None:
        from datetime import datetime, timezone

        self.applied_layer_name = layer_name
        self.applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()

    def clear(self) -> None:
        """Очистить состояние (кнопка «Начать заново»). Файл перезаписывается пустым."""
        self.transcript = None
        self.applied_layer_name = ""
        self.applied_at = ""
        self.save()

    @property
    def has_transcript(self) -> bool:
        return self.transcript is not None

    def summary(self) -> Dict[str, Any]:
        """Короткая сводка для заголовка панели."""
        if not self.transcript:
            return {"has_transcript": False, "blocks": 0}
        blocks = self.transcript.blocks
        return {
            "has_transcript": True,
            "blocks": len(blocks),
            "words": len(self.transcript.words),
            "language": self.transcript.info.language,
            "model": self.transcript.info.model,
            "device": self.transcript.info.device,
            "duration": round(self.transcript.total_speech(), 2),
            "applied_layer_name": self.applied_layer_name,
            "applied_at": self.applied_at,
        }
