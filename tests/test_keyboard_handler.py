"""Musical keyboard: notes, controls, layouts, octave shift
(synth/keyboard_handler.py)"""

from dataclasses import replace

import pytest

from synth.errors import ConfigError
from synth.keyboard_handler import KeyboardHandler
from synth.music import midi_to_note


@pytest.fixture
def make_handler(keyboard_config, controls):
    """Factory of handlers recording notes in .notes and controls in .controls_pressed"""
    def make(**config_changes):
        notes, controls_pressed = [], []
        handler = KeyboardHandler(
            replace(keyboard_config, **config_changes), controls,
            on_note=lambda key, midi, pressed: notes.append((key, midi_to_note(midi), pressed)),
            on_control=controls_pressed.append)
        handler.notes, handler.controls_pressed = notes, controls_pressed
        return handler
    return make


@pytest.fixture
def handler(make_handler):
    return make_handler()


def tap(handler, key):
    handler._on_key(key, True)
    handler._on_key(key, False)


def test_note_on_and_off(handler):
    tap(handler, 'a')
    assert handler.notes == [('a', 'C4', True), ('a', 'C4', False)]


def test_control_key(handler):
    tap(handler, 'tab')
    assert handler.controls_pressed == ['instrument_next'] and handler.notes == []


def test_unmapped_key(handler):
    tap(handler, 'x')
    assert handler.notes == [] and handler.controls_pressed == []


def test_pressed_keys(handler):
    handler._on_key('a', True)
    handler._on_key('s', True)
    assert handler.get_pressed_keys() == {'a': 60, 's': 62}
    handler._on_key('a', False)
    assert handler.get_pressed_keys() == {'s': 62}


def test_same_key_name_twice_plays_one_note(handler):
    """Two physical keys may produce the same name: one note"""
    handler._on_key('a', True)
    handler._on_key('a', True)
    assert handler.notes == [('a', 'C4', True)]


def test_next_layout(handler):
    assert handler.layout == 'qwerty'
    handler.next_layout()  # The custom layout file does not exist: left out
    assert handler.layout == 'azerty'
    tap(handler, 'q')
    assert handler.notes == [('q', 'C4', True), ('q', 'C4', False)]


def test_held_note_released_after_layout_change(handler):
    handler._on_key('a', True)      # C4 in qwerty, unmapped in azerty
    handler.next_layout()
    handler._on_key('a', False)
    assert handler.notes == [('a', 'C4', True), ('a', 'C4', False)]


def test_octave_shift(handler):
    handler.shift_octave(+1)
    tap(handler, 'a')
    assert handler.notes[0] == ('a', 'C5', True)
    assert handler.note_for_key('a') == 72


def test_held_note_keeps_its_pitch_across_octave_shift(handler):
    handler._on_key('a', True)
    handler.shift_octave(+1)
    handler._on_key('a', False)
    assert handler.notes == [('a', 'C4', True), ('a', 'C4', False)]


def test_octave_shift_limits(handler):
    """Built-in layouts span C4-E5: from C-1 (-5) to E9 (+4), within MIDI"""
    for _ in range(10):
        handler.shift_octave(+1)
    assert handler.octave_shift == 4
    for _ in range(20):
        handler.shift_octave(-1)
    assert handler.octave_shift == -5


def test_initial_octave_shift_out_of_range(make_handler):
    with pytest.raises(ConfigError, match='OCTAVE_SHIFT = 5'):
        make_handler(octave_shift=5)


def test_layout_switch_resets_shift_that_does_not_fit(make_handler, tmp_path):
    """A wide custom layout may not fit the current octave shift"""
    path = tmp_path / 'wide.ini'
    path.write_text('[NOTES]\nC1 = q\nB8 = 1\n', encoding='utf-8')
    handler = make_handler(custom_layout=path, octave_shift=2)
    handler.next_layout()  # qwerty -> custom: B8 + 2 octaves is out of range
    assert handler.layout == 'custom' and handler.octave_shift == 0


def test_capture_status_before_start(handler):
    assert handler.capture_status == 'OFF'


def test_held_volume_keys_repeat(handler, controls):
    for name in ('volume_up', 'volume_down'):
        key = controls[name]
        handler._on_key(key, True)
        handler._on_repeat(key)
        handler._on_repeat(key)
        handler._on_key(key, False)
    assert handler.controls_pressed == ['volume_up'] * 3 + ['volume_down'] * 3


def test_held_notes_and_other_controls_do_not_repeat(handler, controls):
    for key in ('a', controls['instrument_next'], controls['octave_up']):
        handler._on_key(key, True)
        handler._on_repeat(key)
        handler._on_key(key, False)
    assert handler.notes == [('a', 'C4', True), ('a', 'C4', False)]
    assert handler.controls_pressed == ['instrument_next', 'octave_up']
