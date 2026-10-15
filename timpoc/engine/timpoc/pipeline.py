"""
timpoc — сквозной конвейер: аудио → распознавание → фразы → субтитры.

Это единственное место, где соединяются все части. Порядок шагов фиксирован
и каждый шаг сообщает прогресс, поэтому панель может честно рисовать этапы:

  1. Чтение аудио            (0.00–0.06)
  2. Проверка на тишину      (0.06–0.08)
  3. Распознавание речи      (0.10–0.90)
  4. Сборка фраз             (0.90–0.95)
  5. Выравнивание по кадру   (0.95–0.99)

Никакого «остальное аналогично»: функция делает ровно то, что написано.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import timing
from .audio import analyze_silence, load_audio, require_speech
from .errors import EmptyRangeError, wrap_unknown
from .recognize import RecognitionSettings, Recognizer, TARGET_SAMPLE_RATE_DEFAULT
from .segmentation import (
    SegmentationSettings,
    build_blocks,
    check_readability,
    extend_holds,
)
from .types import RecognitionInfo, Transcript


@dataclass
class PipelineInput:
    path: str
    range_start: float = 0.0          # секунда в файле, с которой распознаём
    range_end: Optional[float] = None # None = до конца файла
    offset_seconds: float = 0.0       # где в композиции начинается это аудио
    frame_rate: float = 25.0
    comp_start: float = 0.0           # время начала композиции (обычно 0)
    comp_end: Optional[float] = None
    source_kind: str = "file"         # "comp" | "layer" | "file"
    source_label: str = ""


@dataclass
class PipelineResult:
    transcript: Transcript
    silence: dict
    readability: dict
    timings: dict

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "transcript": self.transcript.to_dict(),
            "silence": self.silence,
            "readability": self.readability,
            "timings": self.timings,
        }


def run_pipeline(
    inp: PipelineInput,
    recognition: RecognitionSettings,
    segmentation: SegmentationSettings,
    progress: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    precomputed_words: Optional[list] = None,
) -> PipelineResult:
    """Выполнить распознавание и сборку субтитров.

    precomputed_words — служебный параметр: если передан, шаг распознавания
    пропускается, а дальше идут те же самые шаги сборки фраз и таймингов.
    Нужен, чтобы проверять математику смещений детерминированно: движок
    распознавания по природе недетерминирован, и тест на «сдвинулось ровно
    на 10 секунд» на живом движке был бы нестабильным.
    """
    log = log or (lambda m: None)
    emit = progress or (lambda p, m: None)

    recognition = recognition.sanitized()
    segmentation = segmentation.sanitized()
    warnings: List[str] = []
    warnings.extend(recognition.validate())
    warnings.extend(segmentation.validate())

    if inp.range_end is not None and inp.range_end <= inp.range_start:
        raise EmptyRangeError()

    marks: dict = {}
    t0 = time.time()

    # 1. Аудио -------------------------------------------------------------
    emit(0.02, "Чтение аудио")
    audio, sample_rate = load_audio(
        inp.path,
        start=inp.range_start,
        end=inp.range_end,
        sample_rate=TARGET_SAMPLE_RATE_DEFAULT,
    )
    marks["load_audio"] = round(time.time() - t0, 3)
    log(f"Аудио: {audio.size / sample_rate:.2f} с, {int(sample_rate)} Гц")

    # 2. Тишина ------------------------------------------------------------
    emit(0.06, "Проверка на тишину")
    silence = analyze_silence(audio, int(sample_rate))
    require_speech(silence)
    marks["silence_check"] = round(time.time() - t0, 3)

    # 3. Распознавание -----------------------------------------------------
    if precomputed_words is not None:
        words = list(precomputed_words)
        info = RecognitionInfo(model="injected", device="none", compute_type="none")
        rec_warnings: List[str] = []
    else:
        rec = Recognizer(recognition, log=log)
        words, info, rec_warnings = rec.transcribe(
            audio,
            int(sample_rate),
            offset_seconds=inp.offset_seconds,
            progress=lambda p, m: emit(0.10 + p * 0.80, m),
            cancel=cancel,
        )
    warnings.extend(rec_warnings)
    marks["recognize"] = round(time.time() - t0, 3)
    log(f"Слов распознано: {len(words)}")

    if cancel and cancel():
        from .errors import CancelledError

        raise CancelledError()

    # 4. Сборка фраз -------------------------------------------------------
    emit(0.90, "Разбиение на фразы")
    blocks = build_blocks(words, segmentation)
    if not blocks:
        from .errors import SilenceError

        raise SilenceError(audio.size / sample_rate)

    # 5. Тайминги: «хвосты», пересечения, кадры ----------------------------
    emit(0.95, "Выравнивание таймингов")
    extend_holds(blocks, segmentation)
    overlap_fixed = timing.resolve_overlaps(blocks, fps=inp.frame_rate)
    timing.enforce_min_duration(blocks, max(0.2, segmentation.min_block_seconds * 0.5))
    timing.resolve_overlaps(blocks, fps=inp.frame_rate)  # второй проход после удлинений

    fps = inp.frame_rate if inp.frame_rate > 0 else 25.0
    for b in blocks:
        b.in_time = max(inp.comp_start, timing.snap_to_frame(b.in_time, fps))
        b.out_time = timing.snap_to_frame(b.out_time, fps)
        if b.out_time <= b.in_time:
            b.out_time = b.in_time + timing.frames_to_seconds(1.0, fps)

    if inp.comp_end is not None:
        timing.clamp_to_range(
            _wrap(blocks), inp.comp_start, inp.comp_end
        )
        blocks = _unwrap(blocks)

    readability = check_readability(blocks, segmentation, fps=fps)
    readability.overlap_fixed = overlap_fixed
    if readability.fast_blocks:
        warnings.append(
            f"{len(readability.fast_blocks)} блоков показываются быстрее, чем их успевают прочитать. "
            "Откройте редактор и увеличьте длительность или сократите текст."
        )
    marks["segment"] = round(time.time() - t0, 3)

    transcript = Transcript(
        blocks=blocks,
        words=words,
        info=info,
        warnings=warnings,
        source={
            "kind": inp.source_kind,
            "label": inp.source_label,
            "path": inp.path,
            "range_start": round(inp.range_start, 3),
            "range_end": None if inp.range_end is None else round(inp.range_end, 3),
            "offset_seconds": round(inp.offset_seconds, 3),
            "frame_rate": fps,
        },
        base_offset=0.0,
    )

    emit(0.99, "Готово")
    return PipelineResult(
        transcript=transcript,
        silence=silence.to_dict(),
        readability=readability.to_dict(),
        timings=marks,
    )


def _wrap(blocks):
    """transcript-подобная обёртка, чтобы переиспользовать clamp_to_range."""
    from .types import Transcript as _T

    t = _T()
    t.blocks = list(blocks)
    return t


def _unwrap(t) -> list:
    return list(t.blocks)


def resegment(transcript: Transcript, segmentation: SegmentationSettings, frame_rate: float) -> Transcript:
    """Пересобрать фразы из тех же слов БЕЗ повторного распознавания.

    Именно эта функция делает правду утверждение «изменение оформления не
    запускает распознавание заново»: слова сохранены в транскрипте, и мы
    просто заново раскладываем их на блоки по новым лимитам.
    """
    segmentation = segmentation.sanitized()
    new_blocks = build_blocks(transcript.words, segmentation)
    extend_holds(new_blocks, segmentation)
    fps = frame_rate if frame_rate > 0 else 25.0
    timing.resolve_overlaps(new_blocks, fps=fps)
    for b in new_blocks:
        b.in_time = timing.snap_to_frame(b.in_time, fps)
        b.out_time = timing.snap_to_frame(b.out_time, fps)
        if b.out_time <= b.in_time:
            b.out_time = b.in_time + timing.frames_to_seconds(1.0, fps)
    transcript.blocks = new_blocks
    transcript.warnings = list(segmentation.validate())
    return transcript
