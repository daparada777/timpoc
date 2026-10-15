"""
timpoc — модель оформления субтитров и пресеты.

Три принципиальных решения:

1) Оформление полностью отделено от распознавания. Transcript не знает
   ничего о цветах, а Style — ничего о таймингах. Поэтому смена оформления
   не запускает распознавание: применяется только Style.

2) Каждый параметр отображается на конкретное свойство After Effects.
   Это не «красивые поля», у которых нет адресата. Соответствие лежит в
   host/timpoc_host.jsx, а здесь — источник правды и валидация диапазонов.

3) Подложка (фон) в AE у текстового слоя отсутствует как свойство.
   Мы не выдумываем API: реализуем её прямоугольным сплошным слоем
   (solid) с экспрешеном по sourceRectAtTime() текста. Это стандартный,
   поддерживаемый приём. Пользователь может отключить экспрешены — тогда
   подложка становится обычным слоем фиксированного размера, и панель
   честно предупреждает, что она не будет повторять длину текста.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

STYLE_VERSION = 1


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_color(value: Any) -> List[int]:
    """Любой вход → [r, g, b] в диапазоне 0..255.

    Принимаем: "#RRGGBB", "#RGB", [r,g,b] 0..255, [r,g,b] 0..1, "rgb(r,g,b)".
    Непонятный вход не роняет панель — возвращаем белый и помечаем в warnings.
    """
    if value is None:
        return [255, 255, 255]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("#"):
            s = s[1:]
            if len(s) == 3:
                s = "".join(c * 2 for c in s)
            if len(s) == 8:  # #RRGGBBAA — прозрачность хранится отдельно
                s = s[:6]
            if len(s) == 6:
                try:
                    return [int(s[i : i + 2], 16) for i in (0, 2, 4)]
                except ValueError:
                    return [255, 255, 255]
        if s.lower().startswith("rgb"):
            digits = "".join(c if c.isdigit() or c == "." else " " for c in s).split()
            nums = [float(d) for d in digits if d]
            if len(nums) >= 3:
                return [_int_component(n) for n in nums[:3]]
        return [255, 255, 255]
    if isinstance(value, (list, tuple)):
        nums = list(value)
        if len(nums) >= 3:
            # 0..1 (как в ExtendScript color) или 0..255 — различаем по максимуму.
            if all(isinstance(n, (int, float)) and 0 <= n <= 1.0 for n in nums[:3]) and max(nums[:3]) <= 1.0:
                return [int(round(float(n) * 255)) for n in nums[:3]]
            return [_int_component(n) for n in nums[:3]]
    return [255, 255, 255]


def _int_component(n: float) -> int:
    return int(_clamp(round(float(n)), 0, 255))


def to_ae_color(rgb: List[int]) -> List[float]:
    """0..255 → 0..1 для ExtendScript (app.project цветовая модель)."""
    return [round(c / 255.0, 4) for c in normalize_color(rgb)]


def to_css(rgb: List[int], alpha: float = 1.0) -> str:
    r, g, b = normalize_color(rgb)
    return f"rgba({r}, {g}, {b}, {_clamp(alpha, 0, 1):.3f})"


@dataclass
class TextStyle:
    # Шрифт
    font_family: str = "Arial"
    font_style: str = "Regular"          # Regular / Bold / Italic / Bold Italic
    faux_bold: bool = False              # если начертания нет — имитация жирности
    font_size: float = 64.0              # пиксели
    line_height: float = 1.25            # множитель от размера (leading)
    tracking: float = 0.0                # межбуквенное, в 1/1000 em (единица AE)
    case_mode: str = "none"              # none | upper | lower | title | caps
    text_align: str = "center"           # left | center | right  (внутри блока)
    box_width: float = 900.0             # максимальная ширина блока, px
    max_lines: int = 2

    # Цвет текста
    color: List[int] = field(default_factory=lambda: [255, 255, 255])
    opacity: float = 1.0                 # 0..1

    # Обводка
    stroke_enabled: bool = True
    stroke_color: List[int] = field(default_factory=lambda: [0, 0, 0])
    stroke_width: float = 6.0            # пиксели
    stroke_opacity: float = 1.0
    stroke_over_fill: bool = True        # обводка поверх заливки (AE: Stroke Over Fill)

    # Тень (Drop Shadow — эффект AE)
    shadow_enabled: bool = True
    shadow_color: List[int] = field(default_factory=lambda: [0, 0, 0])
    shadow_opacity: float = 0.55
    shadow_distance: float = 6.0         # пиксели
    shadow_direction: float = 135.0      # градусы, как в эффекте Drop Shadow
    shadow_softness: float = 8.0         # размытие, пиксели

    # Подложка
    box_enabled: bool = False
    box_color: List[int] = field(default_factory=lambda: [0, 0, 0])
    box_opacity: float = 0.55
    box_padding_x: float = 24.0
    box_padding_y: float = 12.0
    box_corner_radius: float = 14.0
    box_use_expression: bool = True      # авто-размер по тексту

    # Служебное
    version: int = STYLE_VERSION

    # ------------------------------------------------------------------ API

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TextStyle":
        data = dict(d or {})
        data.pop("version", None)
        known: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in TextStyle.__dataclass_fields__:
                continue  # неизвестные поля из новых версий просто игнорируем
            known[key] = value
        style = TextStyle(**known)
        return style.sanitize()[0]

    @staticmethod
    def from_json(text: str) -> "TextStyle":
        return TextStyle.from_dict(json.loads(text))

    def validate(self) -> List[str]:
        """Предупреждения о сомнительных значениях (не ошибки)."""
        warns: List[str] = []
        if self.font_size < 6:
            warns.append("Размер шрифта очень мал — текст будет нечитаемым.")
        if self.font_size > 400:
            warns.append("Размер шрифта очень большой — текст может не влезть в кадр.")
        if self.box_width < 40:
            warns.append("Ширина блока слишком мала — слова будут переноситься по буквам.")
        if self.line_height < 0.6:
            warns.append("Межстрочный интервал меньше 0.6 — строки наложатся друг на друга.")
        if self.tracking > 300:
            warns.append("Очень большое межбуквенное расстояние.")
        if self.stroke_enabled and self.stroke_width <= 0:
            warns.append("Обводка включена, но толщина равна нулю — она не будет видна.")
        if self.text_align not in ("left", "center", "right"):
            warns.append(f"Неизвестное выравнивание «{self.text_align}» — использовано center.")
        if self.case_mode not in ("none", "upper", "lower", "title", "caps"):
            warns.append(f"Неизвестный регистр «{self.case_mode}» — текст не изменяется.")
        return warns

    def sanitize(self) -> Tuple["TextStyle", List[str]]:
        """Привести значения к допустимым. Возвращает (стиль, предупреждения)."""
        s = TextStyle(**asdict(self))
        s.font_size = _clamp(float(s.font_size), 1, 1000)
        s.line_height = _clamp(float(s.line_height), 0.2, 5.0)
        s.tracking = _clamp(float(s.tracking), -500, 1000)
        s.opacity = _clamp(float(s.opacity), 0.0, 1.0)
        s.box_width = _clamp(float(s.box_width), 10, 20000)
        s.max_lines = int(max(1, s.max_lines))
        s.color = normalize_color(s.color)
        s.stroke_color = normalize_color(s.stroke_color)
        s.shadow_color = normalize_color(s.shadow_color)
        s.box_color = normalize_color(s.box_color)
        s.stroke_width = _clamp(float(s.stroke_width), 0, 100)
        s.stroke_opacity = _clamp(float(s.stroke_opacity), 0.0, 1.0)
        s.shadow_opacity = _clamp(float(s.shadow_opacity), 0.0, 1.0)
        s.shadow_distance = _clamp(float(s.shadow_distance), 0, 500)
        s.shadow_direction = float(s.shadow_direction) % 360.0
        s.shadow_softness = _clamp(float(s.shadow_softness), 0, 400)
        s.box_opacity = _clamp(float(s.box_opacity), 0.0, 1.0)
        s.box_padding_x = _clamp(float(s.box_padding_x), 0, 500)
        s.box_padding_y = _clamp(float(s.box_padding_y), 0, 500)
        s.box_corner_radius = _clamp(float(s.box_corner_radius), 0, 500)
        if s.text_align not in ("left", "center", "right"):
            s.text_align = "center"
        if s.case_mode not in ("none", "upper", "lower", "title", "caps"):
            s.case_mode = "none"
        return s, s.validate()

    # ------------------------------------------------------------- эффекты

    def apply_case(self, text: str) -> str:
        """Применить регистр. Работает с кириллицей (Python str.upper понимает её)."""
        if self.case_mode == "upper":
            return text.upper()
        if self.case_mode == "lower":
            return text.lower()
        if self.case_mode == "title":
            # str.title() ломает апострофы; делаем аккуратно по словам.
            return " ".join(
                (w[0].upper() + w[1:]) if w else w
                for w in text.replace("\n", " \n ").split(" ")
            )
        if self.case_mode == "caps":
            # «Капс с уменьшенным трекингом» — частый приём в вертикальных роликах.
            return text.upper()
        return text

    def tracking_for_caps(self) -> float:
        """Для режима caps обычно хочется чуть больше трекинга."""
        return self.tracking + 30.0 if self.case_mode == "caps" else self.tracking

    def css_line_height_px(self) -> float:
        return self.font_size * self.line_height

    def css_letter_spacing_px(self) -> float:
        """Перевод трекинга AE (1/1000 em) в px для предпросмотра.

        Это приближение: AE считает трекинг от размера шрифта, браузер —
        тоже, поэтому расхождение небольшое, но предпросмотр всё равно
        нужно сверять с композицией (см. docs/PREVIEW_LIMITS.md).
        """
        return self.font_size * (self.tracking_for_caps() / 1000.0)

    def estimated_block_size(self, text: str) -> Tuple[float, float]:
        """Оценка размера блока в px для предпросмотра и предупреждений о выходе за кадр.

        Это именно оценка: точные метрики даёт только сам After Effects.
        """
        lines = text.split("\n") if text else [""]
        # Средняя ширина глифа для пропорциональных шрифтов ≈ 0.52 em.
        char_w = self.font_size * 0.52 + self.css_letter_spacing_px()
        widest = max((len(l) for l in lines), default=0)
        width = min(self.box_width, widest * char_w)
        height = len(lines) * self.css_line_height_px()
        if self.box_enabled:
            width += 2 * self.box_padding_x
            height += 2 * self.box_padding_y
        return width, height


# --------------------------------------------------------------------------
# Пресеты
# --------------------------------------------------------------------------

def _preset(name: str, **overrides: Any) -> Dict[str, Any]:
    base = TextStyle().to_dict()
    base.update(overrides)
    base["_name"] = name
    return base


BUILTIN_PRESETS: List[Dict[str, Any]] = [
    _preset(
        "Классика",
        font_family="Arial",
        font_size=64,
        stroke_enabled=True,
        stroke_width=6,
        shadow_enabled=True,
        shadow_opacity=0.45,
        box_enabled=False,
    ),
    _preset(
        "Reels / Shorts",
        font_family="Montserrat",
        font_style="Bold",
        faux_bold=True,
        font_size=88,
        case_mode="upper",
        tracking=20,
        line_height=1.1,
        text_align="center",
        color=[255, 255, 255],
        stroke_enabled=True,
        stroke_color=[12, 8, 24],
        stroke_width=10,
        shadow_enabled=True,
        shadow_distance=10,
        shadow_softness=18,
        box_enabled=False,
    ),
    _preset(
        "Стекло timpoc",
        font_family="Inter",
        font_size=72,
        color=[255, 255, 255],
        stroke_enabled=True,
        stroke_color=[74, 32, 128],
        stroke_width=4,
        stroke_opacity=0.85,
        shadow_enabled=True,
        shadow_color=[40, 10, 70],
        shadow_opacity=0.6,
        shadow_softness=24,
        shadow_distance=8,
        box_enabled=True,
        box_color=[26, 12, 44],
        box_opacity=0.42,
        box_corner_radius=22,
        box_padding_x=28,
        box_padding_y=16,
    ),
    _preset(
        "Подложка",
        font_family="Arial",
        font_style="Bold",
        faux_bold=True,
        font_size=58,
        stroke_enabled=False,
        shadow_enabled=False,
        box_enabled=True,
        box_color=[0, 0, 0],
        box_opacity=0.68,
        box_corner_radius=10,
        box_padding_x=18,
        box_padding_y=10,
    ),
    _preset(
        "Без обводки",
        font_family="Georgia",
        font_size=60,
        stroke_enabled=False,
        shadow_enabled=True,
        shadow_opacity=0.35,
        shadow_softness=14,
    ),
]


def builtin_presets() -> List[Dict[str, Any]]:
    return [dict(p) for p in BUILTIN_PRESETS]


def presets_dir() -> str:
    base = os.environ.get("TIMPOC_DATA_DIR")
    if base:
        return os.path.join(os.path.abspath(base), "presets")
    home = os.path.expanduser("~")
    if os.name == "nt":
        root = os.environ.get("APPDATA") or home
        return os.path.join(root, "timpoc", "presets")
    if os.path.isdir(os.path.join(home, "Library")):
        return os.path.join(home, "Library", "Application Support", "timpoc", "presets")
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "timpoc", "presets"
    )


def _safe_filename(name: str) -> str:
    """Имя пресета → безопасное имя файла (кириллица допустима, спецсимволы — нет)."""
    keep = []
    for ch in (name or "").strip():
        if ch.isalnum() or ch in " -_":
            keep.append(ch)
    cleaned = "".join(keep).strip() or "preset"
    return cleaned[:80]


def list_user_presets() -> List[Dict[str, Any]]:
    d = presets_dir()
    if not os.path.isdir(d):
        return []
    out: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["_name"] = data.get("_name") or os.path.splitext(fn)[0]
            data["_builtin"] = False
            out.append(data)
        except (OSError, ValueError):
            # Битый пресет не должен ломать список — пропускаем.
            continue
    return out


def save_preset(style: TextStyle, name: str) -> str:
    d = presets_dir()
    os.makedirs(d, exist_ok=True)
    payload = style.to_dict()
    payload["_name"] = name
    path = os.path.join(d, _safe_filename(name) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def delete_preset(name: str) -> bool:
    path = os.path.join(presets_dir(), _safe_filename(name) + ".json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def load_preset(name: str) -> Optional[TextStyle]:
    for p in BUILTIN_PRESETS:
        if p.get("_name") == name:
            return TextStyle.from_dict(p)
    path = os.path.join(presets_dir(), _safe_filename(name) + ".json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return TextStyle.from_dict(json.load(fh))
    return None
