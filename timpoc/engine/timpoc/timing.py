"""
timpoc — работа со временем: кадры, смещения, сдвиги.

Здесь живут все вычисления, из-за которых субтитры «плывут». Три правила:

1) Движок распознавания отдаёт время в секундах ОТ НАЧАЛА АУДИОФЛАЙЛА.
   Композиция почти всегда начинается не с нуля аудиодорожки, поэтому
   всегда прибавляем смещение источника (см. compute_audio_offset).

2) Храним и отдаём секунды (float), но в After Effects пишем значения,
   выровненные по кадру. Иначе на 23.976/29.97 fps появляются «прыгающие»
   границы. Выравнивание — snap_to_frame().

3) Общий сдвиг таймингов должен быть обратимым и не накапливать ошибку
   при многократном применении. Поэтому сдвиг считаем от сохранённого
   base_offset, а не «ещё раз поверх предыдущего».
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .types import SubtitleBlock, Transcript


def clamp(value: float, lo: float, hi: float) -> float:
    """Ограничить значение. hi < lo считаем ошибкой вызывающего и возвращаем lo."""
    if hi < lo:
        return lo
    return max(lo, min(hi, value))


def seconds_to_frames(seconds: float, fps: float) -> int:
    """Секунды → номер кадра (округление к ближайшему кадру).

    round(), а не int(): int() всегда округляет вниз и на длинных
    проектах даёт устойчивое отставание на один кадр.
    """
    if fps <= 0:
        raise ValueError("fps должен быть больше нуля")
    return int(round(seconds * fps))


def frames_to_seconds(frames: float, fps: float) -> float:
    if fps <= 0:
        raise ValueError("fps должен быть больше нуля")
    return float(frames) / float(fps)


def snap_to_frame(seconds: float, fps: float) -> float:
    """Привести время к границе кадра."""
    return frames_to_seconds(seconds_to_frames(seconds, fps), fps)


def format_timecode(seconds: float, fps: float = 25.0, drop_frame: bool = False) -> str:
    """Время → строка таймкода HH:MM:SS:FF (или HH:MM:SS;FF для drop-frame).

    Используется в панели и в SRT (там всегда миллисекунды — см. format_srt_time).
    """
    if seconds < 0:
        return "-" + format_timecode(-seconds, fps, drop_frame)
    fps_int = int(round(fps)) if fps > 0 else 25
    total_frames = int(round(seconds * fps_int))
    frames = total_frames % fps_int
    total_seconds = total_frames // fps_int
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    sep = ";" if drop_frame else ":"
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{frames:02d}"


def format_srt_time(seconds: float) -> str:
    """Формат времени SRT: HH:MM:SS,mmm (запятая — часть стандарта SRT)."""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_time(text: str) -> float:
    """Обратно из HH:MM:SS,mmm (принимаем точку и точку с запятой вместо запятой).

    Запятая — часть стандарта SRT, поэтому её обязательно приводим к «:».
    Без этого парсер падал на собственных же строках времени.
    """
    t = text.strip().replace(";", ":").replace(",", ":").replace(".", ":")
    parts = t.split(":")
    if len(parts) != 4:
        raise ValueError(f"Не удалось разобрать время SRT: {text!r}")
    h, m, s, ms = (int(p) for p in parts)
    return h * 3600 + m * 60 + s + ms / 1000.0


# --------------------------------------------------------------------------
# Смещения: где в композиции начинается распознаваемое аудио
# --------------------------------------------------------------------------

def compute_audio_offset(
    kind: str,
    range_start: float = 0.0,
    layer_start: float = 0.0,
    layer_in_point: float = 0.0,
    stretch: float = 1.0,
) -> Tuple[float, float]:
    """Вернуть (смещение, масштаб) для перехода «время в аудио» → «время в композиции».

    Формула: comp_time = range_start + layer_start + audio_time * stretch

    kind:
      "comp"   — распознаём звук композиции, выведенный с range_start.
                 Слои внутри уже разведены самим After Effects, поэтому
                 дополнительно сдвигать не нужно: offset = range_start.
      "layer"  — распознаём дорожку одного слоя. Аудио слоя начинается
                 в layer_start, а внутри файла слой может быть подрезан
                 (in_point) и растянут (stretch).
      "file"   — внешний аудиофайл, наложенный на композицию с range_start.

    Возвращаем кортеж, потому что при stretch != 1 одного смещения мало:
    нужно ещё масштабировать длительности.
    """
    if kind == "comp":
        return float(range_start), 1.0
    if kind == "file":
        return float(range_start), 1.0
    if kind == "layer":
        if stretch <= 0:
            raise ValueError("stretch должен быть больше нуля")
        return float(layer_start), float(stretch)
    raise ValueError(f"Неизвестный тип источника аудио: {kind!r}")


def map_audio_time_to_comp(
    audio_time: float, offset: float, scale: float = 1.0
) -> float:
    """Применить формулу из compute_audio_offset к одному моменту."""
    return offset + audio_time * scale


# --------------------------------------------------------------------------
# Сдвиг и обрезка
# --------------------------------------------------------------------------

def shift_transcript(transcript: Transcript, delta: float, fps: float = 0.0) -> Transcript:
    """Общий сдвиг таймингов на delta секунд (положительный — вперёд/позже).

    Важно про накапливание ошибки:
      мы храним base_offset и вычисляем новые границы как
          (исходная граница - base_offset) + (base_offset + delta),
      то есть фактически просто прибавляем delta один раз к текущим значениям,
      но записываем новый base_offset. Это делает сдвиг обратимым:
      shift(-delta) возвращает ровно исходные значения (в пределах float),
      и повторные сдвиги не «съедают» точность из-за многократного округления.

    fps > 0 — дополнительно выровнять по кадру.
    """
    new_offset = transcript.base_offset + float(delta)
    for b in transcript.blocks:
        b.in_time += float(delta)
        b.out_time += float(delta)
        if b.in_time < 0:
            b.in_time = 0.0
            b.out_time = max(b.out_time, b.in_time + 1e-3)
        if fps > 0:
            b.in_time = snap_to_frame(b.in_time, fps)
            b.out_time = snap_to_frame(b.out_time, fps)
            if b.out_time <= b.in_time:
                b.out_time = b.in_time + frames_to_seconds(1.0, fps)
    transcript.base_offset = new_offset
    return transcript


def clamp_to_range(transcript: Transcript, start: float, end: float) -> int:
    """Обрезать блоки по диапазону. Возвращает число затронутых блоков.

    Полностью внешние блоки удаляются; частично внешние — обрезаются.
    Нужен при работе «только с выделенным фрагментом», чтобы субтитры
    не вылезали за рабочую область.
    """
    kept: List[SubtitleBlock] = []
    touched = 0
    for b in transcript.blocks:
        if b.out_time <= start or b.in_time >= end:
            touched += 1
            continue
        new_in = max(b.in_time, start)
        new_out = min(b.out_time, end)
        if new_in != b.in_time or new_out != b.out_time:
            touched += 1
        b.in_time = new_in
        b.out_time = max(new_out, new_in + 1e-3)
        kept.append(b)
    transcript.blocks = kept
    return touched


def resolve_overlaps(blocks: Iterable[SubtitleBlock], fps: float = 0.0) -> int:
    """Убрать пересечения соседних блоков. Возвращает число исправленных.

    Политика: если следующий блок начинается раньше, чем заканчивается
    предыдущий, делим спорный промежуток пополам. Это предсказуемо и
    не «съедает» текст. Закреплённые (locked) блоки не двигаем — вместо
    этого укорачиваем соседа.
    """
    blocks = list(blocks)
    blocks.sort(key=lambda b: (b.in_time, b.out_time))
    fixed = 0
    min_dur = frames_to_seconds(1.0, fps) if fps > 0 else 0.04
    for prev, cur in zip(blocks, blocks[1:]):
        if cur.in_time >= prev.out_time - 1e-6:
            continue
        fixed += 1
        if prev.locked and cur.locked:
            # Оба закреплены — укорачиваем более поздний, иначе не разъехать.
            cur.in_time = prev.out_time
        elif prev.locked:
            cur.in_time = prev.out_time
        elif cur.locked:
            prev.out_time = cur.in_time
        else:
            mid = (prev.out_time + cur.in_time) / 2.0
            prev.out_time = mid
            cur.in_time = mid
        if prev.out_time - prev.in_time < min_dur:
            prev.out_time = prev.in_time + min_dur
        if cur.out_time - cur.in_time < min_dur:
            cur.out_time = cur.in_time + min_dur
    return fixed


def enforce_min_duration(blocks: Iterable[SubtitleBlock], min_seconds: float) -> int:
    """Не дать блоку быть короче min_seconds (иначе текст мелькает).

    Удлиняем вперёд; пересечения после этого убирает resolve_overlaps().
    """
    n = 0
    for b in blocks:
        if b.duration < min_seconds:
            b.out_time = b.in_time + min_seconds
            n += 1
    return n
