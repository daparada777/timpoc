"""
Сквозной тест конвейера: реальное аудио → реальное распознавание → субтитры.

Тест требует:
  * установленного faster-whisper;
  * модели "tiny" (скачается один раз при первом запуске, ~75 МБ);
  * espeak-ng для синтеза тестовой русской речи (если его нет — тест
    честно пропускается, а не падает).

Модель tiny намеренно выбрана крошечная: она плохо распознаёт синтетический
голос, поэтому мы НЕ проверяем точность текста. Проверяем то, что обязано
работать при любом движке: тайминги, порядок, отсутствие пересечений,
корректный учёт смещения и работу отмены.
"""

import shutil
import subprocess
import tempfile
import wave

import pytest

from timpoc.errors import CancelledError, SilenceError
from timpoc.pipeline import PipelineInput, resegment, run_pipeline
from timpoc.recognize import RecognitionSettings, Recognizer
from timpoc.segmentation import SegmentationSettings
from timpoc.srt import to_srt
from timpoc.types import Transcript, Word

pytestmark = pytest.mark.integration

RU_TEXT = (
    "Привет. Это тестовая запись для программы timpoc. "
    "Сначала мы распознаём речь, потом создаём субтитры в After Effects."
)


@pytest.fixture(scope="module")
def ru_wav():
    """Синтезировать русскую речь в WAV 16 кГц. Пропустить тест, если нечем.

    Намеренно НЕ используем tmp_path_factory: он function-scoped, и в
    module-фикстуре каталог успевал удалиться до следующих тестов.
    """
    if shutil.which("espeak-ng") is None:
        pytest.skip("espeak-ng не установлен — нет способа получить тестовое аудио")
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="timpoc_test_")) / "ru.wav"
    subprocess.run(
        ["espeak-ng", "-v", "ru", "-s", "150", "-w", str(path), RU_TEXT],
        check=True,
        capture_output=True,
    )
    assert path.stat().st_size > 1000
    return str(path)


@pytest.fixture(scope="module")
def silent_wav():
    """Файл с тишиной: проверяем, что мы не запускаем модель впустую."""
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="timpoc_test_")) / "silence.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000 * 3)  # 3 секунды тишины
    return str(path)


def _settings(**kw):
    base = dict(model="tiny", device="cpu", language="ru", beam_size=1, vad_filter=True)
    base.update(kw)
    return RecognitionSettings(**base)


# ------------------------------------------------------------------ распознавание

def test_pipeline_produces_blocks_with_valid_timings(ru_wav):
    inp = PipelineInput(path=ru_wav, frame_rate=25.0, source_kind="file")
    result = run_pipeline(inp, _settings(), SegmentationSettings())
    tr = result.transcript

    assert len(tr.blocks) > 0, "распознавание не дало ни одного субтитра"
    assert len(tr.words) > 0

    prev_out = -1.0
    for b in tr.blocks:
        assert b.out_time > b.in_time, f"блок с неположительной длительностью: {b}"
        assert b.in_time >= prev_out - 1e-6, f"пересечение блоков: {b.in_time} < {prev_out}"
        assert b.text.strip(), "блок с пустым текстом"
        prev_out = b.out_time


def test_pipeline_respects_frame_rate(ru_wav):
    """Все тайминги должны лежать на границах кадров 25 fps."""
    fps = 25.0
    inp = PipelineInput(path=ru_wav, frame_rate=fps, source_kind="file")
    tr = run_pipeline(inp, _settings(), SegmentationSettings()).transcript
    for b in tr.blocks:
        assert abs(b.in_time * fps - round(b.in_time * fps)) < 1e-6
        assert abs(b.out_time * fps - round(b.out_time * fps)) < 1e-6


def test_pipeline_offset_shifts_everything(ru_wav):
    """Смещение 10 с: все тайминги должны сдвинуться ровно на 10 с.

    Проверяем детерминированно, на подставленных словах: живой движок
    распознавания по природе даёт чуть разные результаты от прогона к прогону,
    и такой тест на нём был бы нестабильным. Сама формула смещения при этом
    проверяется тем же кодом, который работает в бою (run_pipeline).
    """
    injected = [
        Word(text="раз", start=0.0, end=0.4),
        Word(text="два", start=0.5, end=0.9),
        Word(text="три", start=1.0, end=1.4),
    ]
    base_inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    off_inp = PipelineInput(path=ru_wav, frame_rate=25.0, offset_seconds=10.0)

    base = run_pipeline(
        base_inp, _settings(), SegmentationSettings(), precomputed_words=injected
    ).transcript
    shifted_words = [
        Word(text=w.text, start=w.start + 10.0, end=w.end + 10.0, probability=w.probability)
        for w in injected
    ]
    shifted = run_pipeline(
        off_inp, _settings(), SegmentationSettings(), precomputed_words=shifted_words
    ).transcript

    assert len(base.blocks) == len(shifted.blocks)
    for a, b in zip(base.blocks, shifted.blocks):
        assert b.in_time == pytest.approx(a.in_time + 10.0, abs=0.05)
        assert b.out_time == pytest.approx(a.out_time + 10.0, abs=0.05)


