"""
timpoc — доменная модель.

Главная идея разделения данных:
  Word       — то, что отдал движок распознавания (сырьё, менять не рекомендуется).
  SubtitleBlock — то, что видит и редактирует пользователь (текст, in/out).
  Transcript — весь результат распознавания + метаданные, которые нужны,
               чтобы потом «применить» к композиции без повторного распознавания.

Всё сериализуется в JSON: это то, что сохраняется на диск и передаётся в панель.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


@dataclass
class Word:
    """Одно слово с временными метками от движка распознавания.

    start/end — в секундах ОТНОСИТЕЛЬНО КОМПОЗИЦИИ (смещение уже учтено).
    probability — уверенность движка (0..1). Может отсутствовать.
    """

    text: str
    start: float
    end: float
    probability: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "probability": None if self.probability is None else round(self.probability, 3),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Word":
        return Word(
            text=d["text"],
            start=float(d["start"]),
            end=float(d["end"]),
            probability=d.get("probability"),
        )


@dataclass
class SubtitleBlock:
    """Один субтитр: текст и время показа.

    in_time / out_time — в секундах относительно начала композиции.
    text может содержать "\\n" — это перенос строки внутри блока.
    words — слова, из которых собран блок; нужны, чтобы пересобрать текст
            после правки таймингов и чтобы подсветить слово в редакторе.
    """

    in_time: float
    out_time: float
    text: str
    words: List[Word] = field(default_factory=list)
    locked: bool = False  # пользователь закрепил блок — автоправка его не трогает
    strong: bool = False  # граница поставлена осознанно (точка или пауза) — не склеивать

    @property
    def duration(self) -> float:
        """Длительность в секундах.

        Округление до миллисекунды обязательно: без него 0.3 - 0.2 даёт
        0.09999999999999964, и блок «проваливается» под пороги сравнения
        (например, min_block_seconds = 0.1). На реальной ошибке найдено.
        """
        return round(max(0.0, self.out_time - self.in_time), 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "in": round(self.in_time, 3),
            "out": round(self.out_time, 3),
            "text": self.text,
            "locked": self.locked,
            "strong": self.strong,
            "words": [w.to_dict() for w in self.words],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SubtitleBlock":
        return SubtitleBlock(
            in_time=float(d["in"]),
            out_time=float(d["out"]),
            text=d["text"],
            locked=bool(d.get("locked", False)),
            strong=bool(d.get("strong", False)),
            words=[Word.from_dict(w) for w in d.get("words", [])],
        )


@dataclass
class RecognitionInfo:
    """Что именно и как распознавалось — нужно для диагностики и честных отчётов."""

    language: str = ""
    language_probability: float = 0.0
    model: str = ""
    device: str = ""          # "cpu" | "cuda" | "mps"
    compute_type: str = ""    # "int8" | "float16" | ...
    duration_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    vad_used: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RecognitionInfo":
        known = {k: v for k, v in d.items() if k in RecognitionInfo.__dataclass_fields__}
        return RecognitionInfo(**known)


@dataclass
class Transcript:
    """Полный результат распознавания.

    Сохраняется в файл проекта timpoc (*.timpoc.json), чтобы:
      * смена оформления НЕ запускала распознавание заново;
      * ошибка применения к композиции не теряла работу;
      * можно было вернуться к редактированию на другом компьютере.
    """

    blocks: List[SubtitleBlock] = field(default_factory=list)
    words: List[Word] = field(default_factory=list)
    info: RecognitionInfo = field(default_factory=RecognitionInfo)
    warnings: List[str] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # ---- смещение, чтобы «общий сдвиг» работал обратимо ----
    # base_offset хранит суммарный сдвиг, применённый к этому транскрипту.
    base_offset: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "info": self.info.to_dict(),
            "warnings": list(self.warnings),
            "source": dict(self.source),
            "base_offset": round(self.base_offset, 3),
            "words": [w.to_dict() for w in self.words],
            "blocks": [b.to_dict() for b in self.blocks],
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Transcript":
        version = int(d.get("schema_version", 1))
        if version != SCHEMA_VERSION:
            # Не молчим: будущие версии читаем по старой схеме, но помечаем.
            pass
        return Transcript(
            blocks=[SubtitleBlock.from_dict(b) for b in d.get("blocks", [])],
            words=[Word.from_dict(w) for w in d.get("words", [])],
            info=RecognitionInfo.from_dict(d.get("info", {})),
            warnings=list(d.get("warnings", [])),
            source=dict(d.get("source", {})),
            schema_version=version,
            base_offset=float(d.get("base_offset", 0.0)),
        )

    @staticmethod
    def from_json(text: str) -> "Transcript":
        return Transcript.from_dict(json.loads(text))

    def total_speech(self) -> float:
        return sum(b.duration for b in self.blocks)
