"""
Тесты разбиения на фразы.

Это самая «ремесленная» часть: здесь решается, где зритель увидит границу
субтитра. Проверяем конкретные сценарии из требований:
паузы, пунктуация, лимиты слов/символов/строк, склейка коротких блоков.
"""

import pytest

from timpoc.segmentation import (
    SegmentationSettings,
    build_blocks,
    check_readability,
    extend_holds,
    wrap_words_to_lines,
)
from timpoc.types import Word


def _words(spec):
    """spec: список кортежей (текст, start, end)."""
    return [Word(text=t, start=a, end=b) for (t, a, b) in spec]


def _settings(**kw):
    return SegmentationSettings(**kw)


# ------------------------------------------------------------------- базовое

def test_empty_input_gives_no_blocks():
    assert build_blocks([], _settings()) == []


def test_whitespace_words_are_ignored():
    words = _words([("привет", 0.0, 0.5), ("   ", 0.5, 0.6), ("мир", 0.6, 1.0)])
    blocks = build_blocks(words, _settings())
    assert len(blocks) == 1
    assert blocks[0].text == "привет мир"


def test_block_times_follow_words():
    words = _words([("раз", 1.0, 1.4), ("два", 1.5, 2.0)])
    blocks = build_blocks(words, _settings())
    assert blocks[0].in_time == pytest.approx(1.0)
    assert blocks[0].out_time == pytest.approx(2.0)


def test_words_sorted_even_if_input_unsorted():
    words = _words([("два", 1.5, 2.0), ("раз", 1.0, 1.4)])
    blocks = build_blocks(words, _settings())
    assert blocks[0].text == "раз два"


# ------------------------------------------------------------------- пунктуация

def test_sentence_end_forces_split():
    words = _words(
        [
            ("Привет.", 0.0, 0.6),
            ("Это", 1.0, 1.3),
            ("второе", 1.4, 1.8),
            ("предложение.", 1.9, 2.6),
        ]
    )
    blocks = build_blocks(words, _settings())
    assert len(blocks) == 2
    assert blocks[0].text == "Привет."
    assert blocks[1].text == "Это второе предложение."


def test_question_and_exclamation_split():
    """Вопрос и восклицание — самостоятельные блоки, «Конечно» к ним не приклеивается."""
    words = _words([("Правда?", 0.0, 0.5), ("Да!", 1.0, 1.3), ("Конечно", 1.35, 2.0)])
    blocks = build_blocks(words, _settings())
    texts = [b.text for b in blocks]
    assert texts[0] == "Правда?"
    assert texts[1] == "Да!"
    assert len(blocks) == 3


# ------------------------------------------------------------------- паузы

def test_long_pause_forces_split_without_punctuation():
    """Пауза 1 с между словами — граница, даже без точки."""
    words = _words(
        [
            ("первая", 0.0, 0.5),
            ("часть", 0.6, 1.0),
            ("вторая", 2.0, 2.5),
            ("часть", 2.6, 3.0),
        ]
    )
    blocks = build_blocks(words, _settings(pause_split_seconds=0.55))
    assert len(blocks) == 2


def test_short_pause_keeps_one_block():
    words = _words([("раз", 0.0, 0.5), ("два", 0.6, 1.0), ("три", 1.1, 1.5)])
    blocks = build_blocks(words, _settings())
    assert len(blocks) == 1


def test_pause_split_can_be_disabled():
    words = _words([("раз", 0.0, 0.5), ("два", 3.0, 3.5)])
    assert len(build_blocks(words, _settings(break_on_pause=False))) == 1
    assert len(build_blocks(words, _settings(break_on_pause=True))) == 2


# ------------------------------------------------------------------- лимиты

def test_max_chars_limit_splits_block():
    words = _words([(f"слово{i}", i * 0.3, i * 0.3 + 0.25) for i in range(12)])
    blocks = build_blocks(words, _settings(max_chars=20, max_words=20, pause_split_seconds=99))
    assert len(blocks) > 1
    for b in blocks:
        assert len(b.text.replace("\n", " ")) <= 22  # небольшой запас на перенос строки


