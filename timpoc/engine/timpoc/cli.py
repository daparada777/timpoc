"""
timpoc — командная строка.

Нужна для трёх задач:
  1) проверка установки без After Effects (timpoc info / timpoc doctor);
  2) распознавание из терминала — полезно для отладки таймингов;
  3) запуск сервера, который использует панель (timpoc serve).

Обычному пользователю консоль не нужна: установщик сам запускает serve.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .errors import TimpocError, wrap_unknown
from .logger import get_logger, log_path, tail_log
from .recognize import DEFAULT_MODEL, MODEL_CATALOG, detect_device, model_size_mb
from .types import Transcript


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_info(_args) -> int:
    from .server import Engine

    _print_json(Engine(get_logger("cli")).system_info())
    return 0


def cmd_models(_args) -> int:
    from .recognize import model_is_installed

    rows = []
    for name, meta in MODEL_CATALOG.items():
        rows.append(
            {
                "model": name,
                "size_mb": meta["mb"],
                "installed": model_is_installed(name),
                "russian": meta["ru"],
                "note": meta["note"],
            }
        )
    _print_json({"models": rows, "models_dir": _models_dir()})
    return 0


def _models_dir() -> str:
    from .recognize import models_dir

    return models_dir()


def cmd_download(args) -> int:
    from .recognize import download_model, model_is_installed

    model = args.model
    if model_is_installed(model) and not args.force:
        print(f"Модель «{model}» уже установлена. Используйте --force для повторной загрузки.")
        return 0
    size = model_size_mb(model)
    print(f"Скачиваем модель «{model}» (~{size} МБ). Требуется интернет.")
    last = -1

    def cb(p: float, msg: str) -> None:
        nonlocal last
        pct = int(p * 100)
        if pct != last:
            last = pct
            print(f"\r  {pct:3d}%  {msg}", end="", flush=True)

    try:
        path = download_model(model, progress=cb)
    except TimpocError as exc:
        print(f"\nОшибка: {exc.message}\nЧто делать: {exc.hint}")
        return 2
    print(f"\nГотово: {path}")
    return 0


def cmd_doctor(_args) -> int:
    """Проверка окружения: что работает, а что нет. Ничего не ломает."""
    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("Python", sys.version_info >= (3, 9), sys.version.split()[0])

    try:
        import numpy

        add("numpy", True, numpy.__version__)
    except ImportError as exc:
        add("numpy", False, str(exc))

    try:
        import faster_whisper

        add("faster-whisper", True, getattr(faster_whisper, "__version__", "?"))
    except ImportError as exc:
        add("faster-whisper", False, f"{exc} — переустановите timpoc")

    try:
        import av

        add("Декодер аудио (PyAV)", True, av.__version__)
    except ImportError as exc:
        add("Декодер аудио (PyAV)", False, str(exc))

    try:
        import ctranslate2

        cuda = 0
        try:
            cuda = int(ctranslate2.get_cuda_device_count())
        except Exception:
            cuda = 0
        add("CUDA (видеокарта)", cuda > 0, f"устройств CUDA: {cuda}")
    except ImportError as exc:
        add("CUDA (видеокарта)", False, str(exc))

    plan = detect_device("auto")
    add("Выбранное устройство", True, f"{plan.device}/{plan.compute_type} — {plan.reason}")

    from .audio import free_space_mb
    from .logger import data_dir

    free = free_space_mb(data_dir())
    add("Свободное место", free > 500, f"{free} МБ в {data_dir()}")
    add("Журнал", True, log_path())

    _print_json({"ok": all(c["ok"] for c in checks if c["check"] != "CUDA (видеокарта)"), "checks": checks})
    return 0


def cmd_transcribe(args) -> int:
    """Распознавание из терминала. Полезно для проверки таймингов."""
    from .pipeline import PipelineInput, run_pipeline
    from .recognize import RecognitionSettings
    from .segmentation import SegmentationSettings
    from .srt import write_srt

    log = get_logger("cli")

    def progress(p: float, msg: str) -> None:
        bar = "#" * int(p * 24)
        print(f"\r  [{bar:<24}] {int(p * 100):3d}% {msg}", end="", flush=True)

    inp = PipelineInput(
        path=args.audio,
        range_start=args.start or 0.0,
        range_end=args.end,
        offset_seconds=args.offset or 0.0,
        frame_rate=args.fps or 25.0,
        source_kind="file",
        source_label=os.path.basename(args.audio),
    )
    rec = RecognitionSettings(
        language=args.language,
        model=args.model,
        device=args.device,
        beam_size=args.beam,
        vad_filter=not args.no_vad,
        word_timestamps=True,
    )
    seg = SegmentationSettings(
        max_chars=args.max_chars,
        max_words=args.max_words,
        max_lines=args.lines,
        max_words_per_line=args.words_per_line,
    )

    try:
        result = run_pipeline(inp, rec, seg, progress=progress, log=log.info)
    except TimpocError as exc:
        print(f"\nОшибка: {exc.message}\nЧто делать: {exc.hint}")
        return 2
    print()

    tr = result.transcript
    print(f"Блоков: {len(tr.blocks)}  Слов: {len(tr.words)}  Язык: {tr.info.language}  "
          f"Устройство: {tr.info.device}/{tr.info.compute_type}  Время: {tr.info.elapsed_seconds} с")

    out = args.out
    if out:
        write_srt(out, tr.blocks)
        print(f"SRT: {out}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(tr.to_json(indent=2))
        print(f"JSON: {args.json}")
    if not out and not args.json:
        from .timing import format_srt_time

        for b in tr.blocks[: args.preview]:
            print(f"[{format_srt_time(b.in_time)} --> {format_srt_time(b.out_time)}] {b.text}")

    for w in tr.warnings:
        print(f"Предупреждение: {w}")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    serve(port=args.port, token=args.token, quiet=args.quiet)
    return 0


def cmd_state(_args) -> int:
    from .state import ProjectState, state_path

    st = ProjectState.load(state_path())
    _print_json({"path": state_path(), "summary": st.summary()})
    return 0


def cmd_export(args) -> int:
    """Экспорт сохранённого состояния в SRT — страховка без запуска AE."""
    from .state import ProjectState, state_path
    from .srt import write_srt

    st = ProjectState.load(args.state or state_path())
    if not st.transcript:
        print("В сохранённом состоянии нет распознанного текста.")
        return 2
    write_srt(args.out, st.transcript.blocks)
    print(f"SRT сохранён: {args.out} ({len(st.transcript.blocks)} блоков)")
    return 0


def cmd_logs(args) -> int:
    print(tail_log(args.lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timpoc",
        description="timpoc — распознавание речи и субтитры для After Effects.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Показать сведения о системе и движке").set_defaults(func=cmd_info)
    sub.add_parser("doctor", help="Проверить окружение и зависимости").set_defaults(func=cmd_doctor)
    sub.add_parser("state", help="Показать сохранённое состояние проекта").set_defaults(func=cmd_state)

    m = sub.add_parser("models", help="Список моделей и их статус")
    m.set_defaults(func=cmd_models)

    d = sub.add_parser("download", help="Скачать модель распознавания")
    d.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(MODEL_CATALOG))
    d.add_argument("--force", action="store_true", help="Перезаписать существующую модель")
    d.set_defaults(func=cmd_download)

    t = sub.add_parser("transcribe", help="Распознать аудиофайл и сохранить субтитры")
    t.add_argument("audio", help="Путь к аудио- или видеофайлу")
    t.add_argument("--start", type=float, default=0.0, help="Начало диапазона, сек")
    t.add_argument("--end", type=float, default=None, help="Конец диапазона, сек")
    t.add_argument("--offset", type=float, default=0.0, help="Смещение в композиции, сек")
    t.add_argument("--fps", type=float, default=25.0, help="Частота кадров")
    t.add_argument("--language", default="ru", choices=["ru", "en", "auto"])
    t.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(MODEL_CATALOG))
    t.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    t.add_argument("--beam", type=int, default=5)
    t.add_argument("--no-vad", action="store_true", help="Отключить фильтрацию тишины")
    t.add_argument("--max-chars", type=int, default=42)
    t.add_argument("--max-words", type=int, default=8)
    t.add_argument("--lines", type=int, default=2)
    t.add_argument("--words-per-line", type=int, default=5)
    t.add_argument("--out", default=None, help="Куда сохранить SRT")
    t.add_argument("--json", default=None, help="Куда сохранить JSON-транскрипт")
    t.add_argument("--preview", type=int, default=10, help="Сколько блоков показать в консоли")
    t.set_defaults(func=cmd_transcribe)

    s = sub.add_parser("serve", help="Запустить локальный сервер для панели")
    s.add_argument("--port", type=int, default=0, help="0 = выбрать свободный порт")
    s.add_argument("--token", default=None, help="Токен доступа (по умолчанию случайный)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_serve)

    e = sub.add_parser("export", help="Экспортировать сохранённый транскрипт в SRT")
    e.add_argument("--out", required=True)
    e.add_argument("--state", default=None)
    e.set_defaults(func=cmd_export)

    lg = sub.add_parser("logs", help="Показать конец журнала диагностики")
    lg.add_argument("--lines", type=int, default=200)
    lg.set_defaults(func=cmd_logs)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except TimpocError as exc:
        print(f"Ошибка: {exc.message}\nЧто делать: {exc.hint}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        err = wrap_unknown(exc)
        print(f"Непредвиденная ошибка: {err.message}\nПодробности в журнале: {log_path()}", file=sys.stderr)
        get_logger("cli").error("CLI-сбой: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
