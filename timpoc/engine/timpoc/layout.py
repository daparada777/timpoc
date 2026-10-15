"""
timpoc — расположение блока субтитров.

Здесь важно не путать две разные вещи, которые часто смешивают:
  * ПОЗИЦИЯ БЛОКА на экране (этот модуль) — где стоит текстовый блок;
  * ВЫРАВНИВАНИЕ ТЕКСТА внутри блока (style.text_align) — как строки
    выровнены относительно друг друга.
Они задаются независимо: блок может стоять справа, а текст внутри — по левому краю.

Координатная система After Effects: (0,0) — левый верхний угол композиции,
X растёт вправо, Y вниз. Мы работаем именно в ней, чтобы панель и AE совпадали.
Якорь текстового слоя при этом центрируется (anchor point в центре блока),
иначе при смене длины текста блок «уезжает».

Пять вертикальных позиций задаются через v_slot (1..5) — как в требованиях:
  1 верх, 2 между верхом и центром, 3 центр, 4 между центром и низом, 5 низ.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Предустановленные соотношения сторон для предпросмотра.
ASPECT_PRESETS: Dict[str, Tuple[int, int, str]] = {
    "9:16": (9, 16, "Вертикальные ролики: Reels, Shorts, TikTok"),
    "16:9": (16, 9, "Горизонтальное видео: YouTube"),
    "1:1": (1, 1, "Квадрат: лента Instagram"),
    "4:5": (4, 5, "Портретная лента Instagram"),
    "comp": (0, 0, "Текущий размер композиции"),
}

V_SLOTS = {
    1: 0.00,   # верх
    2: 0.25,   # между верхом и центром
    3: 0.50,   # центр
    4: 0.75,   # между центром и низом
    5: 1.00,   # низ
}

H_SLOTS = {"left": 0.0, "center": 0.5, "right": 1.0}


@dataclass
class SafeZone:
    """Безопасные зоны в долях от размера стороны.

    title  — зона заголовков (внешняя, строже): текст не должен выходить.
    action — зона действия (внутренняя): тут не должно быть важных элементов.
    Значения по умолчанию — распространённая практика 10% / 20% по высоте
    и 5% / 10% по ширине для вертикального видео с элементами интерфейса.
    """

    title_top: float = 0.10
    title_bottom: float = 0.10
    title_left: float = 0.05
    title_right: float = 0.05
    action_top: float = 0.20
    action_bottom: float = 0.20
    action_left: float = 0.10
    action_right: float = 0.10
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SafeZone":
        known = {k: v for k, v in (d or {}).items() if k in SafeZone.__dataclass_fields__}
        return SafeZone(**known)

    def rect(self, width: float, height: float, inner: bool = False) -> Tuple[float, float, float, float]:
        """Вернуть (x, y, w, h) прямоугольника безопасной зоны."""
        if not self.enabled:
            return 0.0, 0.0, width, height
        if inner:
            t, b = self.action_top, self.action_bottom
            l, r = self.action_left, self.action_right
        else:
            t, b = self.title_top, self.title_bottom
            l, r = self.title_left, self.title_right
        x = width * l
        y = height * t
        return x, y, max(0.0, width * (1 - l - r)), max(0.0, height * (1 - t - b))


@dataclass
class Layout:
    """Положение блока субтитров в композиции."""

    v_slot: int = 5                    # 1..5
    h_slot: str = "center"             # left | center | right
    margin_top: float = 60.0           # отступ от верха, px
    margin_bottom: float = 90.0        # отступ от низа, px
    margin_left: float = 60.0
    margin_right: float = 60.0
    offset_x: float = 0.0              # точная донастройка, px
    offset_y: float = 0.0
    use_exact: bool = False            # если True — берём exact_x/exact_y напрямую
    exact_x: float = 0.0
    exact_y: float = 0.0
    safe_zone: SafeZone = field(default_factory=SafeZone)

    # ------------------------------------------------------------------ API

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["safe_zone"] = self.safe_zone.to_dict()
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Layout":
        data = dict(d or {})
        sz = SafeZone.from_dict(data.pop("safe_zone", {}) or {})
        known = {k: v for k, v in data.items() if k in Layout.__dataclass_fields__}
        lay = Layout(**known)
        lay.safe_zone = sz
        lay.v_slot = int(min(5, max(1, lay.v_slot)))
        if lay.h_slot not in H_SLOTS:
            lay.h_slot = "center"
        return lay

    # ------------------------------------------------------------- вычисление

    def block_position(
        self, comp_width: float, comp_height: float, block_width: float, block_height: float
    ) -> Tuple[float, float]:
        """Вернуть (x, y) ЦЕНТРА блока в координатах композиции.

        Центр, а не левый верхний угол: текстовый слой AE удобнее всего
        позиционировать по центру блока, тогда смена длины текста
        не смещает субтитр.
        """
        if self.use_exact:
            return float(self.exact_x), float(self.exact_y)

        sz = self.safe_zone
        if sz.enabled:
            x0, y0, sw, sh = sz.rect(comp_width, comp_height, inner=False)
        else:
            x0, y0, sw, sh = 0.0, 0.0, comp_width, comp_height

        # Вертикаль. Доступная полоса — между верхним и нижним отступами.
        # Пять позиций распределяются по ней равномерно:
        #   slot 1 — у верха, slot 5 — у низа, slot 3 — ровно по центру,
        #   slot 2 и 4 — посередине между соседями.
        # Ранние версии считали 2 и 4 через половину интервала и давали всего
        # три различных положения — требование «пять вертикальных позиций»
        # при этом не выполнялось.
        top = y0 + self.margin_top
        bottom = max(top, y0 + sh - self.margin_bottom)
        half = block_height / 2.0
        if bottom - top <= block_height:
            # Блок выше доступной полосы — центрируем, чтобы не «прилипать» к верху.
            cy = (top + bottom) / 2.0
        else:
            fraction = (self.v_slot - 1) / 4.0
            cy = top + half + (bottom - top - block_height) * fraction

        # Горизонталь.
        left = x0 + self.margin_left
        right = x0 + sw - self.margin_right
        h = H_SLOTS[self.h_slot]
        if self.h_slot == "left":
            cx = left + block_width / 2.0
        elif self.h_slot == "right":
            cx = right - block_width / 2.0
        else:
            cx = (left + right) / 2.0

        return cx + self.offset_x, cy + self.offset_y

    def block_rect(
        self, comp_width: float, comp_height: float, block_width: float, block_height: float
    ) -> Tuple[float, float, float, float]:
        """Прямоугольник блока (x, y, w, h) — левый верхний угол."""
        cx, cy = self.block_position(comp_width, comp_height, block_width, block_height)
        return cx - block_width / 2.0, cy - block_height / 2.0, block_width, block_height

    def overflow_report(
        self, comp_width: float, comp_height: float, block_width: float, block_height: float
    ) -> Dict[str, Any]:
        """Честная проверка выхода за границы — кадры и безопасные зоны.

        Возвращаем не просто True/False, а конкретные величины в px, чтобы
        панель могла показать «выходит на 34 px снизу» и предложить кнопку
        «вписать».
        """
        x, y, w, h = self.block_rect(comp_width, comp_height, block_width, block_height)
        out = {
            "over_left": round(max(0.0, -x), 1),
            "over_top": round(max(0.0, -y), 1),
            "over_right": round(max(0.0, x + w - comp_width), 1),
            "over_bottom": round(max(0.0, y + h - comp_height), 1),
        }
        out["outside_frame"] = any(v > 0.5 for v in out.values())

        sz = self.safe_zone
        if sz.enabled:
            sx, sy, sw, sh = sz.rect(comp_width, comp_height, inner=False)
            sz_out = {
                "over_left": round(max(0.0, sx - x), 1),
                "over_top": round(max(0.0, sy - y), 1),
                "over_right": round(max(0.0, x + w - (sx + sw)), 1),
                "over_bottom": round(max(0.0, y + h - (sy + sh)), 1),
            }
            out["safe_zone"] = sz_out
            out["outside_safe_zone"] = any(v > 0.5 for v in sz_out.values())
        else:
            out["safe_zone"] = None
            out["outside_safe_zone"] = False
        return out

    def fit_into(
        self, comp_width: float, comp_height: float, block_width: float, block_height: float
    ) -> "Layout":
        """Вернуть копию расположения, вписанную в кадр (кнопка «Вписать»)."""
        lay = Layout.from_dict(self.to_dict())
        lay.use_exact = False
        # Сначала пробуем без точных координат.
        for _ in range(4):
            rep = lay.overflow_report(comp_width, comp_height, block_width, block_height)
            if not rep["outside_frame"]:
                break
            lay.offset_x -= rep["over_right"] - rep["over_left"]
            lay.offset_y -= rep["over_bottom"] - rep["over_top"]
        return lay


def preview_size(
    aspect: str, comp_width: float, comp_height: float, custom: Optional[Tuple[int, int]] = None
) -> Tuple[float, float, str]:
    """Размер области предпросмотра (w, h) и подпись формата.

    aspect: ключ из ASPECT_PRESETS или "custom".
    Предпросмотр НЕ меняет композицию — он только считает размеры окна.
    """
    if aspect == "comp":
        return float(comp_width), float(comp_height), "Композиция"
    if aspect == "custom":
        cw, ch = (custom or (9, 16))
        cw = max(1, int(cw))
        ch = max(1, int(ch))
        return float(cw), float(ch), f"{cw}:{ch}"
    if aspect not in ASPECT_PRESETS:
        return float(comp_width), float(comp_height), "Композиция"
    w, h, label = ASPECT_PRESETS[aspect]
    return float(w), float(h), label


def aspect_ratio_value(width: float, height: float) -> float:
    if height <= 0:
        return 0.0
    return float(width) / float(height)


def match_aspect_preset(width: float, height: float, tolerance: float = 0.02) -> str:
    """Найти ближайший предустановленный формат к реальному размеру."""
    target = aspect_ratio_value(width, height)
    best = "comp"
    best_diff = float("inf")
    for key, (w, h, _label) in ASPECT_PRESETS.items():
        if w == 0 or h == 0:
            continue
        diff = abs(target - (w / h))
        if diff < best_diff:
            best_diff = diff
            best = key
    return best if best_diff <= tolerance * max(target, 1.0) else "comp"