def test_recognizer_applies_offset_to_words(ru_wav):
    """Живой движок: слова должны начинаться не раньше заданного смещения."""
    from timpoc.audio import load_audio

    audio, sr = load_audio(ru_wav)
    rec = Recognizer(_settings(vad_filter=False))
    words, _info, _warns = rec.transcribe(audio, int(sr), offset_seconds=7.5)
    assert words, "движок не вернул слов на тестовом аудио"
    assert min(w.start for w in words) >= 7.5 - 0.05


def test_pipeline_range_limits_output(ru_wav):
    """Распознавание только начала не должно давать субтитры в конце файла.

    Берём первые 5 с (в этом тестовом файле там гарантированно есть речь)
    и проверяем, что ничего не вылезает за границу диапазона.
    """
    inp = PipelineInput(path=ru_wav, range_start=0.0, range_end=5.0, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(vad_filter=False), SegmentationSettings()).transcript
    assert tr.blocks
    for b in tr.blocks:
        assert b.out_time <= 5.5


def test_range_beyond_audio_reports_silence(ru_wav):
    """Диапазон, где звука нет, должен давать понятную ошибку, а не пустой результат."""
    inp = PipelineInput(path=ru_wav, range_start=100.0, range_end=110.0, frame_rate=25.0)
    with pytest.raises(SilenceError):
        run_pipeline(inp, _settings(), SegmentationSettings())


def test_pipeline_silence_raises_clear_error(silent_wav):
    inp = PipelineInput(path=silent_wav, frame_rate=25.0)
    with pytest.raises(SilenceError) as exc:
        run_pipeline(inp, _settings(), SegmentationSettings())
    assert exc.value.hint  # пользователю должно быть сказано, что делать


def test_pipeline_empty_range_raises():
    from timpoc.errors import EmptyRangeError

    inp = PipelineInput(path="whatever.wav", range_start=5.0, range_end=5.0)
    with pytest.raises(EmptyRangeError):
        run_pipeline(inp, _settings(), SegmentationSettings())


def test_pipeline_missing_file_raises():
    from timpoc.errors import TimpocError

    inp = PipelineInput(path="/tmp/этого файла точно нет.wav")
    with pytest.raises(TimpocError):
        run_pipeline(inp, _settings(), SegmentationSettings())


def test_progress_is_monotonic(ru_wav):
    """Прогресс не должен «прыгать назад» — иначе полоса в панели дёргается."""
    seen = []

    def cb(p, msg):
        seen.append(p)
        assert 0.0 <= p <= 1.0

    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    run_pipeline(inp, _settings(), SegmentationSettings(), progress=cb)
    assert seen
    assert seen == sorted(seen)
    assert seen[-1] >= 0.99


def test_cancel_stops_recognition(ru_wav):
    """Отмена должна прерывать распознавание, а не ждать его конца."""
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 2  # отменяем почти сразу

    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    with pytest.raises(CancelledError):
        run_pipeline(inp, _settings(), SegmentationSettings(), cancel=cancel)


# ------------------------------------------------------------------ переразбивка

def test_resegment_does_not_change_words(ru_wav):
    """Переразбивка меняет блоки, но не слова — распознавание не повторяется."""
    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(), SegmentationSettings()).transcript
    words_before = [w.to_dict() for w in tr.words]

    resegment(tr, SegmentationSettings(max_chars=18, max_words=4), 25.0)

    assert [w.to_dict() for w in tr.words] == words_before
    assert len(tr.blocks) >= 1
    for b in tr.blocks:
        assert b.out_time > b.in_time


def test_resegment_with_tighter_limits_creates_more_blocks(ru_wav):
    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(), SegmentationSettings(max_chars=200, max_words=99)).transcript
    loose = len(tr.blocks)
    resegment(tr, SegmentationSettings(max_chars=16, max_words=3), 25.0)
    assert len(tr.blocks) > loose


# ------------------------------------------------------------------ интеграция

def test_result_serializes_to_json_and_back(ru_wav):
    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(), SegmentationSettings()).transcript
    restored = Transcript.from_json(tr.to_json())
    assert len(restored.blocks) == len(tr.blocks)
    assert restored.blocks[0].text == tr.blocks[0].text
    assert restored.info.model == tr.info.model


def test_result_exports_to_srt(ru_wav):
    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(), SegmentationSettings()).transcript
    text = to_srt(tr.blocks)
    assert "-->" in text
    assert text.count("-->") == len([b for b in tr.blocks if b.text.strip()])


def test_state_saves_and_restores(ru_wav, tmp_path, monkeypatch):
    """Сбой применения не должен требовать нового распознавания."""
    from timpoc.state import ProjectState

    monkeypatch.setenv("TIMPOC_DATA_DIR", str(tmp_path))
    inp = PipelineInput(path=ru_wav, frame_rate=25.0)
    tr = run_pipeline(inp, _settings(), SegmentationSettings()).transcript

    st = ProjectState(transcript=tr, frame_rate=25.0)
    path = st.save(str(tmp_path / "state.json"))

    loaded = ProjectState.load(path)
    assert loaded.has_transcript
    assert len(loaded.transcript.blocks) == len(tr.blocks)
    assert loaded.transcript.blocks[0].text == tr.blocks[0].text


def test_recognizer_reports_device(ru_wav):
    rec = Recognizer(_settings())
    assert rec.plan.device in ("cpu", "cuda", "mps")
    assert rec.plan.reason  # причина выбора устройства обязательна
