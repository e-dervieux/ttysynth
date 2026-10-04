"""Keyboard layouts: built-in, custom file, validation (synth/keymap.py)"""

import logging
from pathlib import Path

import pytest

from synth.config import DEFAULT_CONFIG_PATH
from synth.errors import ConfigError
from synth.keymap import (BUILTIN_LAYOUTS, LayoutNotConfiguredError, build_all_keymaps,
                          build_keymap, normalize_key)
from synth.music import note_to_midi

SHIPPED_TEMPLATE = DEFAULT_CONFIG_PATH.parent / 'custom_layout.ini'


def custom(tmp_path: Path, notes: str) -> Path:
    """Write a custom layout file with the given [NOTES] lines"""
    path = tmp_path / 'custom_layout.ini'
    path.write_text('[NOTES]\n' + notes, encoding='utf-8')
    return path


@pytest.mark.parametrize('key, name', [('q', 'q'), ('Q', 'q'), (' a ', 'a'), ('ESC', 'esc'),
                                       ('é', 'é'), (';', ';'), ('space', 'space')])
def test_normalize_key(key, name):
    assert normalize_key(key, 'test') == name


@pytest.mark.parametrize('key', ['', 'ctrl', 'cmd', 'qa', 'f1'])
def test_normalize_key_rejects_unmappable_keys(key):
    with pytest.raises(ConfigError, match='test'):
        normalize_key(key, 'test')


@pytest.mark.parametrize('layout', BUILTIN_LAYOUTS)
def test_builtin_layouts_are_chromatic_from_c4_to_e5(layout, controls, tmp_path):
    keymap = build_keymap(layout, tmp_path / 'unused.ini', controls)
    assert list(keymap.values()) == list(range(note_to_midi('C4'), note_to_midi('E5') + 1))


def test_builtin_layouts_share_physical_positions(controls, tmp_path):
    azerty = build_keymap('azerty', tmp_path / 'unused.ini', controls)
    qwerty = build_keymap('qwerty', tmp_path / 'unused.ini', controls)
    assert (azerty['q'], azerty['z'], azerty['m']) == (qwerty['a'], qwerty['w'], qwerty[';'])


def test_unknown_layout(controls, tmp_path):
    with pytest.raises(ConfigError, match="Unknown layout 'dvorak'"):
        build_keymap('dvorak', tmp_path / 'unused.ini', controls)


def test_custom_layout(controls, tmp_path):
    path = custom(tmp_path, 'C4 = q\nDb4 = w\nbb3 = A\nE4 = ;\nF4 = %\nG4 = é\nA4 = space\n')
    assert build_keymap('custom', path, controls) == {
        'q': 60, 'w': 61, 'a': 58, ';': 64, '%': 65, 'é': 67, 'space': 69}


def test_shipped_template_is_not_configured(controls):
    """The template ships with every note commented out"""
    with pytest.raises(LayoutNotConfiguredError):
        build_keymap('custom', SHIPPED_TEMPLATE, controls)


@pytest.mark.parametrize('content, message', [
    ('[OTHER]\nC4 = q\n', 'missing \\[NOTES\\] section'),
    ('[NOTES]\nC4 =\n', 'no key given'),
    ('[NOTES]\nH4 = q\n', 'Invalid note name'),
    ('[NOTES]\nC10 = q\n', 'outside the playable range'),
    ('[NOTES]\nC4 = ctrl\n', 'cannot be mapped'),
    ('[NOTES]\nC4 = q\nD4 = Q\n', "key 'q' is already used"),
    ('[NOTES]\nC4 = v\n', "already used by control layout_next"),
    ('[NOTES]\nC4 = q\nc4 = a\n', 'already exists'),
])
def test_invalid_custom_layouts(content, message, controls, tmp_path):
    path = tmp_path / 'custom_layout.ini'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ConfigError, match=message):
        build_keymap('custom', path, controls)


def test_missing_custom_layout_file(controls, tmp_path):
    with pytest.raises(ConfigError, match='Cannot read custom layout'):
        build_keymap('custom', tmp_path / 'missing.ini', controls)


def test_same_note_on_two_keys_warns(controls, tmp_path, caplog):
    keymap = build_keymap('custom', custom(tmp_path, 'C#4 = q\nDb4 = a\n'), controls)
    assert keymap == {'q': 61, 'a': 61}
    assert "both play C#4" in caplog.text


def test_control_keys_used_twice(controls, tmp_path):
    controls['record_stop'] = controls['record_start']
    with pytest.raises(ConfigError, match='used by both control record_start and control record_stop'):
        build_keymap('qwerty', tmp_path / 'unused.ini', controls)


def test_all_keymaps_cycle_order(controls, tmp_path):
    keymaps = build_all_keymaps('qwerty', custom(tmp_path, 'C3 = q\n'), controls)
    assert list(keymaps) == ['azerty', 'qwerty', 'custom']


def test_all_keymaps_leave_out_unconfigured_custom_layout(controls, caplog):
    caplog.set_level(logging.INFO)
    keymaps = build_all_keymaps('qwerty', SHIPPED_TEMPLATE, controls)
    assert list(keymaps) == ['azerty', 'qwerty']
    assert any(r.levelno == logging.INFO and 'not configured' in r.getMessage() for r in caplog.records)


def test_all_keymaps_leave_out_invalid_custom_layout(controls, tmp_path, caplog):
    keymaps = build_all_keymaps('qwerty', custom(tmp_path, 'C4 = ctrl\n'), controls)
    assert list(keymaps) == ['azerty', 'qwerty']
    assert any(r.levelno == logging.WARNING and 'cannot be mapped' in r.getMessage()
               for r in caplog.records)


def test_all_keymaps_initial_layout_must_be_valid(controls, tmp_path):
    with pytest.raises(ConfigError, match='cannot be mapped'):
        build_all_keymaps('custom', custom(tmp_path, 'C4 = ctrl\n'), controls)
