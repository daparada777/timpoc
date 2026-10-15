"""
Тесты математики времени.

Здесь проверяются вещи, из-за которых субтитры «плывут»: округление к кадру,
смещение источника, обратимость общего сдвига, разборка пересечений.
"""

import math

import pytest

from timpoc.timing import (
    clamp,
    clamp_to_range,
    compute_audio_offset,
    enforce_min_duration,
    format_srt_time,
    format_timecode,
    frames_to_seconds,
    map_audio_time_to_comp,
    parse_srt_time,
    resolve_overlaps,
    seconds_to_frames,
    shift_transcript,
    snap_to_frame,
)
from timpoc.types import SubtitleBlock, Transcript, Word


def _block(a, b, text="тест", locked=False):
    return SubtitleBlock(in_time=a, out_time=b, text=text, locked=locked)


# ---------------------------------------------------------------- кадры

@pytest.mark.parametrize(
    "fps",
    [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0],
)
def test_round_trip_seconds_frames(fps):
    """сек → кадр → сек не должно уезжать больше чем на полкадра."""
    for seconds in (0.0, 0.5, 1.0, 3.7, 60.0, 3600.123):
        back = frames_to_seconds(seconds_to_frames(seconds, fps), fps)
        assert abs(back - seconds) <= (0.5 / fps) + 1e-9


def test_rounding_is_nearest_not_floor():
    """int() вместо round() давал бы устойчивое отставание на кадр."""
    # 0.9 кадра должно округлиться вверх, к кадру 1.
    assert seconds_to_frames(0.9 / 25.0, 25.0) == 1
    assert seconds_to_frames(0.4 / 25.0, 25.0) == 0


def test_long_video_no_frame_drift():
    """На длинном хронометраже ошибка не должна накапливаться."""
    fps = 29.97
    for t in (10.0, 600.0, 3600.0, 7200.5):
        snapped = snap_to_frame(t, fps)
        assert abs(snapped - t) <= (0.5 / fps) + 1e-6


def test_bad_fps_raises():
    with pytest.raises(ValueError):
        seconds_to_frames(1.0, 0)


# ---------------------------------------------------------------- таймкоды

def test_format_timecode_basic():
    assert format_timecode(0.0, 25) == "00:00:00:00"
    assert format_timecode(1.0, 25) == "00:00:01:00"
    assert format_timecode(3661.0, 25) == "01:01:01:00"


def test_srt_time_round_trip():
    for value in (0.0, 0.001, 1.5, 61.234, 3661.999):
        text = format_srt_time(value)
        assert "," in text  # стандарт SRT требует запятую
        assert abs(parse_srt_time(text) - value) <= 0.0005


def test_parse_srt_time_accepts_dot_and_semicolon():
    assert abs(parse_srt_time("00:00:01.500") - 1.5) < 1e-9
    assert abs(parse_srt_time("00:00:01;500") - 1.5) < 1e-9


def test_parse_srt_time_rejects_garbage():
    with pytest.raises(ValueError):
        parse_srt_time("не время")


# ---------------------------------------------------------------- смещения

def test_offset_for_comp_range():
    """Распознаём звук композиции с 10-й секунды → всё сдвигаем на 10."""
    offset, scale = compute_audio_offset("comp", range_start=10.0)
    assert offset == 10.0
    assert scale == 1.0
    assert map_audio_time_to_comp(5.0, offset, scale) == 15.0


def test_offset_for_layer_with_inpoint_and_start():
    """Слой начинается в композиции с 4 с; смещение слоя — именно оно."""
    offset, scale = compute_audio_offset("layer", layer_start=4.0, layer_in_point=2.0)
    assert offset == 4.0
    assert map_audio_time_to_comp(0.0, offset, scale) == 4.0


def test_offset_for_stretched_layer():
    """Растяжение 2x: секунда аудио = две секунды композиции."""
    offset, scale = compute_audio_offset("layer", layer_start=3.0, stretch=2.0)
    assert (offset, scale) == (3.0, 2.0)
    assert map_audio_time_to_comp(1.5, offset, scale) == 6.0


