"""
Musical keyboard: turns key presses and releases into notes and controls.

Keys come from an input backend (see input_backends.py) as normalized key
names. Control keys trigger their control (see config.CONTROLS), other keys
play the note of the current layout (see keymap.py), shifted by whole
octaves.
"""

# Global imports
import logging
import threading
from typing import Callable, Dict

# Local imports
from config import KeyboardConfig
from errors import ConfigError
from input_backends import InputBackend, start_input
from keymap import build_all_keymaps
from music import MIDI_MAX, MIDI_MIN, midi_to_note

log = logging.getLogger(__name__)


class KeyboardHandler:
    """Layouts, octave shift, held notes, and control keys"""

    def __init__(self, keyboard_config: KeyboardConfig, control_keys: dict,
                 on_note: Callable[[str, int, bool], None], on_control: Callable[[str], None]):
        """
        Args:
            keyboard_config: [KEYBOARD] settings (see config.py)
            control_keys: {control name: normalized key} ([CONTROLS] settings)
            on_note: called with (key, MIDI note, pressed) when a note key is
                     pressed or released
            on_control: called with the control name when a control key is
                        pressed

        Raises:
            ConfigError: invalid layout, or octave shift out of range
        """
        self.control_keys = control_keys
        self._control_for_key = {key: name for name, key in control_keys.items()}
        self._on_note = on_note
        self._on_control = on_control

        self.layout = keyboard_config.layout  # Name of the current layout
        # {layout name: keymap}, available layouts in cycling order
        self.keymaps = build_all_keymaps(self.layout, keyboard_config.custom_layout, control_keys)
        # Keymap of the current layout: {normalized key: MIDI note}
        self.key_to_midi = self.keymaps[self.layout]
        # Octaves added to the notes of the layout
        self.octave_shift = keyboard_config.octave_shift
        if not self._octave_shift_fits(self.octave_shift):
            raise ConfigError(f"[KEYBOARD] OCTAVE_SHIFT = {self.octave_shift} puts notes of layout "
                              f"'{self.layout}' outside the playable range "
                              f"({midi_to_note(MIDI_MIN)} to {midi_to_note(MIDI_MAX)})")

        # Held note keys, with the note each one started: a release stops that
        # note even if the layout or octave changed in the meantime. Written
        # by the input thread, read by the TUI.
        self.pressed_keys: Dict[str, int] = {}
        self.key_lock = threading.Lock()

        self.backend: InputBackend = None  # Set by start()

        log.debug(f"Layouts: {', '.join(self.keymaps)} (current: {self.layout}, "
                  f"octave shift: {self.octave_shift:+d})")
        log.info(f"Control keys: {self.control_keys}")

    # ------------------------------------------------------------------
    # Layout and octave
    # ------------------------------------------------------------------

    def note_for_key(self, key: str) -> int:
        """MIDI note currently played by a mapped key (octave shift included)"""
        return self.key_to_midi[key] + 12 * self.octave_shift

    def _octave_shift_fits(self, shift: int) -> bool:
        """True if the current layout, shifted by `shift` octaves, stays
        within the playable range"""
        notes = self.key_to_midi.values()
        return min(notes) + 12 * shift >= MIDI_MIN and max(notes) + 12 * shift <= MIDI_MAX

    def shift_octave(self, delta: int):
        """Shift the layout by `delta` octaves, unless notes would leave the
        playable range. Held notes keep their pitch (see pressed_keys)."""
        if not self._octave_shift_fits(self.octave_shift + delta):
            log.info(f"Octave shift limit reached ({self.octave_shift:+d})")
            return
        self.octave_shift += delta
        log.info(f"Octave shift: {self.octave_shift:+d}")

    def next_layout(self):
        """Switch to the next available layout. Held notes keep playing
        until their key is released (see pressed_keys)."""
        names = list(self.keymaps)
        self.layout = names[(names.index(self.layout) + 1) % len(names)]
        self.key_to_midi = self.keymaps[self.layout]
        log.info(f"Layout: {self.layout}")
        # A wide custom layout may not fit the current shift (every layout
        # fits without shift: build_keymap checks the range)
        if not self._octave_shift_fits(self.octave_shift):
            log.info(f"Octave shift reset to +0: layout '{self.layout}' does not fit a "
                     f"{self.octave_shift:+d} shift")
            self.octave_shift = 0

    # ------------------------------------------------------------------
    # Key events (called from the backend's input thread)
    # ------------------------------------------------------------------

    def _on_key(self, key: str, pressed: bool):
        """A key was pressed or released (presses and releases come in
        pairs, see InputBackend)"""
        if not pressed:
            with self.key_lock:
                midi = self.pressed_keys.pop(key, None)
            if midi is not None:
                self._on_note(key, midi, False)
            return

        control = self._control_for_key.get(key)
        if control is not None:
            self._on_control(control)
            return
        if key not in self.key_to_midi:
            log.debug(f"Key '{key}' is not mapped to any note.")
            return
        midi = self.note_for_key(key)
        with self.key_lock:
            # Two physical keys may produce the same name (e.g. '+' on the
            # main and numeric keypads): one note per name
            if key in self.pressed_keys:
                return
            self.pressed_keys[key] = midi
        self._on_note(key, midi, True)

    def get_pressed_keys(self) -> Dict[str, int]:
        """Currently held note keys and their notes (thread-safe copy)"""
        with self.key_lock:
            return self.pressed_keys.copy()

    # ------------------------------------------------------------------
    # Lifecycle (delegated to the input backend)
    # ------------------------------------------------------------------

    def start(self):
        """
        Start keyboard input with the best available backend.

        Raises:
            SynthError: no usable keyboard input (the terminal is restored)
        """
        self.backend = start_input(self._on_key)

    def stop(self):
        """Stop keyboard input and restore the terminal"""
        if self.backend is not None:
            self.backend.stop()

    @property
    def capture_status(self) -> str:
        """Health of key capture, for the TUI status bar"""
        return self.backend.status if self.backend is not None else 'OFF'

    def on_alt_screen_enter(self):
        """Must be called after the TUI entered the alternate screen"""
        self.backend.on_alt_screen_enter()

    def on_alt_screen_exit(self):
        """Must be called before the TUI leaves the alternate screen"""
        self.backend.on_alt_screen_exit()
