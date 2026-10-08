"""Audio engine and audio process: lifecycle and failure reporting
(synth/audio_engine.py). The audio process is a real spawned process; the
fake sounddevice module (tests/fakes) simulates the audio device and its
failures (FAKE_SD environment variable)."""

import copy
import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from synth.audio_engine import (HEADROOM, RECORDING_ERRORS, AudioEngine, AudioProcessCrash,
                                _AudioProcess, _Recorder)
from synth.dsp import Flute, Note
from synth.errors import SynthError


def wait_for(condition, timeout=5.0):
    """Poll until condition() is true (False on timeout)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def engine(audio_config):
    engine = AudioEngine(audio_config)
    yield engine
    engine.stop()


@contextmanager
def thread_crashes():
    """Collects the exceptions of crashed threads: the status reader raises
    the audio process failures, which the synthesizer catches with
    threading.excepthook. Installed within the test itself: pytest sets its
    own hook while a test runs."""
    crashes = []
    previous = threading.excepthook
    threading.excepthook = lambda args: crashes.append(args.exc_value)
    try:
        yield crashes
    finally:
        threading.excepthook = previous


def test_start_play_stop(engine, caplog):
    caplog.set_level(logging.DEBUG)  # Passed on to the audio process
    engine.start()
    engine.play_note('a', 440.0, 'flute')
    assert wait_for(lambda: engine.get_status().get('voices') == 1)
    engine.stop_note('a')
    engine.stop()
    # Log records of the audio process reach the main process
    audio_records = [r.getMessage() for r in caplog.records if r.name == 'synth.audio_engine']
    assert 'Audio process: note a started (440.0 Hz, flute)' in audio_records
    assert 'Audio process: note a released' in audio_records


def test_volume_is_clamped(engine):
    engine.set_volume(2.0)
    assert engine.get_volume() == 1.0
    engine.change_volume(-5.0)
    assert engine.get_volume() == 0.0


def test_device_by_name(audio_config, caplog):
    """The audio process gets the index of the device (see the fake)"""
    caplog.set_level(logging.INFO)
    engine = AudioEngine(replace(audio_config, device='speakers'))
    try:
        engine.start()
    finally:
        engine.stop()
    assert 'Output device: Fake Speakers' in caplog.text


@pytest.mark.parametrize('device, message', [
    ('headphones', "No output device matching 'headphones'"),
    (7, 'Error querying device 7'),
])
def test_unknown_device(audio_config, device, message):
    engine = AudioEngine(replace(audio_config, device=device))
    with pytest.raises(SynthError, match=f'Output device: {message} \\(see --list-devices\\)'):
        engine.start()
    assert engine.process is None  # Checked before starting the audio process


@pytest.mark.parametrize('volume', [1.0, 0.25])
def test_output_stage(audio_config, volume):
    """Mix x volume x HEADROOM, then soft clipping (in-process audio
    process): the volume comes before the saturation"""
    process = _AudioProcess(queue.Queue(), queue.Queue(), replace(audio_config, default_volume=volume))
    notes = [Note(440.0 * ratio, Flute(48000)) for ratio in (1, 1.25, 1.5)]
    twins = copy.deepcopy(notes)
    process.notes = notes
    outdata = np.zeros((64, 2), np.float32)
    for _ in range(100):
        process._audio_callback(outdata, 64, None, None)
        mix = sum(twin.get_next_buffer(64) for twin in twins)
        expected = np.tanh(volume * HEADROOM * mix)
        assert outdata[:, 0] == pytest.approx(expected) and outdata[:, 1] == pytest.approx(expected)


def test_stream_cannot_be_opened(engine, monkeypatch):
    monkeypatch.setenv('FAKE_SD', 'open-fail')
    with pytest.raises(SynthError, match='Could not create audio stream'):
        engine.start()


def test_audio_process_dies_before_ready(engine, monkeypatch):
    monkeypatch.setenv('FAKE_SD', 'early-death')
    started = time.monotonic()
    with pytest.raises(SynthError, match='died unexpectedly \\(exit code 3\\)'):
        engine.start()
    assert time.monotonic() - started < 5.0  # Not the 10 s start timeout


@pytest.mark.parametrize('mode, error, message', [
    ('device-lost', SynthError, 'Audio stream stopped unexpectedly'),
    ('killed', SynthError, 'killed by signal 9'),
    ('callback-bug', AudioProcessCrash, 'TypeError'),
    ('loop-bug', AudioProcessCrash, 'KeyError'),
])
def test_failures_after_start_are_reported(engine, monkeypatch, mode, error, message):
    monkeypatch.setenv('FAKE_SD', mode)
    with thread_crashes() as crashes:
        engine.start()
        assert wait_for(lambda: crashes), f"{mode} not reported"
    assert isinstance(crashes[0], error) and message in str(crashes[0])


def test_unknown_instrument_is_a_crash(engine):
    with thread_crashes() as crashes:
        engine.start()
        engine.play_note('a', 440.0, 'violin')
        assert wait_for(lambda: crashes)
    assert isinstance(crashes[0], AudioProcessCrash) and "KeyError: 'violin'" in str(crashes[0])


# ----------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------

def recording_duration(engine) -> float:
    return engine.get_status().get('recording') or 0.0


def test_recording(engine, audio_config, caplog):
    caplog.set_level(logging.INFO)
    engine.start()
    engine.start_recording()
    engine.play_note('a', 440.0, 'flute')
    assert wait_for(lambda: recording_duration(engine) >= 0.3)
    engine.stop_recording()
    assert wait_for(lambda: 'Recording saved' in caplog.text)
    assert wait_for(lambda: engine.get_status()['recording'] is None)

    [path] = audio_config.record_dir.glob('*.flac')
    info = sf.info(path)
    assert (info.format, info.subtype, info.samplerate, info.channels) == ('FLAC', 'PCM_24', 48000, 2)
    data, _ = sf.read(path)
    assert len(data) >= 0.3 * 48000 and np.max(np.abs(data)) > 0.01  # The note is in it


def test_quitting_while_recording_finalizes_the_file(engine, audio_config):
    engine.start()
    engine.start_recording()
    assert wait_for(lambda: recording_duration(engine) >= 0.1)
    engine.stop()
    [path] = audio_config.record_dir.glob('*.flac')
    assert sf.info(path).frames >= 0.1 * 48000


def test_start_and_stop_twice(engine, caplog):
    caplog.set_level(logging.INFO)
    engine.start()
    engine.stop_recording()
    engine.start_recording()
    engine.start_recording()
    engine.stop()
    assert 'Not recording' in caplog.text and 'Already recording' in caplog.text
    assert caplog.text.count('Recording saved') == 1


def test_recording_directory_cannot_be_created(audio_config, tmp_path, caplog):
    """Reported; the synthesizer keeps playing"""
    (tmp_path / 'file').write_text('')
    engine = AudioEngine(replace(audio_config, record_dir=tmp_path / 'file' / 'recordings'))
    try:
        engine.start()
        engine.start_recording()
        assert wait_for(lambda: 'Cannot record' in caplog.text)
        engine.play_note('a', 440.0, 'flute')
        assert wait_for(lambda: engine.get_status().get('voices') == 1)
    finally:
        engine.stop()


def test_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / 'take.flac'
    path.write_text('precious')
    with pytest.raises(RECORDING_ERRORS):
        _Recorder(path, 48000, 2)
    assert path.read_text() == 'precious'


def disk_full(data):
    raise OSError(28, 'No space left on device')


def test_write_failure_stops_the_recording_only(audio_config, caplog):
    """In-process audio process: the failure is logged, the callback goes on"""
    process = _AudioProcess(queue.Queue(), queue.Queue(), audio_config)
    process._start_recording(audio_config.record_dir / 'take.flac')
    process.recorder.file.write = disk_full
    process._audio_callback(np.zeros((64, 2), np.float32), 64, None, None)
    process._stop_recording()
    assert process.recorder is None
    assert 'take.flac may be incomplete: [Errno 28] No space left on device' in caplog.text
    process._audio_callback(np.zeros((64, 2), np.float32), 64, None, None)
    assert process.callback_error is None
