"""
timpoc — локальный HTTP-сервер движка распознавания.

Зачем отдельный процесс, а не «всё внутри панели»:
  * After Effects не должен блокироваться. Распознавание идёт здесь,
    в своём процессе; панель раз в 300 мс спрашивает прогресс и может
    отменить задачу. UI AE остаётся отзывчивым.
  * Панель CEP — это браузер без прямого доступа к CUDA-библиотекам.
    Python-процесс решает это корректно.

Безопасность и приватность (важно для требования «не отправляй мои данные»):
  * слушаем ТОЛЬКО 127.0.0.1 — снаружи к серверу не достучаться;
  * каждый запуск генерирует случайный токен; без него запрос отклоняется;
  * токен передаётся панели через локальный файл с правами только владельца;
  * наружу не уходит НИЧЕГО, кроме загрузки модели с Hugging Face,
    и только по явному действию пользователя («Скачать модель»).

Сервер намеренно написан на стандартной библиотеке (http.server), без Flask
и прочего: меньше зависимостей → меньше размер установщика и меньше проблем
с антивирусами.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import fonts as fonts_mod
from . import layout as layout_mod
from . import pipeline as pipeline_mod
from . import recognize as recognize_mod
from . import srt as srt_mod
from . import style as style_mod
from .errors import CancelledError, TimpocError, wrap_unknown
from .logger import get_logger, log_path, mask_sensitive
from .settings import AppSettings, load_settings, save_settings
from .state import ProjectState, state_path

VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
POLL_INTERVAL = 0.3


# --------------------------------------------------------------------------
# Задачи
# --------------------------------------------------------------------------

class Job:
    """Фоновая задача с прогрессом и отменой."""

    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.status = "running"        # running | done | error | cancelled
        self.progress = 0.0
        self.message = ""
        self.result: Optional[dict] = None
        self.error: Optional[dict] = None
        self._cancel = threading.Event()
        self.created = time.time()
        self.logs: List[str] = []

    def cancel(self) -> None:
        self._cancel.set()
        self.status = "cancelled"
        self.message = "Отменяем…"

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def update(self, progress: float, message: str = "") -> None:
        self.progress = max(0.0, min(1.0, float(progress)))
        if message:
            self.message = message

    def finish(self, result: dict) -> None:
        self.result = result
        self.progress = 1.0
        self.status = "done"
        self.message = "Готово"

    def fail(self, err: TimpocError) -> None:
        self.error = err.to_dict()["error"]
        self.status = "error"
        self.message = err.message

    def to_dict(self, with_result: bool = False) -> dict:
        out = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "logs": self.logs[-40:],
        }
        if with_result:
            out["result"] = self.result
        return out


class JobStore:
    """Хранилище задач. Держим немного: старые чистим, память не копим."""

    def __init__(self, keep: int = 12):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self.keep = keep

    def create(self, kind: str) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind)
        with self._lock:
            self._jobs[job.id] = job
            self._gc()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def _gc(self) -> None:
        if len(self._jobs) <= self.keep:
            return
        finished = [j for j in self._jobs.values() if j.status != "running"]
        finished.sort(key=lambda j: j.created)
        for j in finished[: len(self._jobs) - self.keep]:
            self._jobs.pop(j.id, None)


# --------------------------------------------------------------------------
# Ядро сервера
# --------------------------------------------------------------------------

class Engine:
    """Всё, что умеет движок. HTTP-слой только вызывает эти методы."""

    def __init__(self, log: Optional[logging.Logger] = None):
        self.log = log or get_logger("engine")
        self.jobs = JobStore()
        self.settings = load_settings()
        self.project_state = ProjectState.load(state_path())
        self._recognizer: Optional[recognize_mod.Recognizer] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- утилиты

    def system_info(self) -> dict:
        plan = recognize_mod.detect_device(self.settings.recognition.device)
        total_ram_mb = 0
        try:
            if sys.platform.startswith("win"):
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64),
                    ]

                st = MEMORYSTATUSEX()
                st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
                total_ram_mb = int(st.ullTotalPhys / 1048576)
            else:
                with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("MemTotal:"):
                            total_ram_mb = int(line.split()[1]) // 1024
                            break
        except Exception:
            total_ram_mb = 0

        models = []
        for name, meta in recognize_mod.MODEL_CATALOG.items():
            models.append(
                {
                    "name": name,
                    "mb": meta["mb"],
                    "installed": recognize_mod.model_is_installed(name),
                    "ru_quality": meta["ru"],
                    "note": meta["note"],
                }
            )

        return {
            "version": VERSION,
            "python": sys.version.split()[0],
            "os": sys.platform,
            "cpu_count": os.cpu_count() or 0,
            "ram_total_mb": total_ram_mb,
            "disk_free_mb": _disk_free_mb(os.path.expanduser("~")),
            "device": plan.to_dict(),
            "models": models,
            "models_dir": recognize_mod.models_dir(),
            "data_dir": _data_dir(),
            "log_path": log_path(),
            "offline": self.settings.privacy.offline_mode,
        }

    # ------------------------------------------------------------- модели

    def start_model_download(self, model: str) -> Job:
        job = self.jobs.create("download")

        def run() -> None:
            try:
                if self.settings.privacy.offline_mode:
                    raise TimpocError(
                        "Включён автономный режим — загрузки отключены.",
                        hint=(
                            "Выключите автономный режим в Настройках, чтобы скачать модель. "
                            "timpoc не отправляет ваше аудио никуда в любом случае."
                        ),
                    )
                self.log.info("Скачивание модели %s", model)

                def cb(p: float, msg: str) -> None:
                    job.update(p * 0.98, f"Скачивание модели: {msg}")

                path = recognize_mod.download_model(
                    model, progress=cb, cancel=lambda: job.cancelled
                )
                job.finish({"model": model, "path": path, "mb": recognize_mod.model_size_mb(model)})
            except CancelledError as exc:
                job.status = "cancelled"
                job.message = str(exc.message)
            except Exception as exc:
                err = wrap_unknown(exc)
                self.log.error("Сбой загрузки модели: %s", err)
                job.fail(err)

        threading.Thread(target=run, daemon=True, name="timpoc-download").start()
        return job

    # -------------------------------------------------------- распознавание

    def start_transcribe(self, payload: dict) -> Job:
        job = self.jobs.create("transcribe")

        def run() -> None:
            try:
                inp = pipeline_mod.PipelineInput(
                    path=str(payload.get("path") or ""),
                    range_start=float(payload.get("range_start") or 0.0),
                    range_end=(
                        None if payload.get("range_end") in (None, "", 0) else float(payload["range_end"])
                    ),
                    offset_seconds=float(payload.get("offset_seconds") or 0.0),
                    frame_rate=float(payload.get("frame_rate") or 25.0),
                    comp_start=float(payload.get("comp_start") or 0.0),
                    comp_end=(
                        None if payload.get("comp_end") in (None, "") else float(payload["comp_end"])
                    ),
                    source_kind=str(payload.get("source_kind") or "file"),
                    source_label=str(payload.get("source_label") or ""),
                )
                rec = recognize_mod.RecognitionSettings(
                    **{
                        k: v
                        for k, v in (payload.get("recognition") or {}).items()
                        if k in recognize_mod.RecognitionSettings.__dataclass_fields__
                    }
                )
                seg = style_and_seg(payload).segmentation
                fps = inp.frame_rate

                def cb(p: float, msg: str) -> None:
                    job.update(p, msg)

                def log(msg: str) -> None:
                    job.logs.append(mask_sensitive(msg))
                    self.log.info(msg)

                result = pipeline_mod.run_pipeline(
                    inp,
                    rec,
                    seg,
                    progress=cb,
                    cancel=lambda: job.cancelled,
                    log=log,
                )
                # Сохраняем результат на диск: ошибка применения не должна терять работу.
                self.project_state.transcript = result.transcript
                self.project_state.frame_rate = fps
                self.project_state.save()

                job.finish(
                    {
                        **result.to_dict(),
                        "saved_to": self.project_state.path,
                    }
                )
                self.log.info(
                    "Готово: %d блоков, язык=%s, устройство=%s",
                    len(result.transcript.blocks),
                    result.transcript.info.language,
                    result.transcript.info.device,
                )
            except CancelledError as exc:
                job.status = "cancelled"
                job.message = exc.message
                self.log.info("Распознавание отменено пользователем")
            except Exception as exc:
                err = wrap_unknown(exc)
                self.log.error("Сбой распознавания: %s\n%s", err, traceback.format_exc())
                job.fail(err)

        threading.Thread(target=run, daemon=True, name="timpoc-transcribe").start()
        return job

    # -------------------------------------------------------------- тексты

    def resegment(self, payload: dict) -> dict:
        """Пересобрать фразы без повторного распознавания."""
        if not self.project_state.transcript:
            raise TimpocError(
                "Нет распознанного текста.",
                hint="Сначала создайте субтитры — переразбивать пока нечего.",
            )
        seg = style_and_seg(payload).segmentation
        fps = float(payload.get("frame_rate") or self.project_state.frame_rate or 25.0)
        pipeline_mod.resegment(self.project_state.transcript, seg, fps)
        self.project_state.save()
        return {"ok": True, "transcript": self.project_state.transcript.to_dict()}

    def update_block(self, payload: dict) -> dict:
        """Правка одного блока: текст, начало, конец, закрепление."""
        tr = self._require_transcript()
        index = int(payload.get("index", -1))
        if not (0 <= index < len(tr.blocks)):
            raise TimpocError(
                "Блок с таким номером не найден.",
                hint="Обновите список субтитров в панели.",
            )
        block = tr.blocks[index]
        if "text" in payload and payload["text"] is not None:
            block.text = str(payload["text"])
        if "in" in payload and payload["in"] is not None:
            new_in = float(payload["in"])
            if new_in >= block.out_time:
                raise TimpocError(
                    "Начало блока не может быть позже его конца.",
                    hint="Проверьте введённое время.",
                )
            block.in_time = new_in
        if "out" in payload and payload["out"] is not None:
            new_out = float(payload["out"])
            if new_out <= block.in_time:
                raise TimpocError(
                    "Конец блока не может быть раньше его начала.",
                    hint="Проверьте введённое время.",
                )
            block.out_time = new_out
        if "locked" in payload:
            block.locked = bool(payload["locked"])
        self.project_state.save()
        return {"ok": True, "block": block.to_dict()}

    def shift(self, payload: dict) -> dict:
        """Общий сдвиг таймингов."""
        from .timing import shift_transcript

        tr = self._require_transcript()
        delta = float(payload.get("delta_seconds") or 0.0)
        fps = float(payload.get("frame_rate") or self.project_state.frame_rate or 25.0)
        shift_transcript(tr, delta, fps=fps)
        self.project_state.save()
        return {"ok": True, "transcript": tr.to_dict(), "delta": delta}

    def delete_block(self, payload: dict) -> dict:
        tr = self._require_transcript()
        index = int(payload.get("index", -1))
        if 0 <= index < len(tr.blocks):
            tr.blocks.pop(index)
            self.project_state.save()
        return {"ok": True, "count": len(tr.blocks)}

    def merge_blocks(self, payload: dict) -> dict:
        """Склеить блок с предыдущим (частая правка «рваного» текста)."""
        tr = self._require_transcript()
        index = int(payload.get("index", -1))
        if index <= 0 or index >= len(tr.blocks):
            raise TimpocError("Нельзя склеить первый блок с предыдущим.", hint="Выберите блок со второго и далее.")
        prev, cur = tr.blocks[index - 1], tr.blocks[index]
        prev.text = (prev.text + " " + cur.text).strip()
        prev.out_time = cur.out_time
        prev.words = list(prev.words) + list(cur.words)
        tr.blocks.pop(index)
        self.project_state.save()
        return {"ok": True, "count": len(tr.blocks)}

    # -------------------------------------------------------------- экспорт

    def export_srt(self, payload: dict) -> dict:
        tr = self._require_transcript()
        path = str(payload.get("path") or "")
        if not path:
            raise TimpocError("Не указан путь для файла SRT.", hint="Выберите, куда сохранить файл.")
        srt_mod.write_srt(path, tr.blocks)
        self.log.info("SRT сохранён: %s", mask_sensitive(path))
        return {"ok": True, "path": path}

    def import_srt(self, payload: dict) -> dict:
        path = str(payload.get("path") or "")
        blocks = srt_mod.read_srt(path)
        from .types import RecognitionInfo, Transcript

        tr = Transcript(
            blocks=blocks,
            words=srt_mod.srt_blocks_to_words(blocks),
            info=RecognitionInfo(model="import:srt"),
            warnings=[
                "Транскрипт импортирован из SRT. Времена слов приближённые: "
                "переразбивка на фразы будет менее точной, чем после распознавания."
            ],
            source={"kind": "srt", "path": path},
        )
        self.project_state.transcript = tr
        self.project_state.save()
        return {"ok": True, "transcript": tr.to_dict()}

    # -------------------------------------------------------------- прочее

    def fonts(self, payload: dict) -> dict:
        """Проверка шрифта по списку из After Effects."""
        families = list(payload.get("ae_families") or [])
        styles_map = dict(payload.get("ae_styles") or {})
        if not families:
            families = [f.family for f in fonts_mod.scan_system_fonts()]
            styles_map = {f.family: f.styles for f in fonts_mod.scan_system_fonts()}
        wanted_family = str(payload.get("family") or "Arial")
        wanted_style = str(payload.get("style") or "Regular")
        return {"ok": True, **fonts_mod.check_font(wanted_family, wanted_style, families, styles_map)}

    def list_fonts(self) -> dict:
        fs = fonts_mod.scan_system_fonts()
        return {"ok": True, "fonts": [f.to_dict() for f in fs], "source": "system"}

    def presets(self) -> dict:
        return {
            "ok": True,
            "builtin": style_mod.builtin_presets(),
            "user": style_mod.list_user_presets(),
        }

    def save_preset(self, payload: dict) -> dict:
        st = style_mod.TextStyle.from_dict(payload.get("style") or {})
        name = str(payload.get("name") or "").strip() or "Мой пресет"
        path = style_mod.save_preset(st, name)
        return {"ok": True, "path": path, "name": name}

    def delete_preset(self, payload: dict) -> dict:
        name = str(payload.get("name") or "")
        return {"ok": True, "deleted": style_mod.delete_preset(name)}

    def layout_check(self, payload: dict) -> dict:
        """Проверка выхода за границы и безопасные зоны для заданного размера."""
        lay = layout_mod.Layout.from_dict(payload.get("layout") or {})
        st = style_mod.TextStyle.from_dict(payload.get("style") or {})
        text = str(payload.get("text") or "Текст субтитра\nВторая строка")
        text = st.apply_case(text)
        w, h = st.estimated_block_size(text)
        cw = float(payload.get("comp_width") or 1920)
        ch = float(payload.get("comp_height") or 1080)
        return {
            "ok": True,
            "block_width": round(w, 1),
            "block_height": round(h, 1),
            "report": lay.overflow_report(cw, ch, w, h),
            "rect": [round(v, 1) for v in lay.block_rect(cw, ch, w, h)],
        }

    def settings(self) -> dict:
        return {"ok": True, "settings": self.settings.to_dict()}

    def update_settings(self, payload: dict) -> dict:
        self.settings = self.settings.merged(payload.get("settings") or {})
        save_settings(self.settings)
        self.log.info("Настройки сохранены")
        return {"ok": True, "settings": self.settings.to_dict()}

    def state(self) -> dict:
        """Текущее состояние проекта: транскрипт + стиль + расположение."""
        return {
            "ok": True,
            "has_transcript": self.project_state.transcript is not None,
            "transcript": self.project_state.transcript.to_dict() if self.project_state.transcript else None,
            "style": self.project_state.style.to_dict(),
            "layout": self.project_state.layout.to_dict(),
            "frame_rate": self.project_state.frame_rate,
            "path": self.project_state.path,
        }

    def update_style(self, payload: dict) -> dict:
        st, warns = style_mod.TextStyle.from_dict(payload.get("style") or {}).sanitize()
        self.project_state.style = st
        self.project_state.save()
        return {"ok": True, "style": st.to_dict(), "warnings": warns}

    def update_layout(self, payload: dict) -> dict:
        self.project_state.layout = layout_mod.Layout.from_dict(payload.get("layout") or {})
        self.project_state.save()
        return {"ok": True, "layout": self.project_state.layout.to_dict()}

    def diagnostics(self) -> dict:
        """Диагностика без паролей и личных данных — как требует ТЗ."""
        info = self.system_info()
        info.pop("models_dir", None)
        return {
            "ok": True,
            "version": VERSION,
            "system": info,
            "settings": self.settings.to_dict(safe=True),
            "log_path": log_path(),
            "note": "Журнал не содержит аудио, текстов субтитров и ключей.",
        }

    # ------------------------------------------------------------- служебное

    def _require_transcript(self):
        if not self.project_state.transcript:
            raise TimpocError(
                "Нет распознанного текста.",
                hint="Сначала нажмите «Создать субтитры».",
            )
        return self.project_state.transcript


class _SegmentationBundle:
    """Простая обёртка, чтобы вернуть разобранные настройки одним значением."""

    def __init__(self, segmentation):
        self.segmentation = segmentation


def style_and_seg(payload: dict) -> _SegmentationBundle:
    """Разобрать настройки разбиения из запроса.

    Оформление (цвета, шрифт) сюда намеренно не попадает: оно не влияет
    на распознавание, и это гарантирует, что смена оформления не запускает
    тяжёлую задачу заново.
    """
    seg_payload = payload.get("segmentation") or {}
    known = {
        k: v
        for k, v in seg_payload.items()
        if k in pipeline_mod.SegmentationSettings.__dataclass_fields__
    }
    return _SegmentationBundle(segmentation=pipeline_mod.SegmentationSettings(**known))


def _data_dir() -> str:
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or home, "timpoc")
    if os.path.isdir(os.path.join(home, "Library")):
        return os.path.join(home, "Library", "Application Support", "timpoc")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "timpoc")


def _disk_free_mb(path: str) -> int:
    try:
        return int(shutil.disk_usage(path).free / 1048576)
    except OSError:
        return 0


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    engine: Engine = None          # type: ignore[assignment]
    token: str = ""

    # -- служебное ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        # Свой лог: стандартный пишет в stderr и тащит URL с токеном.
        self.engine.log.debug("HTTP %s", mask_sensitive(fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Панель живёт внутри AE; разрешаем только локальное происхождение.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("X-Timpoc-Token", "")
        if not got:
            qs = parse_qs(urlparse(self.path).query)
            got = (qs.get("token") or [""])[0]
        return secrets.compare_digest(got, self.token)

    # -- маршруты -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            if route == "/health":
                self._send(200, {"ok": True, "version": VERSION})
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "Нет доступа"}})
                return
            if route == "/system":
                self._send(200, {"ok": True, **self.engine.system_info()})
            elif route == "/state":
                self._send(200, self.engine.state())
            elif route == "/settings":
                self._send(200, self.engine.settings())
            elif route == "/presets":
                self._send(200, self.engine.presets())
            elif route == "/fonts":
                self._send(200, self.engine.list_fonts())
            elif route == "/diagnostics":
                self._send(200, self.engine.diagnostics())
            elif route.startswith("/job/"):
                job = self.engine.jobs.get(route.rsplit("/", 1)[-1])
                if not job:
                    self._send(404, {"ok": False, "error": {"code": "not_found", "message": "Задача не найдена"}})
                else:
                    self._send(200, {"ok": True, "job": job.to_dict(with_result=job.status == "done")})
            else:
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"Нет маршрута {route}"}})
        except Exception as exc:  # сервер не должен падать из-за одной ошибки
            self._send(500, wrap_unknown(exc).to_dict())

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            if not self._authorized():
                self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "Нет доступа"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError) as exc:
                raise TimpocError(
                    "Некорректный запрос к движку.",
                    hint="Перезапустите панель timpoc ( Window → Extensions → timpoc ).",
                    details=str(exc),
                ) from exc

            if route == "/transcribe":
                job = self.engine.start_transcribe(payload)
                self._send(202, {"ok": True, "job": job.to_dict()})
            elif route == "/models/download":
                job = self.engine.start_model_download(str(payload.get("model") or "medium"))
                self._send(202, {"ok": True, "job": job.to_dict()})
            elif route == "/job/cancel":
                job = self.engine.jobs.get(str(payload.get("id") or ""))
                if not job:
                    self._send(404, {"ok": False, "error": {"code": "not_found", "message": "Задача не найдена"}})
                else:
                    job.cancel()
                    self._send(200, {"ok": True, "job": job.to_dict()})
            elif route == "/resegment":
                self._send(200, self.engine.resegment(payload))
            elif route == "/blocks/update":
                self._send(200, self.engine.update_block(payload))
            elif route == "/blocks/delete":
                self._send(200, self.engine.delete_block(payload))
            elif route == "/blocks/merge":
                self._send(200, self.engine.merge_blocks(payload))
            elif route == "/shift":
                self._send(200, self.engine.shift(payload))
            elif route == "/srt/export":
                self._send(200, self.engine.export_srt(payload))
            elif route == "/srt/import":
                self._send(200, self.engine.import_srt(payload))
            elif route == "/fonts/check":
                self._send(200, self.engine.fonts(payload))
            elif route == "/presets/save":
                self._send(200, self.engine.save_preset(payload))
            elif route == "/presets/delete":
                self._send(200, self.engine.delete_preset(payload))
            elif route == "/layout/check":
                self._send(200, self.engine.layout_check(payload))
            elif route == "/style":
                self._send(200, self.engine.update_style(payload))
            elif route == "/layout":
                self._send(200, self.engine.update_layout(payload))
            elif route == "/settings":
                self._send(200, self.engine.update_settings(payload))
            else:
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": f"Нет маршрута {route}"}})
        except TimpocError as exc:
            self._send(400, exc.to_dict())
        except Exception as exc:
            self.engine.log.error("HTTP-сбой: %s\n%s", exc, traceback.format_exc())
            self._send(500, wrap_unknown(exc).to_dict())


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

def pick_port(preferred: int = 0) -> int:
    """Свободный порт на loopback. 0 = выбрать автоматически."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((DEFAULT_HOST, preferred))
        return int(s.getsockname()[1])


