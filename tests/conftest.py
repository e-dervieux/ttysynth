"""
Shared test setup.

The automated tests never touch the audio device, the keyboard or the
terminal: the fake sounddevice module (tests/fakes, see pytest.ini) shadows
the real one, and terminal escape sequences are not written.
"""

import pytest

from synth import input_backends
from synth.config import AudioConfig, Config, KeyboardConfig

# Control keys of the shipped config.ini, as load_config() normalizes them
CONTROLS = {
    'volume_up': '+', 'volume_down': '-', 'instrument_next': 'tab', 'layout_next': 'v',
    'temperament_next': 'c', 'octave_down': '(', 'octave_up': ')',
    'record_start': 'b', 'record_stop': 'n', 'quit': 'esc',
}


@pytest.fixture
def controls():
    return dict(CONTROLS)


@pytest.fixture
def audio_config(tmp_path):
    return AudioConfig(device=None, sample_rate=48000, buffer_size=64, channels=2,
                       default_volume=0.5, tuning_freq=440.0, temperament='equal',
                       record_dir=tmp_path / 'recordings')


@pytest.fixture
def keyboard_config(tmp_path):
    """qwerty layout; the custom layout file does not exist (left out of the cycle)"""
    return KeyboardConfig(layout='qwerty', custom_layout=tmp_path / 'custom_layout.ini',
                          octave_shift=0)


@pytest.fixture
def config(audio_config, keyboard_config, controls):
    return Config(audio=audio_config, keyboard=keyboard_config, controls=controls)


@pytest.fixture(autouse=True)
def no_terminal_writes(monkeypatch):
    """Escape sequences (kitty flags, focus reporting...) are not written"""
    monkeypatch.setattr(input_backends, '_write_term', lambda seq: None)
