"""
timpoc — экспорт и импорт SRT.

Зачем это нужно, если цель — субтитры внутри After Effects:
 1) Страховка: даже если применение к композиции не удалось, работа не пропала.
 2) Совместимость: SRT открывает любой плеер и монтажная программа.
 3) Отладка: по SRT видно тайминги «глазами», без запуска AE.

Формат SRT строго фиксирован: номер, время через запятую, текст, пустая строка.
Кодировка UTF-8 (с BOM — так надёжнее открывается в старых плеерах Windows).
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from .errors import CorruptFileError, TimpocError, wrap_unknown
from .timing import format_srt_time, parse_srt_time
from .types import SubtitleBlock, Word

_TIME_LINE = re.compile(
    r"^\s*(?P<a>\d{1,2}:\d{2}:\d{2}[,.;]\d{1,3})\s*-->\s*(?P<b>\d{1,2}:\d{2}:\d{2}[,.;]\d{1,3})\s*$"
)


def to_srt(blocks: List[SubtitleBlock]) -> str:
    """Собрать текст SRT. Пустые блоки пропускаются, нумерация сплошная."""
    out: List[str] = []
    index = 0
    for b in sorted(blocks, key=lambda x: (x.in_time, x.out_time)):
        text = (b.text or "").strip()
        if not text:
            continue
        if b.out_time <= b.in_time:
            continue
        index += 1
        out.append(str(index))
        out.append(f"{format_srt_time(b.in_time)} --> {format_srt_time(b.out_time)}")
        out.append(text)
        out.append("")
    return "\n".join(out) + ("\n" if out else "")


def write_srt(path: str, blocks: List[SubtitleBlock], with_bom: bool = True) -> str:
    """Записать SRT на диск. Возвращает путь."""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        encoding = "utf-8-sig" if with_bom else "utf-8"
        with open(path, "w", encoding=encoding, newline="\r\n") as fh:
            fh.write(to_srt(blocks))
        return path
    except PermissionError as exc:
        raise TimpocError(
            "Не удалось записать файл SRT: нет доступа.",
            hint="Выберите другую папку или закройте файл, если он открыт в редакторе.",
            details=str(exc),
        ) from exc
    except OSError as exc:
        raise wrap_unknown(exc) from exc


def parse_srt(text: str) -> List[SubtitleBlock]:
    """Разобрать SRT в блоки.

    Намеренно «прощающий» парсер: принимает CRLF/LF, пустые строки,
    точку или запятую в миллисекундах, отсутствие завершающего перевода строки.
    """
    blocks: List[SubtitleBlock] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        m = _TIME_LINE.match(line)
        if not m:
            # Это мог быть номер или мусор — ищем строку времени дальше.
            i += 1
            continue
        try:
            start = parse_srt_time(m.group("a"))
            end = parse_srt_time(m.group("b"))
        except ValueError:
            i += 1
            continue
        i += 1
        body: List[str] = []
        while i < n and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        text_block = "\n".join(body).strip()
        if text_block and end > start:
            blocks.append(SubtitleBlock(in_time=start, out_time=end, text=text_block))
    if not blocks:
        raise CorruptFileError(
            "<srt>", "не найдено ни одного субтитра с корректным временем"
        )
    blocks.sort(key=lambda b: (b.in_time, b.out_time))
    return blocks


def read_srt(path: str, encoding: Optional[str] = None) -> List[SubtitleBlock]:
    """Прочитать SRT с диска с угадыванием кодировки (UTF-8 / UTF-8 BOM / cp1251).

    Русские SRT из старых программ часто приходят в cp1251 — молча получать
    «кракозябры» недопустимо, поэтому пробуем несколько кодировок.
    """
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError as exc:
        raise TimpocError(
            "Файл SRT не найден.",
            hint="Проверьте путь к файлу.",
            details=str(exc),
        ) from exc
    except OSError as exc:
        raise wrap_unknown(exc) from exc

    if not raw.strip():
        raise CorruptFileError(path, "файл пуст")

    candidates = [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1251"]
    text: Optional[str] = None
    for enc in candidates:
        if not enc:
            continue
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise CorruptFileError(path, "не удалось определить кодировку текста")

    return parse_srt(text)


def srt_blocks_to_words(blocks: List[SubtitleBlock]) -> List[Word]:
    """Сделать из SRT-блоков приближённые слова.

    Нужно, чтобы импортированный SRT можно было переразбивать на фразы.
    Времена слов распределяются равномерно внутри блока — это приближение,
    и мы не выдаём его за точное выравнивание по аудио.
    """
    words: List[Word] = []
    for b in blocks:
        tokens = b.text.replace("\n", " ").split()
        if not tokens:
            continue
        dur = max(0.0, b.out_time - b.in_time)
        step = dur / len(tokens) if len(tokens) else dur
        for k, tok in enumerate(tokens):
            words.append(
                Word(
                    text=tok,
                    start=b.in_time + k * step,
                    end=b.in_time + (k + 1) * step,
                    probability=None,
                )
            )
    return words
