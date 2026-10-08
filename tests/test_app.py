"""Synthesizer: control actions, Ctrl+C, crashed threads, exit codes
(synth/app.py)"""

import logging
import os
import signal
import threading
import time
from dataclasses import replace

import pytest

import synth.app as app
from synth.config import CONTROLS, DEFAULT_CONFIG_PATH
from synth.errors import ConfigError, SynthError
from synth.keyboard_handler import KeyboardHandler


@pytest.fixture
def synth(config):
    return app.Synthesizer(config)


class FakeTUI:
    """Stands in for the TUI: loops like it while the synthesizer runs"""
    started = None  # threading.Event, set when the loop starts

    def __init__(self, synth):
        self.synth = synth

    def run(self):
        FakeTUI.started.set()
        while self.synth.running:
            time.sleep(0.02)


@pytest.fixture
def headless(monkeypatch):
    """No TUI, no keyboard input: run() starts the (fake) audio process only"""
    FakeTUI.started = threading.Event()
    monkeypatch.setattr(app, 'SynthTUI', FakeTUI)
    monkeypatch.setattr(KeyboardHandler, 'start', lambda self: None)


def test_every_control_has_an_action(synth):
    assert synth.actions.keys() == CONTROLS.keys()


def test_control_actions(synth):
    volume = synth.audio_engine.get_volume()
    synth.on_control('volume_up')
    assert synth.audio_engine.get_volume() == pytest.approx(volume + app.VOLUME_STEP)
    synth.on_control('volume_down')
    assert synth.audio_engine.get_volume() == pytest.approx(volume)

    instrument = synth.current_instrument
    synth.on_control('instrument_next')
    assert synth.current_instrument != instrument

    synth.on_control('temperament_next')
    assert synth.music_engine.temperament == 'pythagorean'

    synth.on_control('layout_next')
    assert synth.keyboard_handler.layout == 'azerty'

    synth.on_control('octave_up')
    assert synth.keyboard_handler.octave_shift == 1

    synth.running = True
    synth.on_control('quit')
    assert not synth.running


def test_repeated_ctrl_c_never_interrupts_the_cleanup(synth, headless, monkeypatch):
    """Ctrl+C is a quit request: pressed again and again, including during
    the cleanup, the shutdown still completes"""
    stopping = threading.Event()
    cleanup_done = []

    def send_ctrl_c():
        FakeTUI.started.wait()
        for _ in range(5):              # While playing
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.01)
        stopping.wait()
        for _ in range(10):             # During the cleanup
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.01)

    sender = threading.Thread(target=send_ctrl_c)

    def slow_keyboard_stop(handler):
        stopping.set()
        sender.join()                   # Every Ctrl+C sent during the cleanup...
        time.sleep(0.1)                 # ...and handled
        cleanup_done.append(True)

    monkeypatch.setattr(KeyboardHandler, 'stop', slow_keyboard_stop)
    previous_handler = signal.getsignal(signal.SIGINT)
    sender.start()
    synth.run()
    assert cleanup_done == [True]
    assert not synth.audio_engine.running
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_crashed_thread_stops_the_synthesizer_and_is_reraised(synth, headless):
    def crash():
        FakeTUI.started.wait()
        raise ValueError('bug in a background thread')

    previous_hook = threading.excepthook
    threading.Thread(target=crash).start()
    with pytest.raises(ValueError, match='bug in a background thread'):
        synth.run()
    assert not synth.audio_engine.running
    assert threading.excepthook is previous_hook


@pytest.fixture
def quiet_main(monkeypatch):
    """main() without configuring the logging of the test session: the
    level it asks for is in .levels"""
    levels = []
    monkeypatch.setattr(app, 'setup_logging', levels.append)
    return levels


@pytest.fixture
def captured_config(monkeypatch):
    """Synthesizer.run() records the configuration instead of running"""
    configs = []
    monkeypatch.setattr(app.Synthesizer, 'run', lambda self: configs.append(self.audio_engine.config))
    return configs


def test_main_returns_0_on_normal_exit(quiet_main, monkeypatch, config):
    monkeypatch.setattr(app, 'load_config', lambda path: config)
    monkeypatch.setattr(app.Synthesizer, 'run', lambda self: None)
    assert app.main([]) == 0


@pytest.mark.parametrize('error', [ConfigError('bad option'), SynthError('no audio device')])
def test_main_returns_1_on_expected_failure(quiet_main, monkeypatch, config, caplog, error):
    monkeypatch.setattr(app, 'load_config', lambda path: config)

    def fail(self):
        raise error
    monkeypatch.setattr(app.Synthesizer, 'run', fail)
    assert app.main([]) == 1
    assert str(error) in caplog.text


def test_list_devices(quiet_main, capsys):
    assert app.main(['--list-devices']) == 0
    assert capsys.readouterr().out.startswith('fake device list\n')


def test_default_options(quiet_main, captured_config):
    assert app.main([]) == 0
    assert quiet_main == [logging.INFO]
    assert captured_config[0].device is None  # Shipped config.ini: system default


def test_debug_option(quiet_main, captured_config):
    app.main(['--debug'])
    assert quiet_main == [logging.DEBUG]


def test_config_option(quiet_main, captured_config, tmp_path):
    path = tmp_path / 'other.ini'
    path.write_text(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8').replace(
        'SAMPLE_RATE = 48000', 'SAMPLE_RATE = 44100'), encoding='utf-8')
    app.main(['--config', str(path)])
    assert captured_config[0].sample_rate == 44100


@pytest.mark.parametrize('config_device, option, expected', [
    ('', ['--device', '3'], 3),
    ('Speakers', ['--device', 'Headphones'], 'Headphones'),
    ('Speakers', [], 'Speakers'),
])
def test_device_option_overrides_the_config(quiet_main, captured_config, tmp_path,
                                            config_device, option, expected):
    path = tmp_path / 'other.ini'
    path.write_text(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8').replace(
        'DEVICE =', f'DEVICE = {config_device}'), encoding='utf-8')
    app.main(['--config', str(path), *option])
    assert captured_config[0].device == expected


def test_unknown_option(quiet_main):
    with pytest.raises(SystemExit) as exit_info:
        app.main(['--frobnicate'])
    assert exit_info.value.code == 2


def test_main_lets_bugs_through(quiet_main, monkeypatch, config):
    """Unexpected exceptions keep their traceback"""
    monkeypatch.setattr(app, 'load_config', lambda path: config)

    def bug(self):
        raise TypeError('a bug')
    monkeypatch.setattr(app.Synthesizer, 'run', bug)
    with pytest.raises(TypeError):
        app.main([])


def test_invalid_keyboard_config_is_a_config_error(config):
    with pytest.raises(ConfigError):
        app.Synthesizer(replace(config, keyboard=replace(config.keyboard, octave_shift=9)))