def test_max_words_limit_splits_block():
    words = _words([(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(10)])
    blocks = build_blocks(words, _settings(max_words=4, max_chars=500, pause_split_seconds=99))
    for b in blocks:
        assert len(b.words) <= 4


def test_single_giant_word_is_not_dropped():
    """Одно слово длиннее лимита нельзя «разделить» — отдаём как есть."""
    words = _words([("оченьдлинноесловобезпробелов", 0.0, 1.0)])
    blocks = build_blocks(words, _settings(max_chars=10))
    assert len(blocks) == 1
    assert blocks[0].text.startswith("оченьдлинное")


def test_max_block_duration_forces_split():
    words = _words([(f"слово{i}", i * 1.2, i * 1.2 + 0.9) for i in range(8)])
    blocks = build_blocks(words, _settings(max_block_seconds=4.0, pause_split_seconds=99, max_words=99, max_chars=999))
    assert len(blocks) > 1
    for b in blocks:
        assert b.duration <= 4.0 + 1.5  # последнее слово окна может чуть выходить


# ------------------------------------------------------------------- переносы

def test_wrap_respects_words_per_line():
    words = _words([(f"слово{i}", i * 0.2, i * 0.2 + 0.15) for i in range(6)])
    text = wrap_words_to_lines(words, _settings(max_words_per_line=3, max_chars_per_line=100))
    lines = text.split("\n")
    assert len(lines) == 2
    assert len(lines[0].split()) == 3


def test_wrap_keeps_punctuation_with_word():
    """Запятая не должна остаться в начале следующей строки."""
    words = _words(
        [
            ("первое", 0.0, 0.2),
            ("второе,", 0.2, 0.4),
            ("третье", 0.4, 0.6),
            ("четвёртое", 0.6, 0.8),
        ]
    )
    text = wrap_words_to_lines(words, _settings(max_words_per_line=2, max_chars_per_line=100))
    for line in text.split("\n"):
        assert not line.startswith(",")


def test_wrap_hanging_preposition_moves_down():
    """Предлог «и» не должен оставаться один в конце строки."""
    words = _words([("раз", 0, 0.1), ("два", 0.1, 0.2), ("и", 0.2, 0.3), ("три", 0.3, 0.4)])
    text = wrap_words_to_lines(words, _settings(max_words_per_line=3, max_chars_per_line=100))
    lines = text.split("\n")
    assert lines[0].split()[-1] != "и"


# ------------------------------------------------------------------- склейка

def test_short_blocks_are_merged():
    """Короткие фразы по 0.2 с без знаков препинания — мельтешение; склеиваются."""
    words = _words(
        [
            ("раз", 0.0, 0.2),
            ("два", 1.0, 1.2),
            ("три", 2.0, 2.2),
        ]
    )
    # pause_split_seconds=99 — отключаем влияние пауз, проверяем именно склейку коротких.
    blocks = build_blocks(
        words, _settings(min_block_seconds=0.6, merge_short_blocks=True, pause_split_seconds=99)
    )
    assert len(blocks) == 1
    assert blocks[0].text.replace("\n", " ") == "раз два три"


def test_sentence_marks_block_the_merge():
    """Точки не дают склеить блоки: «раз. два.» остаётся раздельным.

    Это осознанное решение, а не баг: склейка через точку ломает интонацию.
    В панели есть ручное «Объединить», если пользователю нужно иначе.
    """
    words = _words([("раз.", 0.0, 0.2), ("два.", 1.0, 1.2)])
    blocks = build_blocks(words, _settings(min_block_seconds=0.6))
    assert len(blocks) == 2


def test_pause_blocks_are_not_merged_across():
    """Через паузу не склеиваем, даже если блоки короткие."""
    words = _words([("раз", 0.0, 0.2), ("два", 3.0, 3.2)])
    blocks = build_blocks(words, _settings(min_block_seconds=0.6, pause_split_seconds=0.55))
    assert len(blocks) == 2


def test_small_gap_still_merges():
    """Маленький разрыв (< порога паузы) склейке не мешает."""
    words = _words([("раз", 0.0, 0.2), ("два", 0.4, 0.6), ("три", 0.8, 1.0)])
    blocks = build_blocks(words, _settings(min_block_seconds=0.6, pause_split_seconds=0.55))
    assert len(blocks) == 1


def test_merge_respects_limits():
    """Склеиваем, только пока влезает в лимиты."""
    words = _words(
        [
            ("длинноеслово1.", 0.0, 0.2),
            ("длинноеслово2.", 1.0, 1.2),
            ("длинноеслово3.", 2.0, 2.2),
        ]
    )
    blocks = build_blocks(words, _settings(max_chars=16, min_block_seconds=0.6))
    assert len(blocks) >= 2


def test_merge_can_be_disabled():
    words = _words([("раз.", 0.0, 0.2), ("два.", 1.0, 1.2)])
    assert len(build_blocks(words, _settings(merge_short_blocks=False))) == 2


def test_sentence_boundary_is_never_merged():
    """Точка — граница священна: короткое «Да.» не приклеивается к следующему."""
    words = _words(
        [
            ("Правда?", 0.0, 0.3),
            ("Да.", 1.0, 1.2),
            ("Конечно", 1.3, 1.6),
        ]
    )
    blocks = build_blocks(words, _settings(min_block_seconds=0.6, merge_short_blocks=True))
    assert "Правда?" in [b.text for b in blocks]
    assert len(blocks) >= 2


def test_merge_only_fights_flicker():
    """Склеиваем там, где мельтешение; нормально читаемые блоки не трогаем."""
    from timpoc.segmentation import _merge_short
    from timpoc.types import SubtitleBlock

    s = _settings(min_block_seconds=0.6, pause_split_seconds=99)

    def pair(first_dur, second_dur):
        a = SubtitleBlock(in_time=0.0, out_time=first_dur, text="раз",
                          words=[Word(text="раз", start=0.0, end=first_dur)])
        b = SubtitleBlock(in_time=first_dur + 0.1, out_time=first_dur + 0.1 + second_dur,
                          text="два", words=[Word(text="два", start=first_dur + 0.1,
                                                 end=first_dur + 0.1 + second_dur)])
        return [a, b]

    # Оба короче порога → мельтешение, склеиваем.
    assert len(_merge_short(pair(0.2, 0.2), s, ["overflow", "end"])) == 1
    # Оба читаются нормально → не трогаем, склейка не нужна.
    assert len(_merge_short(pair(1.0, 1.0), s, ["overflow", "end"])) == 2
    # Один короткий, но вместе получится длинный блок → не склеиваем.
    assert len(_merge_short(pair(2.0, 0.2), s, ["overflow", "end"])) == 2


def test_merge_respects_screen_time_not_just_speech():
    """Блок, который висит на экране долго, коротким не считается.

    Слова короткие, но суммарно субтитр занимал бы 2.3 с — склейка недопустима.
    """
    from timpoc.segmentation import _merge_short
    from timpoc.types import SubtitleBlock

    s = _settings(min_block_seconds=0.6, pause_split_seconds=99)
    a = SubtitleBlock(in_time=0.0, out_time=0.1, text="а", words=[Word(text="а", start=0.0, end=0.1)])
    b = SubtitleBlock(in_time=2.2, out_time=2.3, text="б", words=[Word(text="б", start=2.2, end=2.3)])
    assert len(_merge_short([a, b], s, ["overflow", "end"])) == 2


def test_merge_blocked_by_sentence_reason():
    """Граница «конец предложения» склейку запрещает всегда.

    reasons[1] — причина границы ПЕРЕД вторым блоком, то есть как раз той,
    через которую идёт склейка.
    """
    from timpoc.segmentation import _merge_short
    from timpoc.types import SubtitleBlock

    s = _settings(min_block_seconds=0.6, pause_split_seconds=99)
    a = SubtitleBlock(in_time=0.0, out_time=0.2, text="раз.", words=[Word(text="раз.", start=0.0, end=0.2)])
    b = SubtitleBlock(in_time=0.3, out_time=0.5, text="два.", words=[Word(text="два.", start=0.3, end=0.5)])
    assert len(_merge_short([a, b], s, ["start", "sentence"])) == 2


def test_merge_blocked_by_pause_reason():
    """Граница «пауза» склейку запрещает: субтитр не должен висеть через тишину."""
    from timpoc.segmentation import _merge_short
    from timpoc.types import SubtitleBlock

    s = _settings(min_block_seconds=0.6, pause_split_seconds=99)
    a = SubtitleBlock(in_time=0.0, out_time=0.2, text="раз", words=[Word(text="раз", start=0.0, end=0.2)])
    b = SubtitleBlock(in_time=0.3, out_time=0.5, text="два", words=[Word(text="два", start=0.3, end=0.5)])
    assert len(_merge_short([a, b], s, ["start", "pause"])) == 2


def test_merge_blocked_by_real_silence_gap():
    """Даже без явной паузы большой разрыв во времени склейку запрещает."""
    from timpoc.segmentation import _merge_short
    from timpoc.types import SubtitleBlock

    s = _settings(min_block_seconds=0.6, pause_split_seconds=0.55)
    a = SubtitleBlock(in_time=0.0, out_time=0.2, text="раз", words=[Word(text="раз", start=0.0, end=0.2)])
    b = SubtitleBlock(in_time=4.0, out_time=4.2, text="два", words=[Word(text="два", start=4.0, end=4.2)])
    assert len(_merge_short([a, b], s, ["overflow", "end"])) == 2


# ------------------------------------------------------------------- хвосты

def test_extend_holds_adds_tail_but_not_past_next():
    from timpoc.types import SubtitleBlock

    blocks = [
        SubtitleBlock(in_time=0.0, out_time=1.0, text="а"),
        SubtitleBlock(in_time=1.1, out_time=2.0, text="б"),
    ]
    extend_holds(blocks, _settings(hold_seconds=0.5, max_hold_seconds=1.0))
    assert blocks[0].out_time == pytest.approx(1.1)  # упёрся в следующий
    assert blocks[1].out_time == pytest.approx(2.5)  # последний — свободно


def test_hold_zero_changes_nothing():
    from timpoc.types import SubtitleBlock

    blocks = [SubtitleBlock(in_time=0.0, out_time=1.0, text="а")]
    assert extend_holds(blocks, _settings(hold_seconds=0.0)) == 0


# ------------------------------------------------------------------- читаемость

def test_readability_flags_too_fast_block():
    from timpoc.types import SubtitleBlock

    blocks = [SubtitleBlock(in_time=0.0, out_time=0.4, text="это очень длинный текст для такого короткого времени")]
    rep = check_readability(blocks, _settings(chars_per_second_warn=18.0))
    assert len(rep.fast_blocks) == 1
    assert rep.has_issues


def test_readability_flags_low_confidence():
    from timpoc.types import SubtitleBlock

    words = [Word(text="слово", start=0.0, end=0.5, probability=0.2)]
    blocks = [SubtitleBlock(in_time=0.0, out_time=0.5, text="слово", words=words)]
    rep = check_readability(blocks, _settings())
    assert len(rep.low_confidence_blocks) == 1


def test_readability_clean_input_has_no_issues():
    from timpoc.types import SubtitleBlock

    blocks = [SubtitleBlock(in_time=0.0, out_time=3.0, text="Короткий текст")]
    rep = check_readability(blocks, _settings())
    assert not rep.has_issues


# ------------------------------------------------------------------- настройки

def test_settings_validate_warns_on_absurd_values():
    warns = _settings(max_lines=0, max_words=0, max_chars=3).validate()
    assert len(warns) >= 3


def test_settings_sanitized_fixes_values():
    s = _settings(max_lines=0, max_words=-1, max_chars=2).sanitized()
    assert s.max_lines == 1
    assert s.max_words == 1
    assert s.max_chars == 8