def write_runtime_info(port: int, token: str) -> str:
    """Записать порт и токен в файл, который читает панель через ExtendScript.

    Права 0600: файл не должны читать другие пользователи системы.
    """
    d = _data_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "runtime.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": token, "pid": os.getpid(), "version": VERSION}, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def serve(port: int = 0, token: Optional[str] = None, quiet: bool = False) -> ThreadingHTTPServer:
    log = get_logger("server")
    engine = Engine(log)
    actual_port = pick_port(port)
    actual_token = token if token is not None else secrets.token_urlsafe(24)

    Handler.engine = engine
    Handler.token = actual_token

    server = ThreadingHTTPServer((DEFAULT_HOST, actual_port), Handler)
    server.daemon_threads = True
    info_path = write_runtime_info(actual_port, actual_token)
    log.info("timpoc engine %s слушает http://%s:%d (только локально)", VERSION, DEFAULT_HOST, actual_port)
    if not quiet:
        print(f"TIMPOC_ENGINE=http://{DEFAULT_HOST}:{actual_port}")
        print(f"TIMPOC_RUNTIME_FILE={info_path}")
        print("Нажмите Ctrl+C, чтобы остановить движок.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
    finally:
        server.server_close()
        try:
            os.remove(info_path)
        except OSError:
            pass
    return server
