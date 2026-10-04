"""
Keyboard layouts: which computer key plays which note.

Two built-in layouts cover C4 to E5 on the same physical keys, laid out like
a piano keyboard (natural notes on the home row, sharps on the row above),
for AZERTY and QWERTY keyboards. A custom layout file can map any note to
any key (see custom_layout.ini for its format).

Notes are MIDI numbers (see music.py). Keys use the normalized names
produced by keyboard_handler: the lowercase character for character keys,
or one of NAMED_KEYS.
"""

# Global imports
import configparser
import logging
from pathlib import Path

# Local imports
from synth.errors import ConfigError
from synth.music import MIDI_MAX, MIDI_MIN, midi_to_note, note_to_midi

log = logging.getLogger(__name__)

# Built-in layouts: keys in chromatic order, starting at BUILTIN_FIRST_NOTE
BUILTIN_LAYOUTS = {
    'azerty': 'qzsedftgyhujkolpm',
    'qwerty': 'awsedftgyhujkolp;',
}
BUILTIN_FIRST_NOTE = 'C4'

# All layouts, in the order the layout-next control cycles through them
LAYOUT_NAMES = (*BUILTIN_LAYOUTS, 'custom')

# Mappable keys that do not produce a character
NAMED_KEYS = ('esc', 'tab', 'enter', 'space')


class LayoutNotConfiguredError(ConfigError):
    """The custom layout file maps no note (e.g. untouched template)"""


def normalize_key(key: str, where: str) -> str:
    """
    Validate a key read from the configuration and return its normalized
    name. `where` describes the setting, for error messages.

    Raises:
        ConfigError: if the key cannot be mapped
    """
    name = key.strip().lower()
    if not name:
        raise ConfigError(f"{where}: no key given")
    if name in NAMED_KEYS or (len(name) == 1 and name.isprintable()):
        return name
    raise ConfigError(f"{where}: '{key}' cannot be mapped. Use a key producing a single "
                      f"character, or one of {', '.join(NAMED_KEYS)} (modifier keys such as "
                      f"ctrl or cmd are not available)")


def _builtin_entries(layout: str) -> list:
    """(key, midi, where) entries of a built-in layout"""
    first = note_to_midi(BUILTIN_FIRST_NOTE)
    return [(key, first + i, f"layout '{layout}'") for i, key in enumerate(BUILTIN_LAYOUTS[layout])]


def _custom_entries(path: Path) -> list:
    """(key, midi, where) entries of a custom layout file"""
    # No interpolation: '%' is a valid key
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # open() rather than parser.read(), which silently skips missing files
        with open(path, encoding='utf-8') as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        raise ConfigError(f"Cannot read custom layout: {e}") from e
    if not parser.has_section('NOTES'):
        raise ConfigError(f"{path}: missing [NOTES] section")

    entries = []
    for note, key in parser['NOTES'].items():
        where = f"{path.name}, note {note.capitalize()}"
        try:
            midi = note_to_midi(note)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}") from e
        entries.append((key, midi, where))

    if not entries:
        raise LayoutNotConfiguredError(f"{path}: no note mapped, uncomment at least one line of [NOTES]")
    return entries


def build_keymap(layout: str, custom_file: Path, control_keys: dict) -> dict:
    """
    Build the {key: midi} mapping of a layout.

    Args:
        layout: a BUILTIN_LAYOUTS name, or 'custom' to read custom_file
        custom_file: path of the custom layout file
        control_keys: {action: normalized key} of the control keys, which
                      notes must not reuse

    Raises:
        ConfigError: unknown layout, custom layout file unreadable or
                     malformed (e.g. same note written twice), invalid or out
                     of range (see music.py) note, invalid key, or a key used
                     twice (by notes and/or controls)
    """
    if layout == 'custom':
        entries = _custom_entries(custom_file)
    elif layout in BUILTIN_LAYOUTS:
        entries = _builtin_entries(layout)
    else:
        raise ConfigError(f"Unknown layout '{layout}': use {', '.join(BUILTIN_LAYOUTS)} or custom")

    # Who uses each key, to report conflicts
    key_users = {}
    for action, key in control_keys.items():
        if key in key_users:
            raise ConfigError(f"Key '{key}' is used by both {key_users[key]} and control {action}")
        key_users[key] = f"control {action}"

    keymap = {}
    key_by_midi = {}
    for key, midi, where in entries:
        name = normalize_key(key, where)
        if name in key_users:
            raise ConfigError(f"{where}: key '{name}' is already used by {key_users[name]}")
        key_users[name] = where

        if not MIDI_MIN <= midi <= MIDI_MAX:
            raise ConfigError(f"{where}: {midi_to_note(midi)} is outside the playable range "
                              f"({midi_to_note(MIDI_MIN)} to {midi_to_note(MIDI_MAX)})")
        if midi in key_by_midi:
            # Allowed, but probably unintentional
            log.warning(f"Keys '{key_by_midi[midi]}' and '{name}' both play {midi_to_note(midi)}")
        key_by_midi[midi] = name
        keymap[name] = midi

    return keymap


def build_all_keymaps(initial: str, custom_file: Path, control_keys: dict) -> dict:
    """
    Build the keymaps of all layouts (see build_keymap), as {layout name:
    keymap} in cycling order.

    The initial layout must be valid: its errors propagate. Other invalid
    layouts are left out of the cycle, with a log line saying why.
    """
    keymaps = {initial: build_keymap(initial, custom_file, control_keys)}
    for name in LAYOUT_NAMES:
        if name == initial:
            continue
        try:
            keymaps[name] = build_keymap(name, custom_file, control_keys)
        except LayoutNotConfiguredError:
            log.info(f"Layout '{name}' not configured ({custom_file.name}), left out of the layout cycle")
        except ConfigError as e:
            log.warning(f"Layout '{name}' left out of the layout cycle: {e}")
    return {name: keymaps[name] for name in LAYOUT_NAMES if name in keymaps}
