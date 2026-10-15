"""
Тесты шрифтов: поиск, подбор начертания, честная замена отсутствующего шрифта.
"""

import pytest

from timpoc.fonts import (
    UNIVERSAL_FALLBACKS,
    check_font,
    find_style,
    scan_system_fonts,
    suggest_replacement,
)

AE_FONTS = ["Arial", "Roboto", "Georgia", "Verdana", "PT Sans", "Myriad Pro"]
AE_STYLES = {
    "Arial": ["Regular", "Bold", "Italic", "Bold Italic"],
    "Roboto": ["Regular", "Medium", "Bold"],
    "Georgia": ["Regular", "Italic"],
}


# ------------------------------------------------------------------- наличие

def test_existing_font_is_available():
    res = check_font("Arial", "Bold", AE_FONTS, AE_STYLES)
    assert res["available"] is True
    assert res["family"] == "Arial"
    assert res["style"] == "Bold"
    assert res["warnings"] == []


def test_missing_font_reports_and_suggests():
    res = check_font("Montserrat", "Bold", AE_FONTS, AE_STYLES)
    assert res["available"] is False
    assert res["replacement"] is not None
    assert res["family"] == res["replacement"]
    # Пользователь должен увидеть объяснение, а не молчаливую подмену.
    assert any("не найден" in w for w in res["warnings"])


def test_missing_font_with_no_alternatives():
    res = check_font("Montserrat", "Bold", [], {})
    assert res["available"] is False
    assert res["replacement"] is None
    assert res["warnings"]


# ------------------------------------------------------------------- начертания

def test_find_style_exact():
    assert find_style(["Regular", "Bold"], "Bold") == ("Bold", False)


def test_find_style_case_insensitive():
    assert find_style(["Regular", "Bold"], "bold") == ("Bold", False)


def test_find_style_partial_match():
    style, faux = find_style(["Regular", "Bold Italic"], "Bold Italic")
    assert style == "Bold Italic"
    assert faux is False


def test_find_style_falls_back_to_regular_with_faux_bold():
    style, faux = find_style(["Regular", "Italic"], "Bold")
    assert style == "Regular"
    assert faux is True


def test_find_style_prefers_real_bold_over_faux():
    style, faux = find_style(["Regular", "Bold"], "Bold")
    assert faux is False


def test_find_style_empty_list_keeps_request():
    assert find_style([], "Bold") == ("Bold", True)


def test_missing_style_is_reported():
    res = check_font("Georgia", "Bold", AE_FONTS, AE_STYLES)
    # У Georgia в нашем списке нет Bold → имитация + предупреждение.
    assert res["faux_bold"] is True
    assert any("Настоящего полужирного" in w or "отсутствует" in w for w in res["warnings"])


# ------------------------------------------------------------------- замена

def test_suggest_replacement_uses_chain():
    # Montserrat → Inter → (нет) → Roboto (есть в списке AE).
    assert suggest_replacement("Montserrat", AE_FONTS) == "Roboto"


def test_suggest_replacement_exact_hit():
    assert suggest_replacement("Arial", AE_FONTS) == "Arial"


def test_suggest_replacement_case_insensitive():
    assert suggest_replacement("arial", AE_FONTS) == "Arial"


def test_suggest_replacement_no_fonts_returns_none():
    assert suggest_replacement("Arial", []) is None


def test_suggest_replacement_last_resort_is_some_font():
    result = suggest_replacement("Экзотический Шрифт", ["Comic Sans MS", "Impact"])
    assert result in ("Comic Sans MS", "Impact")


def test_universal_fallbacks_are_sane():
    assert "Arial" in UNIVERSAL_FALLBACKS


# ------------------------------------------------------------------- система

def test_scan_system_fonts_does_not_crash():
    """В песочнице шрифтов может не быть — функция обязана вернуть список, а не упасть."""
    fonts = scan_system_fonts()
    assert isinstance(fonts, list)
    for f in fonts:
        assert f.family
        assert f.styles


def test_scanned_fonts_are_sorted():
    fonts = scan_system_fonts()
    names = [f.family.lower() for f in fonts]
    assert names == sorted(names)


def test_check_font_without_ae_list_uses_system():
    """Если AE не отдал список шрифтов, проверяем по системному — без падения."""
    res = check_font("Arial", "Regular", [], {})
    assert "available" in res
    assert isinstance(res["warnings"], list)
