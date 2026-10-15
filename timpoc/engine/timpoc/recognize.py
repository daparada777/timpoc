"""
timpoc — работа с движком распознавания (faster-whisper).

Почему faster-whisper, а не «просто Whisper»:
  * та же модель и та же точность, но в разы быстрее и заметно меньше памяти;
  * int8 на CPU/GPU — работает даже без видеокарты;
  * лицензия MIT, модель Whisper — MIT. Ничего платного для пользователя.

Три вещи, которые этот модуль обязан делать правильно:
 1) НЕ блокировать After Effects. Распознавание идёт в отдельном процессе
    (см. server.py), панель только спрашивает прогресс по HTTP.
 2) Быть отменяемым. faster-whisper отдаёт сегменты лениво (генератор),
    поэтому отмена проверяется между сегментами — модель не «висит».
 3) Не накапливать рассинхронизацию на длинных видео. Для этого файл
    обрабатывается окнами с перекрытием (chunked_transcribe), а не одним
    непрерывным прогоном.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from .errors import (
    CancelledError,
    ModelDownloadError,
    ModelMissingError,
    NoGpuError,
    OutOfMemoryError,
    TimpocError,
    wrap_unknown,
)
from .types import RecognitionInfo, Word

# Справочник моделей. Размер на диске указан для честного предупреждения
# перед скачиванием — пользователь должен знать, сколько качать.
MODEL_CATALOG: dict = {
    "tiny": {"mb": 75, "ru": "низкое", "note": "быстрый черновик, много ошибок в тексте"},
    "base": {"mb": 145, "ru": "низкое", "note": "черновик"},
    "small": {"mb": 480, "ru": "среднее", "note": "разумный минимум для русского"},
    "medium": {"mb": 1530, "ru": "хорошее", "note": "рекомендуется для 4-6 ГБ GPU"},
    "large-v3": {"mb": 3100, "ru": "лучшее", "note": "нужно ~3 ГБ свободной памяти"},
    "large-v3-turbo": {"mb": 1620, "ru": "среднее", "note": "быстро, но модель заточена под английский"},
}

DEFAULT_MODEL = "medium"


def model_size_mb(model: str) -> int:
    return int(MODEL_CATALOG.get(model, {}).get("mb", 0))


@dataclass
class DevicePlan:
    """Что реально доступно на этой машине и что мы выбрали."""

    device: str            # "cpu" | "cuda" | "mps"
    compute_type: str      # "int8" | "float16" | "int8_float16"
    reason: str
    gpu_name: str = ""
    gpu_free_mb: int = 0
    threads: int = 0
    downgraded_from_gpu: bool = False

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "compute_type": self.compute_type,
            "reason": self.reason,
            "gpu_name": self.gpu_name,
            "gpu_free_mb": self.gpu_free_mb,
            "threads": self.threads,
            "downgraded_from_gpu": self.downgraded_from_gpu,
        }


def detect_device(prefer: str = "auto", min_gpu_mb: int = 2500) -> DevicePlan:
    """Выбрать устройство честно, с объяснением.

    prefer: "auto" | "cpu" | "cuda"
    Мы НЕ обещаем GPU: если ctranslate2 не видит CUDA, возвращаем CPU и
    пишем причину в reason — панель покажет это пользователю, а не упадёт.
    """
    threads = max(1, (os.cpu_count() or 2))
    # Ограничиваем потоки: быстрее не станет, а After Effects начнёт тормозить.
    threads = min(threads, 8)

    if prefer == "cpu":
        return DevicePlan("cpu", "int8", "Пользователь выбрал процессор (CPU).", threads=threads)

    try:
        import ctranslate2  # type: ignore
    except ImportError as exc:
        return DevicePlan(
            "cpu",
            "int8",
            f"Библиотека ускорения не загрузилась ({exc.__class__.__name__}). Работаем на CPU.",
            threads=threads,
            downgraded_from_gpu=True,
        )

    cuda_ok = False
    try:
        cuda_ok = bool(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        cuda_ok = False

    if not cuda_ok:
        return DevicePlan(
            "cpu",
            "int8",
            "Видеокарта с поддержкой CUDA не найдена или драйвер недоступен. Работаем на процессоре.",
            threads=threads,
            downgraded_from_gpu=prefer != "auto",
        )

    gpu_name, gpu_free = _cuda_device_info()
    if gpu_free and gpu_free < min_gpu_mb:
        return DevicePlan(
            "cpu",
            "int8",
            f"Видеокарта найдена ({gpu_name}), но свободно всего ~{gpu_free} МБ — "
            "модель может не поместиться. Работаем на процессоре.",
            gpu_name=gpu_name,
            gpu_free_mb=gpu_free,
            threads=threads,
            downgraded_from_gpu=True,
        )

    # На GPU float16 быстрее; если памяти мало — int8_float16.
    compute = "float16" if (gpu_free == 0 or gpu_free >= 5000) else "int8_float16"
    return DevicePlan(
        "cuda",
        compute,
        f"Используется видеокарта {gpu_name or 'CUDA'}.",
        gpu_name=gpu_name,
        gpu_free_mb=gpu_free,
        threads=threads,
    )


def _cuda_device_info() -> Tuple[str, int]:
    """Имя и свободная память видеокарты. Пустые значения, если не удалось."""
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0].split(",")
            name = first[0].strip()
            free = int(float(first[1].strip())) if len(first) > 1 else 0
            return name, free
    except Exception:
        pass
    return "", 0


# --------------------------------------------------------------------------
# Модели: наличие и скачивание
# --------------------------------------------------------------------------

def models_dir() -> str:
    """Папка моделей. Отдельная от настроек, чтобы удаление timpoc их не трогало."""
    base = os.environ.get("TIMPOC_MODELS_DIR")
    if base:
        return os.path.abspath(base)
    home = os.path.expanduser("~")
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or home
        return os.path.join(root, "timpoc", "models")
    return os.path.join(home, "Library", "Application Support", "timpoc", "models") \
        if os.path.isdir(os.path.join(home, "Library")) \
        else os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share")), "timpoc", "models")


def model_local_path(model: str) -> Optional[str]:
    """Локальный путь модели, если она уже скачана, иначе None.

    Смотрим ОБЕ папки: нашу (models_dir(), куда кладёт download_model) и
    стандартный кеш Hugging Face — пользователь мог скачать модель раньше
    другим инструментом, и заставлять его качать повторно неправильно.

    Раньше здесь был только стандартный кеш, из-за чего модель, скачанная
    нашей же кнопкой, считалась отсутствующей. Баг проявлялся только когда
    TIMPOC_MODELS_DIR указывал на стандартный кеш — то есть в тестах его
    не было видно.
    """
    from huggingface_hub import scan_cache_dir  # локальный импорт: не тянем hub зря

    repo = f"Systran/faster-whisper-{model}"
    roots = []
    our = models_dir()
    if our:
        roots.append(our)
    default = os.environ.get("HF_HOME")
    roots.append(os.path.join(default, "hub") if default else None)
    roots.append(None)  # None = стандартный кеш huggingface_hub

    for root in roots:
        try:
            info = scan_cache_dir(cache_dir=root) if root else scan_cache_dir()
        except Exception:
            continue
        for repo_info in info.repos:
            if repo_info.repo_id != repo or not repo_info.revisions:
                continue
            newest = max(repo_info.revisions, key=lambda r: r.last_modified)
            if newest.snapshot_path and os.path.isdir(newest.snapshot_path):
                return newest.snapshot_path
    return None


def model_is_installed(model: str) -> bool:
    return model_local_path(model) is not None


def download_model(
    model: str,
    progress: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> str:
    """Скачать модель с Hugging Face. Возвращает путь.

    Честно про ограничения:
      * нужен интернет и доступ к huggingface.co;
      * размер показываем заранее (model_size_mb);
      * отмена поддерживается между чанками загрузки.
    """
    from .audio import ensure_free_space

    need = model_size_mb(model)
    ensure_free_space(models_dir(), need + 200)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ModelDownloadError(model, f"нет huggingface_hub: {exc}") from exc

    os.makedirs(models_dir(), exist_ok=True)
    repo = f"Systran/faster-whisper-{model}"

    def _cb(downloaded: int, total: int) -> None:
        if cancel and cancel():
            raise CancelledError("Загрузка модели отменена.")
        if progress and total:
            progress(downloaded / total, f"{downloaded // 1048576} из {total // 1048576} МБ")

    try:
        path = snapshot_download(
            repo_id=repo,
            cache_dir=models_dir(),
            local_dir=None,
            max_workers=2,
        )
    except CancelledError:
        raise
    except Exception as exc:
        raise ModelDownloadError(model, f"{exc.__class__.__name__}: {exc}") from exc

    if progress:
        progress(1.0, "Готово")
    return path


# --------------------------------------------------------------------------
# Распознавание
# --------------------------------------------------------------------------

@dataclass
class RecognitionSettings:
    language: str = "ru"          # "ru" | "en" | "auto"
    model: str = DEFAULT_MODEL
    device: str = "auto"          # "auto" | "cpu" | "cuda"
    compute_type: str = ""        # "" = выбрать автоматически
    beam_size: int = 5
    vad_filter: bool = True
    vad_min_silence_ms: int = 400
    word_timestamps: bool = True
    temperature_fallback: bool = True
    # Окна обработки длинного аудио (защита от накопления рассинхронизации).
    chunk_seconds: float = 600.0
    chunk_overlap_seconds: float = 10.0
    initial_prompt: str = ""

    def validate(self) -> List[str]:
        warns: List[str] = []
        if self.model not in MODEL_CATALOG:
            warns.append(f"Неизвестная модель «{self.model}» — использована {DEFAULT_MODEL}.")
        if self.language not in ("ru", "en", "auto"):
            warns.append(f"Язык «{self.language}» не поддерживается явно — использовано автоопределение.")
        if self.chunk_seconds < 60:
            warns.append("Окно обработки меньше 60 с — увеличено до 60 с.")
        if self.chunk_overlap_seconds >= self.chunk_seconds / 2:
            warns.append("Перекрытие окон слишком большое — уменьшено.")
        if self.beam_size < 1:
            warns.append("beam_size меньше 1 — использовано 1.")
        return warns

    def sanitized(self) -> "RecognitionSettings":
        s = RecognitionSettings(**self.__dict__)
        if s.model not in MODEL_CATALOG:
            s.model = DEFAULT_MODEL
        if s.language not in ("ru", "en", "auto"):
            s.language = "auto"
        s.chunk_seconds = max(60.0, float(s.chunk_seconds))
        s.chunk_overlap_seconds = min(max(0.0, float(s.chunk_overlap_seconds)), s.chunk_seconds / 3.0)
        s.beam_size = max(1, int(s.beam_size))
        s.vad_min_silence_ms = max(100, int(s.vad_min_silence_ms))
        return s


class Recognizer:
    """Обёртка над WhisperModel с ленивой загрузкой и отменой.

    Модель грузится один раз на процесс и переиспользуется между задачами:
    повторное распознавание того же проекта не ждёт загрузки снова.
    """

    def __init__(self, settings: RecognitionSettings, log: Optional[Callable[[str], None]] = None):
        self.settings = settings.sanitized()
        self._log = log or (lambda msg: None)
        self._model = None
        self._plan: Optional[DevicePlan] = None
        self._loaded_key: Optional[Tuple[str, str, str]] = None

    @property
    def plan(self) -> DevicePlan:
        if self._plan is None:
            self._plan = detect_device(self.settings.device)
        return self._plan

    def ensure_model(self, cancel: Optional[Callable[[], bool]] = None) -> None:
        """Загрузить модель в память. Бросает понятные ошибки."""
        s = self.settings
        plan = self.plan
        key = (s.model, plan.device, plan.compute_type or "auto")
        if self._model is not None and self._loaded_key == key:
            return

        if not model_is_installed(s.model):
            # Автоматически не качаем: скачивание — осознанное действие пользователя.
            raise ModelMissingError(s.model, model_size_mb(s.model))

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TimpocError(
                "Движок распознавания не установлен.",
                hint="Переустановите timpoc: движок идёт в комплекте, ставить Python не нужно.",
                details=str(exc),
            ) from exc

        compute = plan.compute_type or s.compute_type or "int8"
        self._log(f"Загрузка модели {s.model} на {plan.device}/{compute}")
        try:
            self._model = WhisperModel(
                s.model,
                device=plan.device,
                compute_type=compute,
                cpu_threads=plan.threads or 0,
                download_root=models_dir(),
            )
        except MemoryError as exc:
            raise OutOfMemoryError(f"модель «{s.model}»") from exc
        except RuntimeError as exc:
            text = str(exc).lower()
            if plan.device == "cuda" and ("cuda" in text or "cudnn" in text or "cublas" in text):
                # Честно падаем в CPU вместо «непонятной ошибки».
                self._log("CUDA недоступна в рантайме — переключаюсь на CPU")
                raise NoGpuError() from exc
            raise TimpocError(
                "Не удалось загрузить модель распознавания.",
                hint="Попробуйте модель меньше или перезапустите timpoc.",
                details=str(exc),
            ) from exc
        except Exception as exc:
            raise wrap_unknown(exc) from exc

        self._loaded_key = key

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int,
        offset_seconds: float = 0.0,
        progress: Optional[Callable[[float, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[Word], RecognitionInfo, List[str]]:
        """Распознать массив аудио. Возвращает (слова, метаданные, предупреждения).

        audio  — float32 моно, частота sample_rate.
        offset_seconds — сколько секунд композиции прошло до начала этого аудио.
                         Все возвращённые времена уже сдвинуты на offset_seconds.
        """
        self.ensure_model(cancel)
        s = self.settings
        warnings: List[str] = []
        assert self._model is not None

        total = float(audio.size) / float(sample_rate)
        if total <= 0:
            from .errors import SilenceError

            raise SilenceError(0.0)

        started = time.time()
        all_words: List[Word] = []
        language = ""
        language_prob = 0.0
        processed = 0.0

        for chunk, chunk_start in self._windows(audio, sample_rate):
            if cancel and cancel():
                raise CancelledError()

            chunk_dur = float(chunk.size) / float(sample_rate)
            self._log(f"Окно {chunk_start:.1f}–{chunk_start + chunk_dur:.1f} с")

            kwargs = dict(
                beam_size=s.beam_size,
                word_timestamps=s.word_timestamps,
                vad_filter=s.vad_filter,
                condition_on_previous_text=True,
            )
            if s.vad_filter:
                kwargs["vad_parameters"] = {"min_silence_duration_ms": s.vad_min_silence_ms}
            if s.language and s.language != "auto":
                kwargs["language"] = s.language
            if s.initial_prompt:
                kwargs["initial_prompt"] = s.initial_prompt
            if not s.temperature_fallback:
                kwargs["temperature"] = 0.0

            try:
                segments, info = self._model.transcribe(chunk, **kwargs)
                segments = list(self._guarded(segments, cancel, chunk_start, total, processed, progress))
            except CancelledError:
                raise
            except MemoryError as exc:
                raise OutOfMemoryError("память при распознавании") from exc
            except Exception as exc:
                raise wrap_unknown(exc) from exc

            language = language or (info.language or "")
            language_prob = max(language_prob, float(info.language_probability or 0.0))

            keep_from, keep_to = self._keep_range(
                chunk_start, chunk_dur, total, inp_offset=offset_seconds
            )
            for seg in segments:
                for w in seg.words or []:
                    wtext = (w.word or "").strip()
                    if not wtext:
                        continue
                    abs_start = offset_seconds + chunk_start + float(w.start)
                    abs_end = offset_seconds + chunk_start + float(w.end)
                    if abs_start < keep_from or abs_start >= keep_to:
                        continue  # слово из зоны перекрытия — берём из «своего» окна
                    all_words.append(
                        Word(
                            text=wtext,
                            start=abs_start,
                            end=max(abs_end, abs_start + 0.01),
                            probability=None if w.probability is None else float(w.probability),
                        )
                    )

            processed += chunk_dur
            if progress:
                progress(min(1.0, processed / total), "Распознавание речи")

        if not all_words:
            from .errors import SilenceError

            raise SilenceError(total)

        # Стабильный порядок — иначе разборка на фразы может дать разные результаты.
        all_words.sort(key=lambda w: (w.start, w.end))

        info_out = RecognitionInfo(
            language=language,
            language_probability=round(language_prob, 4),
            model=s.model,
            device=self.plan.device,
            compute_type=self.plan.compute_type,
            duration_seconds=round(total, 3),
            elapsed_seconds=round(time.time() - started, 2),
            vad_used=bool(s.vad_filter),
        )

        if language_prob and language_prob < 0.6:
            warnings.append(
                f"Язык определён неуверенно ({language}, вероятность {language_prob:.2f}). "
                "Если текст выглядит чужим языком — выберите язык вручную в Настройках."
            )
        if self.plan.downgraded_from_gpu:
            warnings.append(self.plan.reason)

        return all_words, info_out, warnings

    # -- внутренние помощники -------------------------------------------------

    def _guarded(self, segments, cancel, chunk_start, total, processed, progress):
        """Проксировать генератор сегментов, проверяя отмену между ними."""
        seen = 0
        for seg in segments:
            if cancel and cancel():
                raise CancelledError()
            seen += 1
            # Лёгкий прогресс внутри окна (по номеру сегмента — приблизительно).
            if progress and total > 0 and seen % 5 == 0:
                progress(min(0.999, (processed + chunk_start) / max(total, 1e-6)), "Распознавание речи")
            yield seg

    def _windows(self, audio: np.ndarray, sample_rate: int):
        """Разбить аудио на окна с перекрытием.

        Зачем: один непрерывный прогон на длинном видео накапливает ошибки
        выравнивания. Окна с перекрытием + отсечение «чужих» слов дают
        предсказуемую точность на любом хронометраже.
        """
        s = self.settings
        win = int(s.chunk_seconds * sample_rate)
        step = max(1, win - int(s.chunk_overlap_seconds * sample_rate))
        if audio.size <= win:
            yield audio, 0.0
            return
        pos = 0
        while pos < audio.size:
            yield audio[pos : pos + win], pos / float(sample_rate)
            if pos + win >= audio.size:
                break
            pos += step

    def _keep_range(
        self, chunk_start: float, chunk_dur: float, total: float, inp_offset: float = 0.0
    ) -> Tuple[float, float]:
        """Какой кусок окна считать «своим» (зоны перекрытия отдаём соседям).

        Возвращаем границы ВО ВРЕМЕНИ КОМПОЗИЦИИ, то есть с учётом inp_offset.
        Раньше границы считались без смещения, и при любом ненулевом смещении
        (распознавание не с начала композиции) под фильтр попадали ВСЕ слова —
        результат получался пустым. Найдено тестом на живом движке.

        Отдельно: половина перекрытия вычитается только если окно длиннее
        перекрытия вдвое, иначе keep_to уходил в отрицательные значения.
        """
        overlap = self.settings.chunk_overlap_seconds
        half = overlap / 2.0 if (overlap and chunk_dur > 2 * overlap) else 0.0

        if chunk_start == 0.0:
            rel_from, rel_to = 0.0, chunk_dur - half
        else:
            rel_from = chunk_start + half
            rel_to = chunk_start + chunk_dur

        if chunk_start + chunk_dur >= total:
            rel_to = float("inf")

        return inp_offset + rel_from, inp_offset + rel_to


def transcribe_file(
    path: str,
    start: float,
    end: Optional[float],
    settings: RecognitionSettings,
    offset_seconds: float = 0.0,
    progress: Optional[Callable[[float, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
):
    """Удобная сквозная функция: файл → слова. Используется CLI и сервером."""
    from .audio import analyze_silence, load_audio, require_speech

    if progress:
        progress(0.02, "Чтение аудио")
    audio, sr = load_audio(path, start=start, end=end, sample_rate=TARGET_SAMPLE_RATE_DEFAULT)
    if progress:
        progress(0.06, "Проверка на тишину")
    report = analyze_silence(audio, int(sr))
    require_speech(report)

    rec = Recognizer(settings, log=log)
    words, info, warnings = rec.transcribe(
        audio,
        int(sr),
        offset_seconds=offset_seconds,
        progress=lambda p, m: progress(0.1 + p * 0.9, m) if progress else None,
        cancel=cancel,
    )
    return words, info, warnings, report


TARGET_SAMPLE_RATE_DEFAULT = 16000