def test_unknown_source_kind_raises():
    with pytest.raises(ValueError):
        compute_audio_offset("magic")


# ---------------------------------------------------------------- сдвиг

def test_shift_is_reversible():
    """Сдвиг вперёд и обратно должен вернуть исходные тайминги."""
    tr = Transcript(blocks=[_block(1.0, 2.0), _block(5.0, 6.5)])
    original = [(b.in_time, b.out_time) for b in tr.blocks]

    shift_transcript(tr, 3.25)
    assert tr.blocks[0].in_time == pytest.approx(4.25)
    assert tr.base_offset == pytest.approx(3.25)

    shift_transcript(tr, -3.25)
    for (a, b), block in zip(original, tr.blocks):
        assert block.in_time == pytest.approx(a)
        assert block.out_time == pytest.approx(b)
    assert tr.base_offset == pytest.approx(0.0)


def test_repeated_shift_does_not_accumulate_error():
    """100 сдвигов по 0.1 вперёд и назад = исходные значения (без дрейфа)."""
    tr = Transcript(blocks=[_block(1.0, 2.0)])
    for _ in range(100):
        shift_transcript(tr, 0.1)
    for _ in range(100):
        shift_transcript(tr, -0.1)
    assert tr.blocks[0].in_time == pytest.approx(1.0, abs=1e-9)


def test_shift_never_goes_negative():
    tr = Transcript(blocks=[_block(0.5, 1.0)])
    shift_transcript(tr, -2.0)
    assert tr.blocks[0].in_time == 0.0
    assert tr.blocks[0].out_time > 0.0


def test_shift_snaps_to_frame():
    tr = Transcript(blocks=[_block(1.0, 2.0)])
    shift_transcript(tr, 0.013, fps=25.0)
    # 1.013 с → ближайший кадр 25 fps = 1.0
    assert tr.blocks[0].in_time == pytest.approx(1.0)


# ---------------------------------------------------------------- диапазоны

def test_clamp_to_range_removes_and_trims():
    """Один блок вне диапазона, два частично — их должно обрезать."""
    tr = Transcript(
        blocks=[_block(0.0, 1.0), _block(3.5, 6.0), _block(9.0, 10.0)]
    )
    touched = clamp_to_range(tr, 4.0, 9.5)
    assert touched == 3  # один удалён, два обрезаны
    assert [b.in_time for b in tr.blocks] == [4.0, 9.0]
    assert tr.blocks[0].out_time == pytest.approx(6.0)
    assert tr.blocks[1].out_time == pytest.approx(9.5)


def test_clamp_to_range_keeps_inside_blocks_untouched():
    tr = Transcript(blocks=[_block(4.5, 6.0)])
    assert clamp_to_range(tr, 4.0, 9.5) == 0
    assert tr.blocks[0].in_time == pytest.approx(4.5)


# ---------------------------------------------------------------- пересечения

def test_resolve_overlaps_splits_difference():
    blocks = [_block(0.0, 2.0), _block(1.0, 3.0)]
    fixed = resolve_overlaps(blocks)
    assert fixed == 1
    assert blocks[0].out_time == pytest.approx(1.5)
    assert blocks[1].in_time == pytest.approx(1.5)


def test_resolve_overlaps_respects_locked():
    blocks = [_block(0.0, 2.0, locked=True), _block(1.0, 3.0)]
    resolve_overlaps(blocks)
    assert blocks[0].out_time == pytest.approx(2.0)  # закреплённый не двигаем
    assert blocks[1].in_time == pytest.approx(2.0)


def test_resolve_overlaps_no_change_when_disjoint():
    blocks = [_block(0.0, 1.0), _block(2.0, 3.0)]
    assert resolve_overlaps(blocks) == 0


def test_enforce_min_duration():
    blocks = [_block(0.0, 0.1), _block(2.0, 3.0)]
    assert enforce_min_duration(blocks, 0.5) == 1
    assert blocks[0].duration == pytest.approx(0.5)


def test_clamp_helper():
    assert clamp(5, 0, 3) == 3
    assert clamp(-5, 0, 3) == 0
    assert clamp(5, 3, 0) == 3  # некорректный диапазон не роняет вызов
