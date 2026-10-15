"""
timpoc — шрифты: поиск, проверка наличия, подбор замены.

Почему это отдельный модуль: «отсутствует шрифт» — одна из самых частых
причин, по которой субтитры выглядят не так, как в панели. После After
Effects молча подставляет свой шрифт, и пользователь думает, что timpoc
сломался. Поэтому мы:
  1) показываем список шрифтов, реально доступных в системе;
  2) если выбранного нет — говорим об этом прямо и предлагаем замену;
  3) НИКОГДА не подменяем шрифт молча.

Список шрифтов для AE берётся из самого After Effects (хост-скрипт отдаёт
app.fonts), а не из ОС: AE видит только то, что видит сам. Системный
список здесь — запасной вариант и источник для предпросмотра в панели.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Порядок предпочтения при подборе замены: сначала близкие по характеру.
FALLBACK_CHAIN: Dict[str, List[str]] = {
    "Montserrat": ["Inter", "Roboto", "Open Sans", "Arial", "DejaVu Sans"],
    "Inter": ["Roboto", "Open Sans", "Segoe UI", "Arial", "DejaVu Sans"],
    "Roboto": ["Inter", "Open Sans", "Segoe UI", "Arial", "DejaVu Sans"],
    "Open Sans": ["Inter", "Roboto", "Arial", "DejaVu Sans"],
    "Segoe UI": ["Inter", "Roboto", "Arial", "DejaVu Sans"],
    "Helvetica": ["Arial", "Inter", "DejaVu Sans"],
    "Arial": ["Helvetica", "Inter", "DejaVu Sans", "Liberation Sans"],
    "Georgia": ["Times New Roman", "DejaVu Serif", "Liberation Serif"],
    "Times New Roman": ["Georgia", "DejaVu Serif", "Liberation Serif"],
    "Verdana": ["Tahoma", "Arial", "DejaVu Sans"],
    "PT Sans": ["Open Sans", "Inter", "Arial", "DejaVu Sans"],
    "Noto Sans": ["Inter", "Open Sans", "DejaVu Sans", "Arial"],
}

# Универсальный хвост цепочки — почти всегда что-то из этого есть.
UNIVERSAL_FALLBACKS = [
    "Arial",
    "Helvetica",
    "DejaVu Sans",
    "Liberation Sans",
    "Segoe UI",
    "Roboto",
    "Noto Sans",
    "Verdana",
    "Tahoma",
]

# Кириллица обязательна: без неё русский текст превратится в квадраты.
CYRILLIC_REQUIRED = True


@dataclass
class FontInfo:
    family: str
    styles: List[str]
    has_cyrillic: Optional[bool] = None  # None = неизвестно
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "styles": list(self.styles),
            "has_cyrillic": self.has_cyrillic,
            "path": self.path,
        }


def system_font_dirs() -> List[str]:
    """Папки со шрифтами для текущей ОС."""
    if os.name == "nt":
        out = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        return out
    if os.path.isdir(os.path.expanduser("~/Library")):
        return [
            os.path.expanduser("~/Library/Fonts"),
            "/Library/Fonts",
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
        ]
    return [
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
    ]


def scan_system_fonts() -> List[FontInfo]:
    """Просканировать системные папки.

    Намеренно НЕ парсим таблицы шрифтов: это медленно и требует сторонних
    библиотек. Берём имя из имени файла — достаточно для предпросмотра и
    для списка выбора. Точный список начертаний даёт сам After Effects.
    """
    found: Dict[str, FontInfo] = {}
    exts = (".ttf", ".otf", ".ttc", ".otc")
    for root in system_font_dirs():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith(exts):
                    continue
                stem = os.path.splitext(fn)[0]
                family, style = _split_family_style(stem)
                info = found.setdefault(family, FontInfo(family=family, styles=[], path=dirpath))
                if style and style not in info.styles:
                    info.styles.append(style)
    for info in found.values():
        if not info.styles:
            info.styles = ["Regular"]
    return sorted(found.values(), key=lambda f: f.family.lower())


_STYLE_WORDS = (
    "Bold Italic",
    "Italic",
    "Bold",
    "Regular",
    "Medium",
    "Light",
    "Thin",
    "Black",
    "SemiBold",
    "DemiBold",
    "ExtraBold",
    "ExtraLight",
    "Condensed",
)


def _split_family_style(stem: str) -> Tuple[str, str]:
    """'Arial-BoldItalic' → ('Arial', 'Bold Italic')."""
    for word in _STYLE_WORDS:
        pattern = re.compile(r"[-_ ]" + re.escape(word.replace(" ", r"[\s_-]?")) + r"$", re.IGNORECASE)
        m = pattern.search(stem)
        if m:
            family = stem[: m.start()].strip(" -_")
            if family:
                return family, word
    return stem, "Regular"


def find_style(font_styles: List[str], wanted: str, faux_bold: bool = False) -> Tuple[str, bool]:
    """Подобрать ближайшее начертание к запрошенному.

    Возвращает (имя начертания, нужна ли имитация жирности).
    ExtendScript умеет faux bold, но выглядит хуже настоящего Bold —
    поэтому сначала ищем настоящее начертание.
    """
    if not font_styles:
        return wanted or "Regular", True
    wanted_norm = (wanted or "Regular").strip().lower()
    for s in font_styles:
        if s.strip().lower() == wanted_norm:
            return s, False
    # Частичное совпадение ("Bold Italic" при запросе "Bold").
    for s in font_styles:
        if wanted_norm and wanted_norm in s.strip().lower():
            return s, False
    want_bold = "bold" in wanted_norm or faux_bold
    for s in font_styles:
        if "bold" in s.strip().lower() and want_bold:
            return s, False
    for s in font_styles:
        if s.strip().lower() == "regular":
            return s, bool(want_bold)
    return font_styles[0], bool(want_bold)


def suggest_replacement(
    wanted_family: str, available: List[str], prefer_cyrillic: bool = CYRILLIC_REQUIRED
) -> Optional[str]:
    """Предложить замену отсутствующему шрифту из доступных.

    available — список семейств, которые реально видит After Effects.
    Возвращаем None, если подходящей замены нет: тогда панель должна
    показать предупреждение и оставить выбор за пользователем.
    """
    if not available:
        return None
    avail_norm = {a.strip().lower(): a for a in available}

    # 1) Точное совпадение с учётом регистра — вдруг шрифт всё же есть.
    exact = avail_norm.get((wanted_family or "").strip().lower())
    if exact:
        return exact

    chain = FALLBACK_CHAIN.get(wanted_family, []) + UNIVERSAL_FALLBACKS
    for candidate in chain:
        hit = avail_norm.get(candidate.strip().lower())
        if hit:
            return hit

    # 2) Последний шанс: любой шрифт со словом Sans (без засечек) — нейтральный.
    for key, original in avail_norm.items():
        if "sans" in key or key in ("arial", "helvetica", "roboto", "inter"):
            return original
    return available[0]


def check_font(
    wanted_family: str,
    wanted_style: str,
    ae_families: List[str],
    ae_styles: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, object]:
    """Полная проверка шрифта перед применением.

    Возвращает словарь для панели:
      available      — шрифт найден в AE;
      family/style   — что фактически будет применено;
      faux_bold      — придётся ли имитировать жирность;
      replacement    — предложенная замена (если шрифта нет);
      warnings       — человекочитаемые предупреждения.

    Мы НЕ бросаем исключение: отсутствие шрифта — не авария, а ситуация,
    в которой нужно показать предупреждение и продолжить работу.
    """
    warnings: List[str] = []
    families = ae_families or []
    styles_map = ae_styles or {}

    exact = wanted_family in families
    replacement: Optional[str] = None
    family = wanted_family
    if not exact:
        replacement = suggest_replacement(wanted_family, families)
        if replacement:
            warnings.append(
                f"Шрифт «{wanted_family}» не найден в After Effects. "
                f"Будет использован «{replacement}». Чтобы увидеть исходный шрифт — "
                "установите его в систему и перезапустите After Effects."
            )
            family = replacement
        else:
            warnings.append(
                f"Шрифт «{wanted_family}» не найден, и подобрать замену не удалось. "
                "After Effects подставит свой шрифт по умолчанию."
            )

    styles = styles_map.get(family) or styles_map.get(wanted_family) or []
    style, faux_bold = find_style(styles, wanted_style)
    if styles and wanted_style and wanted_style not in styles and not faux_bold:
        warnings.append(
            f"Начертание «{wanted_style}» у шрифта «{family}» отсутствует. Использовано «{style}»."
        )
    if faux_bold:
        warnings.append(
            "Настоящего полужирного начертания нет — жирность будет имитирована. "
            "Выглядит чуть хуже настоящего Bold."
        )

    return {
        "available": exact,
        "family": family,
        "style": style,
        "faux_bold": faux_bold,
        "replacement": replacement,
        "warnings": warnings,
    }
