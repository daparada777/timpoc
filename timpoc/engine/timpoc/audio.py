"""
timpoc — подготовка аудио перед распознаванием.

Отвечает на три практических вопроса:
 1) Как прочитать звук и не зависеть от установленного FFmpeg.
    → используем PyAV (av): он идёт вместе с faster-whisper и содержит
      все нужные кодеки внутри. Пользователю ничего ставить не нужно.
 2) Как узнать длительность/частоту, чтобы честно посчитать тайминги.
    → probe_audio().
 3) Как понять, что в диапазоне тишина, ДО запуска тяжёлой модели.
    → silence_report(): дешёвый проход по RMS-огибающей.

Про «растяжение времени и сложный микс»: мы НЕ пытаемся угадать итоговый
звук композиции по параметрам слоёв. Это принципиально ненадёжно
(time remap, экспрешены, эффекты на звуке). Поэтому есть два честных пути,
и оба реализованы в проекте:
  a) kind="layer" + явный stretch — простой случай, считается точно;
  b) kind="comp" + предварительный рендер звука средствами After Effects
     (хост-скрипт делает RAM preview / экспорт в WAV) — мы распознаём
     ровно то, что слышно в композиции. См. host/timpoc_host.jsx, audioExport.
Если ни то, ни другое не подходит, возвращаем предупреждение, а не молчим.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .errors import (
    CorruptFileError,
    NoDiskSpaceError,
    PermissionError_,
    SilenceError,
    TimpocError,
    wrap_unknown,
)

TARGET_SAMPLE_RATE = 16000  # Whisper работает на 16 кГц — выше не нужно


@dataclass
class AudioProbe:
    path: str
    duration: float
    sample_rate: int
    channels: int
    codec: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration": round(self.duration, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "codec": self.codec,
        }


def _check_readable(path: str) -> None:
    if not path:
        raise TimpocError(
            "Не указан файл аудио.",
            hint="Выберите аудиофайл или слой со звуком.",
        )
    # Пути с пробелами и кириллицей — норма, поэтому НЕ нормализуем агрессивно.
    # Используем os.path напрямую: он корректно работает с Unicode в Python 3.
    if not os.path.exists(path):
        raise TimpocError(
            "Файл не найден.",
            hint="Возможно, файл переместили или переименовали. Выберите его заново.",
            details=f"path={path}",
        )
    if os.path.isdir(path):
        raise TimpocError(
            "Указана папка, а не файл.",
            hint="Выберите конкретный аудиофайл (wav, mp3, m4a и т. п.).",
            details=f"path={path}",
        )
    if not os.access(path, os.R_OK):
        raise PermissionError_(path, "прочитать аудиофайл")


def probe_audio(path: str) -> AudioProbe:
    """Быстро узнать параметры файла, не декодируя его целиком."""
    _check_readable(path)
    try:
        import av  # локальный импорт: панель может звать probe без полной загрузки
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise TimpocError(
            "Компонент декодирования аудио не установлен.",
            hint="Переустановите timpoc — компонент идёт в комплекте.",
            details=str(exc),
        ) from exc

    try:
        with av.open(path) as container:  # type: ignore[arg-type]
            stream = container.streams.audio[0]
            duration = 0.0
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = float(container.duration) / 1_000_000.0
            codec = getattr(stream.codec_context, "name", "") or ""
            return AudioProbe(
                path=path,
                duration=duration,
                sample_rate=int(stream.rate or 0),
                channels=int(stream.channels or 0),
                codec=str(codec),
            )
    except Exception as exc:
        raise CorruptFileError(path, str(exc)) from exc


def load_audio(
    path: str,
    start: float = 0.0,
    end: Optional[float] = None,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> Tuple[np.ndarray, float]:
    """Декодировать фрагмент файла в моно float32 с частотой sample_rate.

    Возвращает (массив отсчётов, фактическая частота).
    Декодирование потоковое: длинное видео не загоняется в память целиком.

    start/end — в секундах ОТНОСИТЕЛЬНО ФАЙЛА.
    """
    _check_readable(path)
    if end is not None and end <= start:
        from .errors import EmptyRangeError

        raise EmptyRangeError()

    try:
        import av
    except ImportError as exc:  # pragma: no cover
        raise TimpocError(
            "Компонент декодирования аудио не установлен.",
            hint="Переустановите timpoc — компонент идёт в комплекте.",
            details=str(exc),
        ) from exc

    chunks: List[np.ndarray] = []
    try:
        with av.open(path) as container:  # type: ignore[arg-type]
            stream = container.streams.audio[0]
            stream.thread_type = "AUTO"

            if start > 0:
                # Seek по ключевому кадру, затем отбрасываем «хвост» до start.
                time_base = stream.time_base
                if time_base is not None:
                    container.seek(int(start / float(time_base)), stream=stream, any_frame=False)

            resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)

            for frame in container.decode(stream):
                frame_time = float(frame.pts * stream.time_base) if frame.pts is not None else 0.0
                frame_dur = float(frame.samples) / float(frame.rate or sample_rate)
                frame_end = frame_time + frame_dur

                if frame_end <= start:
                    continue
                if end is not None and frame_time >= end:
                    break

                for res in resampler.resample(frame):
                    arr = res.to_ndarray()
                    if arr.size == 0:
                        continue
                    # to_ndarray даёт форму (channels, samples) или (1, samples) для моно
                    arr = arr.reshape(-1) if arr.ndim == 1 else arr.mean(axis=0)
                    chunks.append(np.asarray(arr, dtype=np.float32))

                if end is not None and frame_end >= end:
                    break

            for res in resampler.resample(None):  # сброс буфера ресемплера
                arr = res.to_ndarray()
                if arr.size:
                    chunks.append(np.asarray(arr.reshape(-1), dtype=np.float32))

    except MemoryError as exc:  # pragma: no cover
        from .errors import OutOfMemoryError

        raise OutOfMemoryError("аудио") from exc
    except OSError as exc:
        if exc.errno == 28:  # ENOSPC
            raise NoDiskSpaceError(0, 0, path) from exc
        raise CorruptFileError(path, str(exc)) from exc
    except Exception as exc:
        raise CorruptFileError(path, str(exc)) from exc

    if not chunks:
        return np.zeros(0, dtype=np.float32), float(sample_rate)

    audio = np.concatenate(chunks)

    # Точная обрезка по start, если seek привёл к лишним отсчётам в начале.
    if start > 0:
        probe = probe_audio(path)
        # Если контейнер отдал больше, чем нужно, подрезаем по факту.
        expected = int(round((min(end, probe.duration) if end else probe.duration) - start) * sample_rate)
        if expected > 0 and audio.size > expected + sample_rate:
            audio = audio[:expected]

    # Защита от клиппинга выше |1| — Whisper от этого деградирует.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak

    return audio.astype(np.float32, copy=False), float(sample_rate)


@dataclass
class SilenceReport:
    """Что нашлось в аудио до запуска модели."""

    duration: float
    speech_seconds: float
    silence_seconds: float
    peak_rms: float
    is_silent: bool
    rms_threshold: float
    loudest_at: float  # секунда с максимальной громкостью — для подсказки

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "speech_seconds": round(self.speech_seconds, 2),
            "silence_seconds": round(self.silence_seconds, 2),
            "peak_rms": round(self.peak_rms, 6),
            "is_silent": self.is_silent,
            "rms_threshold": self.rms_threshold,
            "loudest_at": round(self.loudest_at, 3),
        }


def rms_envelope(audio: np.ndarray, sample_rate: int, window_ms: int = 100) -> np.ndarray:
    """RMS-огибающая с шагом window_ms миллисекунд."""
    if audio.size == 0:
        return np.zeros(0, dtype=np.float32)
    win = max(1, int(sample_rate * window_ms / 1000))
    n = audio.size // win
    if n == 0:
        return np.array([float(np.sqrt(np.mean(np.square(audio))))], dtype=np.float32)
    trimmed = audio[: n * win].reshape(n, win)
    return np.sqrt(np.mean(np.square(trimmed.astype(np.float64)), axis=1)).astype(np.float32)


def analyze_silence(
    audio: np.ndarray,
    sample_rate: int,
    rms_threshold: float = 0.0035,
    window_ms: int = 100,
) -> SilenceReport:
    """Определить, есть ли в сигнале звук вообще.

    Порог задан в линейных единицах (0..1). 0.0035 ≈ -49 dBFS — очень тихий
    фон уже считается «не тишиной», чтобы не резать шёпот и тихую речь.
    """
    env = rms_envelope(audio, sample_rate, window_ms)
    if env.size == 0:
        return SilenceReport(0.0, 0.0, 0.0, 0.0, True, rms_threshold, 0.0)

    step = window_ms / 1000.0
    voiced = env > rms_threshold
    speech = float(voiced.sum()) * step
    silence = float((~voiced).sum()) * step
    loudest_idx = int(np.argmax(env))
    # Тишина = речи почти нет (меньше 0.4 с на весь фрагмент).
    is_silent = speech < 0.4

    return SilenceReport(
        duration=float(env.size * step),
        speech_seconds=speech,
        silence_seconds=silence,
        peak_rms=float(env.max()),
        is_silent=is_silent,
        rms_threshold=rms_threshold,
        loudest_at=loudest_idx * step,
    )


def require_speech(report: SilenceReport) -> None:
    """Поднять понятную ошибку, если распознавать нечего."""
    if report.is_silent:
        raise SilenceError(report.duration)


def free_space_mb(path: str) -> int:
    """Свободное место в МБ для папки path (или её ближайшего существующего родителя)."""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        st = os.statvfs(p)  # type: ignore[attr-defined]
        return int(st.f_bavail * st.f_frsize / (1024 * 1024))
    except (AttributeError, OSError):  # Windows не имеет statvfs
        try:
            import shutil

            return int(shutil.disk_usage(p).free / (1024 * 1024))
        except OSError as exc:
            raise wrap_unknown(exc) from exc


def ensure_free_space(path: str, need_mb: int) -> None:
    free = free_space_mb(path)
    if free < need_mb:
        raise NoDiskSpaceError(need_mb, free, path)


def dbfs_from_linear(value: float) -> float:
    """Линейная амплитуда → dBFS (для отображения в панели)."""
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value)
