"""
Тесты оформления: цвета, диапазоны, регистр, пресеты.

Отдельно проверяем требование «оформление не влияет на распознавание»:
TextStyle не содержит ни одного поля, связанного с таймингами.
"""

import json

import pytest

from timpoc.style import (
    BUILTIN_PRESETS,
    TextStyle,
    builtin_presets,
    delete_preset,
    list_user_presets,
    load_preset,
    normalize_color,
    save_preset,
    to_ae_color,
    to_css,
)


# ------------------------------------------------------------------- цвета

@pytest.mark.parametrize(
    "value,expected",
    [
        ("#FFFFFF", [255, 255, 255]),
        ("#000000", [0, 0, 0]),
        ("#ff8800", [255, 136, 0]),
        ("#f80", [255, 136, 0]),
        ("#FF8800CC", [255, 136, 0]),  # альфа хранится отдельно
        ("rgb(12, 34, 56)", [12, 34, 56]),
        ([255, 0, 128], [255, 0, 128]),
        ([1.0, 0.0, 0.5], [255, 0, 128]),  # формат ExtendScript 0..1
        ((10, 20, 30), [10, 20, 30]),
        (None, [255, 255, 255]),
        ("бред", [255, 255, 255]),  # не роняем панель на мусоре
    ],
)
def test_normalize_color(value, expected):
    assert normalize_color(value) == expected


def test_normalize_color_clamps_out_of_range():
    assert normalize_color([900, -50, 30]) == [255, 0, 30]


def test_to_ae_color_is_zero_to_one():
    assert to_ae_color("#FF0000") == [1.0, 0.0, 0.0]


def test_to_css_has_alpha():
    assert to_css([255, 0, 0], 0.5) == "rgba(255, 0, 0, 0.500)"


# ------------------------------------------------------------------- валидация

def test_sanitize_fixes_absurd_values():
    st = TextStyle(font_size=-50, opacity=3.0, line_height=0.01, stroke_width=-4)
    fixed, warns = st.sanitize()
    assert fixed.font_size == 1
    assert fixed.opacity == 1.0
    assert fixed.line_height == 0.2
    assert fixed.stroke_width == 0
    assert warns  # пользователь должен увидеть предупреждение


def test_validate_warns_on_invisible_stroke():
    warns = TextStyle(stroke_enabled=True, stroke_width=0).validate()
    assert any("Обводка" in w for w in warns)


def test_unknown_align_falls_back_to_center():
    st = TextStyle(text_align="по-диагонали")
    warns = st.validate()
    assert any("выравнивание" in w for w in warns)
    fixed, _ = st.sanitize()
    assert fixed.text_align == "center"


# ------------------------------------------------------------------- регистр

@pytest.mark.parametrize(
    "mode,text,expected",
    [
        ("none", "Привет Мир", "Привет Мир"),
        ("upper", "привет мир", "ПРИВЕТ МИР"),
        ("lower", "ПРИВЕТ", "привет"),
        ("title", "привет мир", "Привет Мир"),
        ("caps", "привет", "ПРИВЕТ"),
    ],
)
def test_apply_case(mode, text, expected):
    assert TextStyle(case_mode=mode).apply_case(text) == expected


def test_caps_adds_tracking():
    st = TextStyle(case_mode="caps", tracking=10)
    assert st.tracking_for_caps() == 40
    assert TextStyle(case_mode="none", tracking=10).tracking_for_caps() == 10


# ------------------------------------------------------------------- сериализация

def test_round_trip_preserves_values():
    st = TextStyle(font_family="Montserrat", font_size=88, stroke_width=10, box_enabled=True)
    restored = TextStyle.from_json(st.to_json())
    assert restored.font_family == "Montserrat"
    assert restored.font_size == 88
    assert restored.stroke_width == 10
    assert restored.box_enabled is True


def test_from_dict_ignores_unknown_fields():
    """Пресет из будущей версии не должен ронять текущую."""
    data = TextStyle().to_dict()
    data["field_from_future"] = 42
    st = TextStyle.from_dict(data)
    assert st.font_size > 0


def test_from_dict_tolerates_empty():
    assert TextStyle.from_dict({}).font_size > 0
    assert TextStyle.from_dict(None).font_size > 0


# ------------------------------------------------------------------- размеры

def test_estimated_block_size_grows_with_text():
    st = TextStyle(font_size=60)
    w1, h1 = st.estimated_block_size("Коротко")
    w2, h2 = st.estimated_block_size("Очень длинная строка текста для проверки")
    assert w2 > w1
    assert h1 == h2  # одна строка в обоих случаях


def test_estimated_block_size_accounts_for_box_padding():
    base = TextStyle(font_size=60, box_enabled=False).estimated_block_size("Текст")
    boxed = TextStyle(font_size=60, box_enabled=True, box_padding_x=24, box_padding_y=12).estimated_block_size("Текст")
    assert boxed[0] == pytest.approx(base[0] + 48)
    assert boxed[1] == pytest.approx(base[1] + 24)


def test_estimated_block_size_two_lines():
    st = TextStyle(font_size=60, line_height=1.2)
    _, h = st.estimated_block_size("Первая\nВторая")
    assert h == pytest.approx(2 * 60 * 1.2)


def test_box_width_caps_estimation():
    st = TextStyle(font_size=60, box_width=200)
    w, _ = st.estimated_block_size("С" * 200)
    assert w <= 200


# ------------------------------------------------------------------- пресеты

def test_builtin_presets_are_valid():
    for p in builtin_presets():
        st = TextStyle.from_dict(p)
        _, warns = st.sanitize()
        assert "_name" in p
        # Встроенные пресеты не должны содержать заведомо битых значений.
        assert st.font_size >= 6


def test_builtin_preset_names_unique():
    names = [p["_name"] for p in builtin_presets()]
    assert len(names) == len(set(names))


def test_save_load_delete_user_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMPOC_DATA_DIR", str(tmp_path))
    st = TextStyle(font_family="Roboto", font_size=77, box_enabled=True)
    path = save_preset(st, "Мой пресет")
    assert path.endswith(".json")

    names = [p["_name"] for p in list_user_presets()]
    assert "Мой пресет" in names

    loaded = load_preset("Мой пресет")
    assert loaded is not None
    assert loaded.font_size == 77

    assert delete_preset("Мой пресет") is True
    assert load_preset("Мой пресет") is None


def test_cyrillic_preset_filename_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMPOC_DATA_DIR", str(tmp_path))
    save_preset(TextStyle(), "Стёкла/градиент: тест")
    files = [p.name for p in (tmp_path / "presets").iterdir()]
    assert len(files) == 1
    assert "/" not in files[0] and ":" not in files[0]


def test_corrupt_preset_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMPOC_DATA_DIR", str(tmp_path))
    d = tmp_path / "presets"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{ не json", encoding="utf-8")
    save_preset(TextStyle(), "Нормальный")
    names = [p["_name"] for p in list_user_presets()]
    assert "Нормальный" in names


def test_load_builtin_preset_by_name():
    st = load_preset("Классика")
    assert st is not None
    assert st.font_family == "Arial"


# ------------------------------------------------------------------- разделение

def test_style_has_no_timing_fields():
    """Оформление не должно знать про время — иначе смена цвета запускала бы распознавание."""
    forbidden = {"in_time", "out_time", "start", "end", "duration", "fps", "frame_rate"}
    assert not (forbidden & set(TextStyle.__dataclass_fields__))
