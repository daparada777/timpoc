"""
timpoc — разбиение распознанных слов на читаемые субтитры.

Движок отдаёт слова. Зритель читает фразы. Здесь решается, где поставить
границу блока. Правила по приоритету (сверху важнее):

 1. Конец предложения ( . ! ? … ) — почти всегда граница.
 2. Длинная пауза между словами — граница, даже без знака препинания.
 3. Клаузальная граница ( , ; : — ) — мягкая граница, используется,
    когда блок уже длинный.
 4. Жёсткие лимиты: max_chars, max_words, max_lines — переполнение.
 5. Слишком короткие блоки склеиваются с соседями (анти-«мельтешение»).
 6. Текст блока раскладывается на строки по max_words_per_line / max_chars_per_line.

Функция build_blocks() детерминирована и не зависит от движка — её можно
тестировать на готовом списке слов без запуска распознавания.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .timing import clamp, frames_to_seconds
from .types import SubtitleBlock, Word

# Знаки, после которых почти всегда стоит закрыть блок.
SENTENCE_END = frozenset(".!?…")
# Знаки «мягкой» границы: делить здесь можно, если блок уже длинный.
CLAUSE_END = frozenset(",;:—–")

_WHITESPACE = re.compile(r"\s+")
# Слова, которые не должны начинать строку (висячая пунктуация слева).
_NO_LINE_START = frozenset(",.;:!?)]}»„”")
# Слова, которые не должны заканчивать строку (висячие предлоги/союзы).
_NO_LINE_END = frozenset("и а но или да не ни к с в на за от до из под над при о об у по для")


@dataclass
class SegmentationSettings:
    """Настройки разбиения. Всё это доступно в панели без правки кода."""

    max_chars: int = 42            # максимум символов в блоке (со всеми строками)
    max_words: int = 8             # максимум слов в блоке
    max_lines: int = 2             # максимум строк в блоке
    max_words_per_line: int = 5    # ориентир для переноса
    max_chars_per_line: int = 24   # ориентир для переноса

    min_block_seconds: float = 0.6   # короче — склеиваем с соседом
    max_block_seconds: float = 7.0   # длиннее — делим принудительно

    pause_split_seconds: float = 0.55  # пауза длиннее — граница блока
    soft_pause_seconds: float = 0.30   # пауза, после которой «мягкая» граница

    # Сколько секунд добавляем к концу блока, чтобы текст не исчезал
    # одновременно со звуком (читаем мы дольше, чем говорим).
    hold_seconds: float = 0.20
    max_hold_seconds: float = 1.20

    # Читаемость: минимальная скорость показа. Если блок исчезает быстрее,
    # чем его успевают прочитать, предупреждаем (но не ломаем тайминги).
    chars_per_second_warn: float = 18.0

    merge_short_blocks: bool = True
    break_on_sentence_end: bool = True
    break_on_pause: bool = True

    def validate(self) -> List[str]:
        """Вернуть список предупреждений о сомнительных настройках (не ошибок)."""
        warns: List[str] = []
        if self.max_lines < 1:
            warns.append("Количество строк меньше 1 — использовано 1.")
        if self.max_words < 1:
            warns.append("Максимум слов меньше 1 — использовано 1.")
        if self.max_chars < 8:
            warns.append("Максимум символов очень мал — текст будет рваться.")
        if self.max_words_per_line > self.max_words:
            warns.append("Слов в строке больше, чем в блоке — перенос не сработает.")
        if self.min_block_seconds > self.max_block_seconds:
            warns.append("Минимальная длина блока больше максимальной.")
        return warns

    def sanitized(self) -> "SegmentationSettings":
        """Копия с безопасно подставленными значениями вместо некорректных."""
        s = SegmentationSettings(**self.__dict__)
        s.max_lines = max(1, int(s.max_lines))
        s.max_words = max(1, int(s.max_words))
        s.max_chars = max(8, int(s.max_chars))
        s.max_words_per_line = max(1, int(s.max_words_per_line))
        s.max_chars_per_line = max(1, int(s.max_chars_per_line))
        if s.min_block_seconds > s.max_block_seconds:
            s.min_block_seconds = min(s.min_block_seconds, s.max_block_seconds)
        return s


def _strip_word(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _ends_sentence(word: str) -> bool:
    w = _strip_word(word)
    if not w:
        return False
    return w[-1] in SENTENCE_END


def _ends_clause(word: str) -> bool:
    w = _strip_word(word)
    if not w:
        return False
    return w[-1] in CLAUSE_END


def _plain_len(text: str) -> int:
    """Длина без переносов строк: перенос не должен считаться символом текста."""
    return len(text.replace("\n", " ").strip())


def wrap_words_to_lines(words: Sequence[Word], settings: SegmentationSettings) -> str:
    """Разложить слова блока на строки.

    Переносим по max_words_per_line и max_chars_per_line, не рвём слова
    и не оставляем пунктуацию в начале строки.
    """
    if not words:
        return ""
    s = settings.sanitized()
    lines: List[List[str]] = [[]]
    cur_len = 0
    for i, w in enumerate(words):
        text = _strip_word(w.text)
        if not text:
            continue
        line_words = len(lines[-1])
        projected = cur_len + (1 if cur_len else 0) + len(text)
        overflow_words = line_words >= s.max_words_per_line
        overflow_chars = projected > s.max_chars_per_line
        next_is_punct_only = i + 1 < len(words) and _strip_word(words[i + 1].text) in _NO_LINE_START

        if lines[-1] and (overflow_words or overflow_chars) and not next_is_punct_only:
            lines.append([text])
            cur_len = len(text)
        else:
            if lines[-1]:
                cur_len += 1
            lines[-1].append(text)
            cur_len += len(text)

    # Не оставляем одиночную «висячую» частицу в конце строки.
    result: List[str] = []
    for line in lines:
        if not line:
            continue
        if len(line) > 1 and line[-1].strip().lower() in _NO_LINE_END:
            tail = line.pop()
            result.append(" ".join(line))
            result.append(tail)
        else:
            result.append(" ".join(line))
    return "\n".join(l for l in result if l.strip())


def _make_block(words: Sequence[Word], settings: SegmentationSettings) -> SubtitleBlock:
    text = wrap_words_to_lines(words, settings)
    if not text:
        text = " ".join(_strip_word(w.text) for w in words if _strip_word(w.text))
    return SubtitleBlock(
        in_time=float(words[0].start),
        out_time=float(words[-1].end),
        text=text,
        words=list(words),
    )


def build_blocks(words: Sequence[Word], settings: SegmentationSettings) -> List[SubtitleBlock]:
    """Собрать список субтитров из потока слов.

    words должны быть отсортированы по start и уже переведены во время
    композиции (смещение учтено вызывающим кодом).
    """
    if not words:
        return []
    s = settings.sanitized()

    # Слова без времени/текста ломают всё дальше по конвейеру — отбрасываем явно.
    clean: List[Word] = []
    for w in words:
        t = _strip_word(w.text)
        if not t:
            continue
        if w.end < w.start:
            w = Word(text=t, start=w.start, end=w.start, probability=w.probability)
        else:
            w = Word(text=t, start=w.start, end=w.end, probability=w.probability)
        clean.append(w)
    clean.sort(key=lambda w: (w.start, w.end))
    if not clean:
        return []

    groups: List[List[Word]] = []
    # Причина, по которой группа была ЗАКРЫТА (sentence / pause / overflow /
    # clause / end). Из неё дальше выводится причина границы ПЕРЕД следующим
    # блоком — именно она решает, можно ли склеить два коротких блока.
    closes: List[str] = []
    current: List[Word] = []

    def flush(reason: str = "end") -> None:
        nonlocal current
        if current:
            groups.append(current)
            closes.append(reason)
            current = []

    for i, w in enumerate(clean):
        current.append(w)

        text_plain = " ".join(_strip_word(x.text) for x in current)
        chars = _plain_len(text_plain)
        n_words = len(current)
        duration = w.end - current[0].start

        # 4. Жёсткое переполнение — делим безусловно.
        hard_overflow = chars > s.max_chars or n_words > s.max_words
        # Слишком длинный блок по времени.
        too_long = duration > s.max_block_seconds
        if hard_overflow or too_long:
            # Если переполнение наступило на первом же слове группы (одно гигантское
            # слово), делить некуда — отдаём как есть.
            if len(current) == 1:
                flush("overflow")
            else:
                overflow_word = current.pop()
                flush("overflow")
                current = [overflow_word]
            continue

        is_last = i == len(clean) - 1
        gap = (clean[i + 1].start - w.end) if not is_last else 0.0

        # 1. Конец предложения.
        if s.break_on_sentence_end and _ends_sentence(w.text) and n_words >= 1:
            # Не закрываем блок из одного короткого слова после точки, если
            # дальше идёт пауза меньше минимальной — иначе получим «рваный» текст.
            if n_words > 1 or not is_last:
                flush("sentence")
                continue

        # 2. Длинная пауза.
        if s.break_on_pause and gap >= s.pause_split_seconds:
            flush("pause")
            continue

        # 3. Мягкая граница: клауза + заметная пауза + блок уже приличный.
        if _ends_clause(w.text) and gap >= s.soft_pause_seconds and (
            chars >= s.max_chars * 0.6 or n_words >= max(2, s.max_words * 0.6)
        ):
            flush("clause")
            continue

    flush("end")

    # Причина границы ПЕРЕД каждым блоком:
    #   sentence — предыдущий блок закрыт концом предложения;
    #   pause    — текущий блок начался после долгой паузы;
    #   иначе    — граница «техническая» (переполнение, клауза, конец потока),
    #              и склейка коротких блоков через неё разрешена.
    reasons: List[str] = []
    for i, g in enumerate(groups):
        if i == 0:
            reasons.append("start")
        else:
            prev_close = closes[i - 1] if i - 1 < len(closes) else "end"
            reasons.append("sentence" if prev_close == "sentence" else prev_close)

    blocks: List[SubtitleBlock] = []
    for g, reason in zip(groups, reasons):
        if not g:
            continue
        block = _make_block(g, s)
        block.strong = reason in ("sentence", "pause")
        blocks.append(block)

    if s.merge_short_blocks:
        blocks = _merge_short(blocks, s, reasons)

    return blocks


def _merge_short(
    blocks: List[SubtitleBlock], s: SegmentationSettings, reasons: Optional[List[str]] = None
) -> List[SubtitleBlock]:
    """Склеить слишком короткие блоки с соседями, не нарушая лимитов.

    Политика границ — итоговая, после трёх итераций отладки на реальных данных:

      1. Конец предложения — граница неприкосновенна. Склейка через точку
         превращает «Правда? Да.» в одну реплику и ломает интонацию.
         Пользователь может склеить вручную (в панели есть «Объединить»).
      2. Пауза — не склеиваем. Иначе субтитр «висит» через всю тишину,
         и это ровно тот баг, из-за которого субтитры кажутся рассинхронными.
      3. Всё остальное (переполнение лимита, мягкая клауза, конец потока) —
         склеиваем, если оба блока короче min_block_seconds, вместе влезают
         в лимиты и между ними нет фактической тишины.

    Пункт 3 обязателен: без него короткие фразы мельтешат по одному слову.
    """
    if len(blocks) < 2:
        return blocks
    if reasons is None:
        reasons = ["end"] * len(blocks)

    out: List[SubtitleBlock] = [blocks[0]]
    out_reasons: List[str] = [reasons[0] if reasons else "end"]

    for idx in range(1, len(blocks)):
        b = blocks[idx]
        prev = out[-1]
        boundary_reason = reasons[idx] if idx < len(reasons) else "end"
        gap = max(0.0, b.in_time - prev.out_time)

        mergeable_reason = boundary_reason not in ("sentence", "pause")
        # Склеиваем только там, где есть реальное мельтешение: хотя бы один
        # из блоков короче min_block_seconds. Если оба читаются нормально —
        # трогать их незачем.
        either_short = prev.duration < s.min_block_seconds or b.duration < s.min_block_seconds
        # Порог берём от min_block_seconds, а не от max_block_seconds:
        # склейка нужна против мельтешения, а не чтобы собирать длинные фразы.
        # Иначе блок из 0.1 с приклеивался бы к двухсекундному соседу.
        merged_span = (b.out_time - prev.in_time) if b.out_time > prev.in_time else 0.0
        combined_ok = merged_span <= max(s.min_block_seconds * 3.0, 1.0)
        no_real_silence = gap < s.pause_split_seconds

        merged_words = list(prev.words) + list(b.words)
        merged_text = wrap_words_to_lines(merged_words, s)
        merged_chars = _plain_len(merged_text.replace("\n", " "))
        merged_lines = merged_text.count("\n") + 1
        fits = (
            len(merged_words) <= s.max_words
            and merged_chars <= s.max_chars
            and merged_lines <= s.max_lines
        )

        if mergeable_reason and either_short and combined_ok and no_real_silence and fits:
            prev.out_time = b.out_time
            prev.words = merged_words
            prev.text = merged_text
            prev.strong = b.strong
            out_reasons[-1] = boundary_reason
        else:
            out.append(b)
            out_reasons.append(boundary_reason)
    return out


@dataclass
class ReadabilityReport:
    """Честный отчёт о читаемости — без обещаний «всё идеально»."""

    fast_blocks: List[dict] = field(default_factory=list)   # не успевают прочитать
    long_blocks: List[dict] = field(default_factory=list)   # длиннее max_block_seconds
    low_confidence_blocks: List[dict] = field(default_factory=list)
    overlap_fixed: int = 0
    chars_per_second_warn: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fast_blocks": self.fast_blocks,
            "long_blocks": self.long_blocks,
            "low_confidence_blocks": self.low_confidence_blocks,
            "overlap_fixed": self.overlap_fixed,
            "chars_per_second_warn": self.chars_per_second_warn,
        }

    @property
    def has_issues(self) -> bool:
        return bool(
            self.fast_blocks or self.long_blocks or self.low_confidence_blocks or self.overlap_fixed
        )


def check_readability(
    blocks: Sequence[SubtitleBlock],
    settings: SegmentationSettings,
    fps: float = 0.0,
    confidence_threshold: float = 0.45,
) -> ReadabilityReport:
    """Найти блоки, которые стоит поправить вручную.

    Мы НЕ правим их автоматически: решение о тексте принимает человек.
    """
    rep = ReadabilityReport(chars_per_second_warn=settings.chars_per_second_warn)
    for idx, b in enumerate(blocks):
        plain = b.text.replace("\n", " ").strip()
        dur = b.duration
        if dur > 0 and plain:
            cps = len(plain) / dur
            if cps > settings.chars_per_second_warn:
                rep.fast_blocks.append(
                    {
                        "index": idx,
                        "in": round(b.in_time, 3),
                        "out": round(b.out_time, 3),
                        "chars_per_second": round(cps, 1),
                        "text": plain[:80],
                    }
                )
        if dur > settings.max_block_seconds + 1e-6:
            rep.long_blocks.append(
                {"index": idx, "in": round(b.in_time, 3), "duration": round(dur, 2)}
            )
        probs = [w.probability for w in b.words if w.probability is not None]
        if probs and (sum(probs) / len(probs)) < confidence_threshold:
            rep.low_confidence_blocks.append(
                {
                    "index": idx,
                    "in": round(b.in_time, 3),
                    "confidence": round(sum(probs) / len(probs), 3),
                    "text": plain[:80],
                }
            )
    return rep


def extend_holds(blocks: Sequence[SubtitleBlock], settings: SegmentationSettings) -> int:
    """Добавить «хвост» к концу блока, чтобы текст не исчезал со звуком.

    Хвост не выходит за начало следующего блока (иначе будет пересечение)
    и не длиннее max_hold_seconds.
    Возвращает число удлинённых блоков.
    """
    blocks = list(blocks)
    n = 0
    hold = clamp(settings.hold_seconds, 0.0, settings.max_hold_seconds)
    if hold <= 0:
        return 0
    for i, b in enumerate(blocks):
        limit = blocks[i + 1].in_time if i + 1 < len(blocks) else float("inf")
        target = b.out_time + hold
        new_out = min(target, limit)
        if new_out > b.out_time + 1e-6:
            b.out_time = new_out
            n += 1
    return n


def frames_to_seconds_safe(frames: float, fps: float) -> float:
    return frames_to_seconds(frames, fps) if fps > 0 else 0.0


def optional(value: Optional[str]) -> str:
    return value or ""
