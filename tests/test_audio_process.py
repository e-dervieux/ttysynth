"""Audio engine and audio process: lifecycle and failure reporting
(synth/audio_engine.py). The audio process is a real spawned process; the
fake sounddevice module (tests/fakes) simulates the audio device and its
failures (FAKE_SD environment variable)."""

import logging
import threading
import time
from contextlib import contextmanager

import pytest

from synth.audio_engine import AudioEngine, AudioProcessCrash
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
