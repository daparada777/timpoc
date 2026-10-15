"""
timpoc — типы ошибок с человекочитаемыми сообщениями.

Правило проекта: каждая ошибка, которая доходит до пользователя, обязана
содержать (1) что произошло, (2) почему, (3) что сделать. Поэтому у
TimpocError два текста: `message` (короткий, для лога) и `hint`
(подсказка действия, для панели).

Ни одна ошибка не должна «проглатываться» молча: если мы не можем
корректно учесть какое-то свойство аудио (например, растяжение времени),
мы обязаны вернуть предупреждение в поле `warnings` результата,
а не сделать вид, что всё в порядке.
"""

from __future__ import annotations

from typing import Optional


# Коды ошибок. Стабильны — панель по ним выбирает иконку и действие.
CODE_NO_AUDIO = "no_audio"
CODE_SILENCE = "silence"
CODE_RANGE_EMPTY = "range_empty"
CODE_MODEL_MISSING = "model_missing"
CODE_MODEL_DOWNLOAD = "model_download"
CODE_MODEL_LOAD = "model_load"
CODE_NO_GPU = "no_gpu"
CODE_OUT_OF_MEMORY = "out_of_memory"
CODE_NO_DISK = "no_disk"
CODE_CORRUPT_FILE = "corrupt_file"
CODE_UNSUPPORTED_FORMAT = "unsupported_format"
CODE_CANCELLED = "cancelled"
CODE_PERMISSION = "permission"
CODE_TIMEOUT = "timeout"
CODE_INTERNAL = "internal"
CODE_BAD_INPUT = "bad_input"


class TimpocError(Exception):
    """Базовая ошибка timpoc.

    code    — стабильный строковый код (см. константы выше).
    message — короткое описание для журнала и заголовка диалога.
    hint    — что сделать пользователю.
    details — технические детали (путь, исключение) — идут только в лог.

    Код по умолчанию — bad_input, а не internal: большинство ошибок, которые
    мы создаём явно, это ошибки входных данных. internal остаётся для
    wrap_unknown(), то есть для действительно непредвиденных сбоев.
    """

    code = CODE_BAD_INPUT

    def __init__(
        self,
        message: str,
        hint: str = "",
        code: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details
        if code is not None:
            self.code = code

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "details": self.details or "",
            },
        }

    def __str__(self) -> str:  # pragma: no cover - только для логов
        base = f"[{self.code}] {self.message}"
        if self.hint:
            base += f" | {self.hint}"
        if self.details:
            base += f" | {self.details}"
        return base


class CancelledError(TimpocError):
    """Пользователь отменил операцию. Это не сбой — панель не должна краснеть."""

    code = CODE_CANCELLED

    def __init__(self, message: str = "Операция отменена пользователем.") -> None:
        super().__init__(message, hint="Можно запустить распознавание заново в любой момент.")


class ModelMissingError(TimpocError):
    code = CODE_MODEL_MISSING

    def __init__(self, model: str, size_mb: int) -> None:
        super().__init__(
            f"Модель распознавания «{model}» не найдена на этом компьютере.",
            hint=(
                f"Нужно скачать её один раз (~{size_mb} МБ) — кнопка «Скачать модель» "
                "в разделе Настройки. Требуется интернет."
            ),
            details=f"model={model}",
        )
        self.model = model


class ModelDownloadError(TimpocError):
    code = CODE_MODEL_DOWNLOAD

    def __init__(self, model: str, reason: str) -> None:
        super().__init__(
            "Не удалось скачать модель распознавания.",
            hint=(
                "Проверьте подключение к интернету и доступ к huggingface.co. "
                "Если сеть корпоративная — может мешать прокси. "
                "Модель можно скачать на другом компьютере и положить в папку моделей."
            ),
            details=f"model={model}; reason={reason}",
        )


class NoGpuError(TimpocError):
    code = CODE_NO_GPU

    def __init__(self) -> None:
        super().__init__(
            "Не удалось использовать видеокарту.",
            hint=(
                "Переключитесь на процессор (CPU) в разделе Настройки — распознавание "
                "будет работать, но медленнее. Для GPU нужны драйверы NVIDIA и CUDA."
            ),
        )


class OutOfMemoryError(TimpocError):
    code = CODE_OUT_OF_MEMORY

    def __init__(self, what: str = "модель") -> None:
        super().__init__(
            f"Не хватило памяти, чтобы загрузить {what}.",
            hint=(
                "Выберите модель меньше (например, small вместо large-v3) или "
                "закройте другие программы. Для large-v3 нужно ~3 ГБ свободной памяти."
            ),
        )


class NoDiskSpaceError(TimpocError):
    code = CODE_NO_DISK

    def __init__(self, need_mb: int, free_mb: int, path: str) -> None:
        super().__init__(
            "Не хватает места на диске.",
            hint=(
                f"Нужно примерно {need_mb} МБ, свободно {free_mb} МБ. "
                "Освободите место или выберите другую папку для временных файлов."
            ),
            details=f"path={path}",
        )


class CorruptFileError(TimpocError):
    code = CODE_CORRUPT_FILE

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            "Файл повреждён или его формат не поддерживается.",
            hint=(
                "Проверьте, что файл открывается в плеере. Если это видео из монтажа — "
                "попробуйте сначала вывести звук в WAV/MP3, затем выбрать этот файл."
            ),
            details=f"path={path}; reason={reason}",
        )


class PermissionError_(TimpocError):
    """Отдельный класс, чтобы не путать со встроенной PermissionError."""

    code = CODE_PERMISSION

    def __init__(self, path: str, what: str) -> None:
        super().__init__(
            f"Нет доступа: не удалось {what}.",
            hint=(
                "Проверьте права на папку и что файл не открыт в другой программе. "
                "На macOS может потребоваться разрешение в «Конфиденциальность и безопасность»."
            ),
            details=f"path={path}",
        )


class SilenceError(TimpocError):
    code = CODE_SILENCE

    def __init__(self, seconds: float) -> None:
        super().__init__(
            "В выбранном диапазоне нет речи.",
            hint=(
                f"Проанализировано {seconds:.1f} с аудио: найдена только тишина. "
                "Проверьте, что выбран правильный слой/файл и что звук не выключен."
            ),
        )


class EmptyRangeError(TimpocError):
    code = CODE_RANGE_EMPTY

    def __init__(self) -> None:
        super().__init__(
            "Выбранный диапазон пустой.",
            hint="Начало диапазона должно быть раньше конца.",
        )


def wrap_unknown(exc: BaseException) -> TimpocError:
    """Обернуть непредвиденное исключение, сохранив его текст для журнала.

    Важно: текст исключения может содержать путь с кириллицей или пробелами —
    это допустимо в details, но мы НЕ показываем его как подсказку.
    """
    if isinstance(exc, TimpocError):
        return exc
    text = str(exc) or exc.__class__.__name__
    return TimpocError(
        "Произошла непредвиденная ошибка при обработке.",
        hint=(
            "Сохраните журнал диагностики (раздел Настройки → Журнал) и попробуйте "
            "повторить. Если ошибка повторяется — сообщите текст из журнала."
        ),
        details=text[:2000],
    )
