"""Terminal user interface: layout at various window sizes (synth/tui.py).
Rendered off-screen, on a rich console of a given size."""

import io
import logging
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


def test_held_keys_are_highlighted(synth):
    note_cells = tui.SynthTUI(synth)._render_keymap().renderable.renderables[0].renderables
    assert [cell.plain for cell in note_cells if cell.style == "reverse"] == \
           ['a -> C4', 'd -> E4', 'g -> G4']


def test_flow_never_splits_a_field():
    fields = [Text(f"field{i}: value") for i in range(5)]  # 13 cells each
    assert tui._flow(fields, 30).plain.splitlines() == [
        "field0: value   field1: value", "field2: value   field3: value", "field4: value"]
    assert tui._flow(fields, 28).plain.splitlines() == [f"field{i}: value" for i in range(5)]
