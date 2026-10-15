"""
Тесты расположения: пять вертикальных позиций, горизонталь, отступы,
безопасные зоны, предупреждения о выходе за границы.
"""

import pytest

from timpoc.layout import (
    ASPECT_PRESETS,
    H_SLOTS,
    Layout,
    SafeZone,
    aspect_ratio_value,
    match_aspect_preset,
    preview_size,
)

W, H = 1080.0, 1920.0  # вертикальная композиция 9:16
BW, BH = 800.0, 200.0


def _layout(**kw):
    lay = Layout.from_dict({})
    for k, v in kw.items():
        setattr(lay, k, v)
    return lay


# ------------------------------------------------------------------- вертикаль

def test_five_vertical_positions_are_ordered():
    """1 верх < 2 < 3 центр < 4 < 5 низ — по возрастанию Y."""
    ys = []
    for slot in (1, 2, 3, 4, 5):
        _cx, cy = _layout(v_slot=slot, safe_zone=SafeZone(enabled=False)).block_position(W, H, BW, BH)
        ys.append(cy)
    assert ys == sorted(ys)
    assert len(set(ys)) == 5


def test_top_position_respects_top_margin():
    lay = _layout(v_slot=1, margin_top=100, safe_zone=SafeZone(enabled=False))
    _cx, cy = lay.block_position(W, H, BW, BH)
    assert cy == pytest.approx(100 + BH / 2)


def test_bottom_position_respects_bottom_margin():
    lay = _layout(v_slot=5, margin_bottom=150, safe_zone=SafeZone(enabled=False))
    _cx, cy = lay.block_position(W, H, BW, BH)
    assert cy == pytest.approx(H - 150 - BH / 2)


def test_center_position_is_middle_of_available_area():
    lay = _layout(v_slot=3, margin_top=100, margin_bottom=100, safe_zone=SafeZone(enabled=False))
    _cx, cy = lay.block_position(W, H, BW, BH)
    assert cy == pytest.approx(H / 2)


# ------------------------------------------------------------------- горизонталь

def test_horizontal_left_center_right_ordered():
    xs = []
    for slot in ("left", "center", "right"):
        cx, _cy = _layout(h_slot=slot, safe_zone=SafeZone(enabled=False)).block_position(W, H, BW, BH)
        xs.append(cx)
    assert xs == sorted(xs)
    assert len(set(xs)) == 3


def test_left_position_respects_left_margin():
    cx, _cy = _layout(h_slot="left", margin_left=80, safe_zone=SafeZone(enabled=False)).block_position(W, H, BW, BH)
    assert cx == pytest.approx(80 + BW / 2)


def test_right_position_respects_right_margin():
    cx, _cy = _layout(h_slot="right", margin_right=80, safe_zone=SafeZone(enabled=False)).block_position(W, H, BW, BH)
    assert cx == pytest.approx(W - 80 - BW / 2)


def test_unknown_horizontal_slot_falls_back_to_center():
    lay = Layout.from_dict({"h_slot": "где-то"})
    assert lay.h_slot == "center"


def test_vertical_slot_is_clamped_to_1_5():
    assert Layout.from_dict({"v_slot": 99}).v_slot == 5
    assert Layout.from_dict({"v_slot": -3}).v_slot == 1


# ------------------------------------------------------------------- точные координаты

def test_exact_coordinates_win():
    lay = _layout(use_exact=True, exact_x=123.0, exact_y=456.0)
    cx, cy = lay.block_position(W, H, BW, BH)
    assert (cx, cy) == (123.0, 456.0)


def test_offsets_are_added():
    base = _layout(safe_zone=SafeZone(enabled=False))
    bx, by = base.block_position(W, H, BW, BH)
    moved = _layout(offset_x=30, offset_y=-20, safe_zone=SafeZone(enabled=False))
    mx, my = moved.block_position(W, H, BW, BH)
    assert mx == pytest.approx(bx + 30)
    assert my == pytest.approx(by - 20)


# ------------------------------------------------------------------- безопасные зоны

def test_safe_zone_shrinks_available_area():
    sz_on = _layout(v_slot=1, margin_top=0, safe_zone=SafeZone(enabled=True, title_top=0.1))
    sz_off = _layout(v_slot=1, margin_top=0, safe_zone=SafeZone(enabled=False))
    _cx1, cy1 = sz_on.block_position(W, H, BW, BH)
    _cx2, cy2 = sz_off.block_position(W, H, BW, BH)
    assert cy1 > cy2  # с безопасной зоной блок ниже


def test_safe_zone_rect_dimensions():
    sz = SafeZone(title_top=0.1, title_bottom=0.1, title_left=0.05, title_right=0.05)
    x, y, w, h = sz.rect(1000, 2000)
    assert x == pytest.approx(50)
    assert y == pytest.approx(200)
    assert w == pytest.approx(900)
    assert h == pytest.approx(1600)


def test_disabled_safe_zone_is_full_frame():
    sz = SafeZone(enabled=False)
    assert sz.rect(1000, 2000) == (0.0, 0.0, 1000.0, 2000.0)


# ------------------------------------------------------------------- выход за границы

