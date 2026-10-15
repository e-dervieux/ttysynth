"""Terminal user interface: layout at various window sizes (synth/tui.py).
Rendered off-screen, on a rich console of a given size."""

import io
import logging
import threading
import time
import types

import pytest
from rich.console import Console
from rich.text import Text

import synth.tui as tui
from synth.app import Synthesizer
from synth.config import CONTROLS


def log_record(message):
    return logging.makeLogRecord(dict(msg=message, levelno=logging.INFO, levelname='INFO'))


@pytest.fixture
def synth(config, monkeypatch):
    """Synthesizer playing C4 E4 G4, with 50 log records (one of them long)"""
    records = [log_record(f"log line {i}" + " long" * 40 * (i == 45)) for i in range(50)]
    monkeypatch.setattr(tui, 'log_history', lambda: records)
    synth = Synthesizer(config)
    synth.keyboard_handler.backend = types.SimpleNamespace(status='OK (kitty protocol)')
    for key in 'adg':
        synth.keyboard_handler._on_key(key, True)
    return synth


def render(synth, width, height) -> list:
    """Lines of the TUI screen at this size"""
    console = Console(width=width, height=height, file=io.StringIO(), record=True)
    console.print(tui.SynthTUI(synth)._render(console))
    return console.export_text().splitlines()


@pytest.mark.parametrize('width, height', [(70, 22), (80, 24), (100, 30), (120, 40), (200, 60)])
def test_everything_is_visible(synth, width, height):
    lines = render(synth, width, height)
    screen = "\n".join(lines)
    assert len(lines) == height
    assert "keyboard: OK (kitty protocol)" in screen
    assert "notes: C4 E4 G4" in screen
    for key, label in zip(synth.keyboard_handler.control_keys.values(), CONTROLS.values()):
        assert f"{key} {label}" in screen
    for key in synth.keyboard_handler.key_to_midi:
        assert f"{key} -> " in screen
    # Newest log lines last, at the bottom of the log panel
    assert "log line 49" in lines[-2] and "log line 47" in screen


def test_status_fields_stay_in_place_from_9_to_10_voices(synth):
    columns = []
    for voices in (9, 10):
        synth.audio_engine.latest_status = {'voices': voices}
        # Wide enough for the keyboard field to follow the voices on the same line
        screen = "\n".join(render(synth, 250, 60))
        columns.append(screen.index("keyboard:"))
    assert columns[0] == columns[1]


def test_log_panel_fills_the_remaining_height(synth):
    small, large = render(synth, 80, 24), render(synth, 80, 60)
    count = lambda lines: sum("[  INFO   ]" in line for line in lines)
    assert count(large) == count(small) + 36
    assert "log line 0 " not in "\n".join(large)  # 50 records do not fit


@pytest.mark.parametrize('width, height, needed', [
    (69, 50, "at least 70 columns"),
    (70, 21, "at least 22 rows"),
    (120, 16, "at least 17 rows"),
])
def test_window_too_small(synth, width, height, needed):
    lines = render(synth, width, height)
    screen = "\n".join(lines)
    assert len(lines) == height
    assert f"Window too small ({width}×{height})" in screen and needed in screen
    assert "Keys" not in screen


def test_recording_is_shown(synth):
    assert "● REC" not in "\n".join(render(synth, 80, 24))
    synth.audio_engine.latest_status = {'recording': 65.4}
    assert "● REC 1:05" in "\n".join(render(synth, 80, 24))


def test_held_keys_are_highlighted(synth):
    assert [cell.plain for cell in tui.SynthTUI(synth)._note_cells() if cell.style == "reverse"] == \
           ['a -> C4', 'd -> E4', 'g -> G4']


def test_columns():
    cells = [Text("a" * length) for length in (3, 5, 4, 5, 1)]  # 5 cells wide, 2 apart
    assert tui._columns(cells, 19).plain.splitlines() == [
        "aaa    aaaaa  aaaa ", "aaaaa  a    "]
    assert len(tui._columns(cells, 4).plain.splitlines()) == 5  # Narrower than a cell: one per row


class FakeLive:
    """Stands in for rich's Live: records the time of each frame"""
    frames = None

    def __init__(self, **kwargs):
        self.console = Console(width=100, height=30, file=io.StringIO())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def update(self, renderable, refresh):
        FakeLive.frames.append(time.monotonic())


def test_refresh_is_immediate_on_change_and_slow_when_idle(synth, monkeypatch):
    FakeLive.frames = []
    monkeypatch.setattr(tui, 'Live', FakeLive)
    synth.keyboard_handler.backend.on_alt_screen_enter = lambda: None
    synth.keyboard_handler.backend.on_alt_screen_exit = lambda: None
    synth.running = True
    thread = threading.Thread(target=tui.SynthTUI(synth).run)
    thread.start()
    try:
        time.sleep(1.0)
        idle_frames = len(FakeLive.frames)
        pressed = time.monotonic()
        synth.on_note('a', 60, True)
        time.sleep(0.1)
        drawn = [frame for frame in FakeLive.frames if frame > pressed]
    finally:
        synth.running = False
        synth.display_changed.set()
        thread.join(timeout=2)
    assert 3 <= idle_frames <= 6                    # 4 Hz (plus the first frame)
    assert drawn and drawn[0] - pressed < 0.05      # Not at the next idle refresh
    assert not thread.is_alive()


def test_flow_never_splits_a_field():
    fields = [Text(f"field{i}: value") for i in range(5)]  # 13 cells each
    assert tui._flow(fields, 30).plain.splitlines() == [
        "field0: value   field1: value", "field2: value   field3: value", "field4: value"]
    assert tui._flow(fields, 28).plain.splitlines() == [f"field{i}: value" for i in range(5)]
