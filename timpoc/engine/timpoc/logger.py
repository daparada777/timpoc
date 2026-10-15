"""
timpoc — журнал диагностики.

Требование из ТЗ: журнал без паролей, API-ключей и лишних пользовательских
данных. Поэтому:
  * в журнал НЕ пишутся тексты субтитров и аудио;
  * пути к файлам маскируются (остаётся только имя файла);
  * любые строки вида key=... / token=... / Authorization вычищаются.

Журнал — первый инструмент поддержки: пользователь присылает его, и по нему
видно, что именно не получилось, без доступа к его проекту.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Optional

LOGGER_NAME = "timpoc"
_MAX_BYTES = 2 * 1024 * 1024  # 2 МБ на файл, храним 3 ротации

# Что вычищаем из любой строки перед записью.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(token|key|secret|password|passwd|authorization|api[-_]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"hf_[A-Za-z0-9]{10,}"),  # токены Hugging Face
]

# Пути маскируем, но имя файла оставляем: оно полезно для диагностики.
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|[~/])(?:[^\s:*?\"<>|]+[\\/])+([^\s:*?\"<>|]+)")


def mask_sensitive(text: str) -> str:
    """Убрать секреты и урезать пути. Работает и с кириллицей, и с пробелами."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1) if m.groups() else 'secret'}=***", out)
    out = _PATH_PATTERN.sub(r"…/\1", out)
    return out


def data_dir() -> str:
    base = os.environ.get("TIMPOC_DATA_DIR")
    if base:
        return os.path.abspath(base)
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or home, "timpoc")
    if os.path.isdir(os.path.join(home, "Library")):
        return os.path.join(home, "Library", "Application Support", "timpoc")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "timpoc")


def log_path() -> str:
    return os.path.join(data_dir(), "logs", "timpoc.log")


class _MaskingFormatter(logging.Formatter):
    """Форматтер, который пропускает сообщение через mask_sensitive."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        record.msg = mask_sensitive(str(record.msg))
        if record.args:
            try:
                record.args = tuple(
                    mask_sensitive(a) if isinstance(a, str) else a for a in record.args
                )
            except TypeError:
                pass
        return super().format(record)


def get_logger(name: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """Логгер с записью в файл и (опционально) в stderr.

    Повторные вызовы не дублируют обработчики — иначе каждая строка
    писалась бы в файл несколько раз.
    """
    root = logging.getLogger(LOGGER_NAME)
    if not root.handlers:
        root.setLevel(level)
        formatter = _MaskingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

        try:
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            # Нет прав на папку — работаем без файла, но не падаем.
            pass

        if os.environ.get("TIMPOC_LOG_STDERR"):
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
            root.addHandler(stream)

        root.propagate = False

    return root.getChild(name) if name else root


def tail_log(lines: int = 200) -> str:
    """Последние строки журнала — для кнопки «Показать журнал» в панели."""
    path = log_path()
    if not os.path.isfile(path):
        return "Журнал ещё пуст."
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
        return "".join(data[-max(1, lines) :])
    except OSError as exc:
        return f"Не удалось прочитать журнал: {exc}"
