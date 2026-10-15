"""Configuration loading and validation (synth/config.py)"""

import re

import pytest

from synth.config import CONTROLS, DEFAULT_CONFIG_PATH, load_config
from synth.errors import ConfigError

SHIPPED = DEFAULT_CONFIG_PATH.read_text(encoding='utf-8')


def variant(tmp_path, pattern: str, replacement: str) -> str:
    """Path of a copy of the shipped config.ini, with one line changed"""
    text, count = re.subn(pattern, replacement, SHIPPED, count=1, flags=re.M)
    assert count == 1, f"pattern not found: {pattern}"
    path = tmp_path / 'config.ini'
    path.write_text(text, encoding='utf-8')
    return path


def test_shipped_config_loads():
    config = load_config()
    assert config.audio.device is None  # System default
    assert config.audio.queued_buffers is None  # Device's default latency
    assert config.audio.sample_rate > 0 and config.audio.buffer_size > 0
    assert 0 <= config.audio.default_volume <= 1
    assert config.controls.keys() == CONTROLS.keys()
    # Resolved relative to config.ini, not to the current directory
    assert config.keyboard.custom_layout == DEFAULT_CONFIG_PATH.parent / 'custom_layout.ini'
    assert config.audio.record_dir == DEFAULT_CONFIG_PATH.parent / 'recordings'


@pytest.mark.parametrize('pattern, replacement, message', [
    (r'^SAMPLE_RATE', 'SAMPLERATE', 'unknown option \\[AUDIO\\] SAMPLERATE'),
    (r'^CHANNELS = 2', 'CHANNELS = 2\nGAIN = 3', 'unknown option \\[AUDIO\\] GAIN'),
    (r'^OCTAVE_UP = .*\n', '', 'missing option \\[CONTROLS\\] OCTAVE_UP'),
    (r'^\[CONTROLS\][\s\S]*', '', 'missing section \\[CONTROLS\\]'),
    (r'^BUFFER_SIZE = .*', 'BUFFER_SIZE = abc', "BUFFER_SIZE = 'abc': expected a positive integer"),
    (r'^BUFFER_SIZE = .*', 'BUFFER_SIZE = 0', 'expected a positive integer'),
    (r'^QUEUED_BUFFERS =.*', 'QUEUED_BUFFERS = 0', 'expected empty or a positive integer'),
    (r'^QUEUED_BUFFERS =.*', 'QUEUED_BUFFERS = two', 'expected empty or a positive integer'),
    (r'^CHANNELS = .*', 'CHANNELS = 2.5', 'expected a positive integer'),
    (r'^DEFAULT_VOLUME = .*', 'DEFAULT_VOLUME = 3', 'expected a number between 0 and 1'),
    (r'^TUNING_FREQ = .*', 'TUNING_FREQ = -440', 'expected a positive number'),
    (r'^TUNING_FREQ = .*', 'TUNING_FREQ = 440  ; Hz', 'expected a positive number'),
    (r'^TEMPERAMENT = .*', 'TEMPERAMENT = just', 'expected one of equal'),
    (r'^LAYOUT = .*', 'LAYOUT = dvorak', 'expected one of azerty, qwerty, custom'),
    (r'^CUSTOM_LAYOUT = .*', 'CUSTOM_LAYOUT =', 'expected a file name'),
    (r'^OCTAVE_SHIFT = .*', 'OCTAVE_SHIFT = up', 'expected an integer'),
    (r'^RECORD_DIR = .*', 'RECORD_DIR =', 'expected a directory'),
    (r'^QUIT = .*', 'QUIT = ctrl', '\\[CONTROLS\\] QUIT'),
    (r'^CHANNELS = 2', 'CHANNELS = 2\nCHANNELS = 1', 'already exists'),
])
def test_invalid_configs(tmp_path, pattern, replacement, message):
    with pytest.raises(ConfigError, match=message):
        load_config(variant(tmp_path, pattern, replacement))


def test_unknown_section(tmp_path):
    path = tmp_path / 'config.ini'
    path.write_text(SHIPPED + '\n[AUDO]\nX = 1\n', encoding='utf-8')
    with pytest.raises(ConfigError, match='unknown section \\[AUDO\\]'):
        load_config(path)


def test_default_section_not_supported(tmp_path):
    path = tmp_path / 'config.ini'
    path.write_text('[DEFAULT]\nX = 1\n' + SHIPPED, encoding='utf-8')
    with pytest.raises(ConfigError, match='\\[DEFAULT\\] section not supported'):
        load_config(path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match='Cannot read configuration'):
        load_config(tmp_path / 'missing.ini')


@pytest.mark.parametrize('value, device', [('3', 3), ('MacBook Pro', 'MacBook Pro'), ('', None)])
def test_device(tmp_path, value, device):
    assert load_config(variant(tmp_path, r'^DEVICE =.*', f'DEVICE = {value}')).audio.device == device


def test_option_names_are_case_insensitive(tmp_path):
    assert load_config(variant(tmp_path, r'^SAMPLE_RATE', 'sample_rate')).audio.sample_rate > 0


def test_percent_is_a_valid_key(tmp_path):
    """No interpolation: '%' is not special"""
    assert load_config(variant(tmp_path, r'^QUIT = .*', 'QUIT = %')).controls['quit'] == '%'