def test_overflow_detected_when_block_too_wide():
    """Блок шире композиции — должен быть выход за обе стороны."""
    lay = _layout(v_slot=3, h_slot="center", safe_zone=SafeZone(enabled=False))
    rep = lay.overflow_report(W, H, 2000.0, 200.0)
    assert rep["outside_frame"] is True
    assert rep["over_left"] > 0
    assert rep["over_right"] > 0


def test_overflow_detected_when_pushed_right_by_offset():
    lay = _layout(offset_x=900, safe_zone=SafeZone(enabled=False))
    rep = lay.overflow_report(W, H, BW, BH)
    assert rep["outside_frame"] is True
    assert rep["over_right"] > 0


def test_no_overflow_for_centered_small_block():
    lay = _layout(v_slot=3, h_slot="center", safe_zone=SafeZone(enabled=False))
    rep = lay.overflow_report(W, H, 600.0, 200.0)
    assert rep["outside_frame"] is False


def test_overflow_values_are_pixels():
    lay = _layout(use_exact=True, exact_x=100.0, exact_y=100.0, safe_zone=SafeZone(enabled=False))
    rep = lay.overflow_report(1000.0, 1000.0, 400.0, 400.0)
    # центр в (100,100), блок 400x400 → левый край на -100, верхний на -100
    assert rep["over_left"] == pytest.approx(100.0)
    assert rep["over_top"] == pytest.approx(100.0)


def test_safe_zone_violation_reported_separately():
    """Блок внутри кадра, но в безопасной зоне — два разных предупреждения.

    Координаты заданы явно, чтобы проверка не зависела от отступов.
    Центр (540, 1820), блок 800x200 → низ на 1920 = граница кадра ровно.
    Безопасная зона снизу 20% → граница на 1536. Значит зона нарушена, кадр — нет.
    """
    lay = _layout(
        use_exact=True,
        exact_x=540.0,
        exact_y=1820.0,
        safe_zone=SafeZone(enabled=True, title_top=0.1, title_bottom=0.2, title_left=0.05, title_right=0.05),
    )
    rep = lay.overflow_report(W, H, BW, BH)
    assert rep["outside_frame"] is False
    assert rep["outside_safe_zone"] is True
    assert rep["safe_zone"]["over_bottom"] > 0


def test_fit_into_resolves_overflow():
    lay = _layout(use_exact=True, exact_x=50.0, exact_y=50.0, safe_zone=SafeZone(enabled=False))
    fitted = lay.fit_into(1000.0, 1000.0, 400.0, 400.0)
    rep = fitted.overflow_report(1000.0, 1000.0, 400.0, 400.0)
    assert rep["outside_frame"] is False


def test_fit_into_does_not_mutate_original():
    lay = _layout(offset_x=0, offset_y=0, safe_zone=SafeZone(enabled=False))
    original_offset = lay.offset_x
    lay.fit_into(W, H, 5000.0, 500.0)
    assert lay.offset_x == original_offset


# ------------------------------------------------------------------- сериализация

def test_layout_round_trip():
    lay = _layout(v_slot=2, h_slot="right", margin_bottom=77, offset_x=13)
    restored = Layout.from_dict(lay.to_dict())
    assert restored.v_slot == 2
    assert restored.h_slot == "right"
    assert restored.margin_bottom == 77
    assert restored.offset_x == 13


def test_layout_from_dict_keeps_safe_zone():
    lay = Layout.from_dict({"safe_zone": {"title_top": 0.3, "enabled": False}})
    assert lay.safe_zone.title_top == pytest.approx(0.3)
    assert lay.safe_zone.enabled is False


# ------------------------------------------------------------------- предпросмотр

@pytest.mark.parametrize("key", ["9:16", "16:9", "1:1", "4:5", "comp"])
def test_preview_size_known_aspects(key):
    w, h, label = preview_size(key, 1920, 1080)
    assert w > 0 and h > 0
    assert label


def test_preview_comp_uses_real_size():
    assert preview_size("comp", 1234, 567)[:2] == (1234.0, 567.0)


def test_preview_custom_aspect():
    w, h, label = preview_size("custom", 1920, 1080, custom=(21, 9))
    assert (w, h) == (21.0, 9.0)
    assert "21:9" in label


def test_preview_unknown_aspect_falls_back_to_comp():
    assert preview_size("неизвестно", 1920, 1080)[:2] == (1920.0, 1080.0)


def test_preview_does_not_change_comp_size():
    """Переключение формата предпросмотра не должно менять композицию."""
    before = (1920.0, 1080.0)
    preview_size("9:16", *before)
    assert preview_size("comp", *before)[:2] == before


def test_aspect_ratio_value():
    assert aspect_ratio_value(1920, 1080) == pytest.approx(16 / 9)
    assert aspect_ratio_value(100, 0) == 0.0


def test_match_aspect_preset():
    assert match_aspect_preset(1080, 1920) == "9:16"
    assert match_aspect_preset(1920, 1080) == "16:9"
    assert match_aspect_preset(1000, 1000) == "1:1"
    assert match_aspect_preset(1333, 1000) == "comp"  # нестандартный


def test_all_aspect_presets_have_description():
    for key, (w, h, label) in ASPECT_PRESETS.items():
        assert label
        if key != "comp":
            assert w > 0 and h > 0


def test_h_slots_cover_three_positions():
    assert set(H_SLOTS) == {"left", "center", "right"}
