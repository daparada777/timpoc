"""
Тесты SRT: экспорт, импорт, кодировки, повреждённые файлы.
"""

import pytest

from timpoc.errors import CorruptFileError
from timpoc.srt import (
    parse_srt,
    read_srt,
    srt_blocks_to_words,
    to_srt,
    write_srt,
)
from timpoc.types import SubtitleBlock


def _block(a, b, text):
    return SubtitleBlock(in_time=a, out_time=b, text=text)


def test_to_srt_format_is_standard():
    text = to_srt([_block(0.0, 1.5, "Первая строка"), _block(2.0, 3.0, "Вторая")])
    lines = text.strip().split("\n")
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:01,500"
    assert lines[2] == "Первая строка"
    assert lines[3] == ""
    assert lines[4] == "2"


def test_to_srt_skips_empty_and_zero_duration():
    blocks = [_block(0.0, 1.0, "ок"), _block(2.0, 2.0, "нулевая"), _block(3.0, 4.0, "  ")]
    text = to_srt(blocks)
    assert text.count("-->") == 1


def test_to_srt_sorts_by_time():
    text = to_srt([_block(5.0, 6.0, "позже"), _block(1.0, 2.0, "раньше")])
    assert text.index("раньше") < text.index("позже")


def test_to_srt_empty_input():
    assert to_srt([]) == ""


def test_multiline_block_keeps_newlines():
    text = to_srt([_block(0.0, 2.0, "Строка раз\nСтрока два")])
    assert "Строка раз\nСтрока два" in text


def test_parse_round_trip():
    blocks = [_block(0.0, 1.5, "Первая"), _block(2.25, 4.0, "Вторая\nстрока")]
    parsed = parse_srt(to_srt(blocks))
    assert len(parsed) == 2
    assert parsed[0].in_time == pytest.approx(0.0)
    assert parsed[0].out_time == pytest.approx(1.5)
    assert parsed[1].text == "Вторая\nстрока"


def test_parse_tolerates_crlf_and_missing_trailing_newline():
    text = "1\r\n00:00:00,000 --> 00:00:01,000\r\nТекст"
    blocks = parse_srt(text)
    assert len(blocks) == 1
    assert blocks[0].text == "Текст"


def test_parse_tolerates_dot_milliseconds():
    blocks = parse_srt("1\n00:00:00.500 --> 00:00:01.500\nТекст\n")
    assert blocks[0].in_time == pytest.approx(0.5)


def test_parse_skips_garbage_lines():
    text = "мусор\nещё мусор\n1\n00:00:01,000 --> 00:00:02,000\nПолезный текст\n"
    blocks = parse_srt(text)
    assert len(blocks) == 1
    assert blocks[0].text == "Полезный текст"


def test_parse_raises_when_no_subtitles():
    with pytest.raises(CorruptFileError):
        parse_srt("просто текст без таймингов")


def test_parse_raises_on_empty():
    with pytest.raises(CorruptFileError):
        parse_srt("")


def test_write_and_read_file(tmp_path):
    path = str(tmp_path / "подпапка с пробелами" / "тест.srt")
    write_srt(path, [_block(0.0, 1.0, "Проверка кириллицы")])
    blocks = read_srt(path)
    assert len(blocks) == 1
    assert blocks[0].text == "Проверка кириллицы"


def test_read_cp1251_file(tmp_path):
    """Русские SRT из старых программ часто в cp1251 — не должно быть кракозябр."""
    path = tmp_path / "win.srt"
    path.write_bytes("1\n00:00:00,000 --> 00:00:01,000\nПривет мир\n".encode("cp1251"))
    blocks = read_srt(str(path))
    assert blocks[0].text == "Привет мир"


def test_read_utf8_bom_file(tmp_path):
    path = tmp_path / "bom.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nПривет\n", encoding="utf-8-sig")
    assert read_srt(str(path))[0].text == "Привет"


def test_read_missing_file_gives_clear_error(tmp_path):
    from timpoc.errors import TimpocError

    with pytest.raises(TimpocError) as exc:
        read_srt(str(tmp_path / "нет.srt"))
    assert "не найден" in exc.value.message.lower()


def test_read_empty_file_is_corrupt(tmp_path):
    path = tmp_path / "empty.srt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(CorruptFileError):
        read_srt(str(path))


def test_blocks_to_words_spread_over_block():
    words = srt_blocks_to_words([_block(1.0, 3.0, "раз два три четыре")])
    assert len(words) == 4
    assert words[0].start == pytest.approx(1.0)
    assert words[-1].end == pytest.approx(3.0)
    # Слова идут подряд без разрывов и пересечений.
    for a, b in zip(words, words[1:]):
        assert a.end == pytest.approx(b.start)
