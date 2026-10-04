"""
Keyboard input for the terminal synthesizer.

A synthesizer needs key *release* events, which a classic tty does not
deliver. Two backends are therefore used, picked automatically at startup:

1. Kitty keyboard protocol (preferred): modern terminals (kitty, iTerm2 3.5+,
   WezTerm, Alacritty, Ghostty, foot, ...) report key press / repeat / release
   as escape codes over stdin. Event-driven, layout-aware, no OS permissions,
   and it works precisely because the terminal window is focused.
   Spec: https://sw.kovidgoyal.net/kitty/keyboard-protocol/

2. pynput global hook (fallback) for terminals without the protocol. On
   macOS this requires the Input Monitoring permission, and it is blocked
   while "Secure Keyboard Entry" is active in the focused terminal (a
   Terminal.app / iTerm2 menu option, sometimes enabled by password
   managers). Both conditions are detected and surfaced in the TUI status
   bar; Secure Keyboard Entry is the classic cause of "keys only work when
   the terminal is NOT focused".
"""

# Global imports
import codecs
import configparser
import ctypes
import functools
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Callable, Dict, Optional

# Local imports
from errors import ConfigError, SynthError
from keymap import build_all_keymaps, normalize_key
from logger import log
from music import MIDI_MAX, MIDI_MIN, midi_to_note

# Kitty protocol progressive enhancement flags:
# 1 = disambiguate escape codes, 2 = report event types (press/repeat/release),
# 8 = report all keys as escape codes, 16 = report associated text
KITTY_FLAGS = 1 + 2 + 8 + 16

# How long to wait for the terminal to answer the capability query (seconds)
KITTY_QUERY_TIMEOUT = 0.3

# CSI sequence: ESC [ <optional private prefix> <params> <final letter>
_CSI_RE = re.compile(r'^\x1b\[([?<>=]?)([0-9:;]*)([A-Za-z~])')

# Functional key codepoints we care about (kitty encodes them as CSI <code> u)
_CODE_NAMES = {27: 'esc', 9: 'tab', 13: 'enter', 32: 'space'}

# Plain characters that map to named keys (flag-2-only terminals may still
# send text-producing key presses as plain bytes)
_CHAR_NAMES = {'\t': 'tab', '\r': 'enter', '\n': 'enter', ' ': 'space'}

# Kitty functional key codes live at U+E000 and above (Unicode Private Use
# Area); they are not characters and must not be chr()'d into key names.
_FUNCTIONAL_KEY_FLOOR = 0xE000

# Kitty key event types
_EVENT_PRESS, _EVENT_REPEAT, _EVENT_RELEASE = 1, 2, 3

# Kitty modifier bits (the modifiers field is sent as 1 + bitmask):
# shift 1, alt 2, ctrl 4, super 8, hyper 16, meta 32, caps lock 64, num lock 128
_MOD_CTRL = 4
# Modifiers turning a key press into a shortcut (no note played). Shift is
# excluded (needed for '+' on many layouts), as are the lock bits.
_SHORTCUT_MODS = 2 | 4 | 8 | 16 | 32

# Focus reporting (DEC private mode 1004): the terminal sends CSI I / CSI O
# when its window gains / loses focus. Unlike the kitty flags, this mode is
# shared by the main and alternate screens.
_FOCUS_REPORTING_ON, _FOCUS_REPORTING_OFF = '\x1b[?1004h', '\x1b[?1004l'

@functools.cache
def _carbon() -> Optional[ctypes.CDLL]:
    """macOS Carbon framework, loaded on first use only. None if it cannot
    be loaded: logged once, Secure Keyboard Entry detection is then off
    (a mere diagnostic, not worth stopping the synthesizer)."""
    try:
        return ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
    except OSError as e:
        log(f"Secure Keyboard Entry detection unavailable: {e}", 2)
        return None


def _secure_input_active() -> bool:
    """True if macOS Secure Keyboard Entry is currently active (it blocks
    global event taps while the enabling app - usually a terminal - is
    focused). Cheap enough to call from the TUI refresh loop."""
    if sys.platform != 'darwin':
        return False
    carbon = _carbon()
    return carbon is not None and bool(carbon.IsSecureEventInputEnabled())
    try:
        if _carbon is None:
            _carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        return bool(_carbon.IsSecureEventInputEnabled())
    except OSError as e:
        log(f"Could not query Secure Keyboard Entry state: {e}", 2)
        return False


class KeyboardHandler:
    """Keyboard events and key mapping (notes and controls)"""

    def __init__(self, config_file):
        self.config_file = config_file
        config = configparser.ConfigParser()
        config.read(config_file)
        self.keymaps = {}      # {layout name: keymap}, available layouts in cycling order
        self.layout = None     # Name of the current layout
        self.key_to_midi = {}  # Keymap of the current layout: {normalized key: MIDI note}
        self.octave_shift = 0  # Octaves added to the notes of the layout
        # Held note keys, with the note each one started: a release stops that
        # note even if the layout changed in the meantime
        self.pressed_keys: Dict[str, int] = {}
        self.key_lock = threading.Lock()
        # Physical key id -> key name resolved at press time. Releases are
        # looked up here rather than resolved again, as the resolved name
        # may depend on the modifiers held at that moment (Shift, Ctrl...).
        # Only ever accessed from the active backend's input thread.
        self._held: dict = {}
        self.volume = config.getfloat('AUDIO', 'DEFAULT_VOLUME', fallback=0.5)
        self.volume_lock = threading.Lock()
        self.running = False

        # Backend state
        self.backend = None  # 'kitty' or 'pynput', set in start()
        self._saved_term_attrs = None
        self._reader_thread = None
        self._listener = None  # pynput listener
        self._input_monitoring_ok = True

        # Callbacks to external components
        self.on_note_on_callback: Optional[Callable] = None
        self.on_note_off_callback: Optional[Callable] = None
        self.on_volume_change_callback: Optional[Callable] = None
        self.on_quit_callback: Optional[Callable] = None
        self.on_record_start_callback: Optional[Callable] = None
        self.on_record_stop_callback: Optional[Callable] = None
        self.on_instrument_next_callback: Optional[Callable] = None

        self.load_key_mapping()

    def load_key_mapping(self):
        """Load the control keys and the note layout from the configuration
        file (raises ValueError on an invalid or conflicting mapping)"""
        config = configparser.ConfigParser()
        config.read(self.config_file)

        controls = {
            'volume_up': config.get('CONTROLS', 'VOLUME_UP', fallback='+'),
            'volume_down': config.get('CONTROLS', 'VOLUME_DOWN', fallback='-'),
            'quit': config.get('CONTROLS', 'QUIT', fallback='esc'),
            'record_start': config.get('CONTROLS', 'RECORD_START', fallback='b'),
            'record_stop': config.get('CONTROLS', 'RECORD_STOP', fallback='n'),
            'instrument_next': config.get('CONTROLS', 'INSTRUMENT_NEXT', fallback='tab'),
            'layout_next': config.get('CONTROLS', 'LAYOUT_NEXT', fallback='v'),
            'octave_down': config.get('CONTROLS', 'OCTAVE_DOWN', fallback='('),
            'octave_up': config.get('CONTROLS', 'OCTAVE_UP', fallback=')'),
        }
        self.control_keys = {action: normalize_key(key, f"control {action}")
                             for action, key in controls.items()}

        self.layout = config.get('KEYBOARD', 'LAYOUT', fallback='qwerty')
        # The custom layout path is relative to the configuration file
        custom_file = Path(self.config_file).parent / config.get(
            'KEYBOARD', 'CUSTOM_LAYOUT', fallback='custom_layout.ini')
        self.keymaps = build_all_keymaps(self.layout, custom_file, self.control_keys)
        self.key_to_midi = self.keymaps[self.layout]
        self.octave_shift = config.getint('KEYBOARD', 'OCTAVE_SHIFT', fallback=0)
        if not self._octave_shift_fits(self.octave_shift):
            raise ConfigError(f"OCTAVE_SHIFT = {self.octave_shift} puts notes of layout '{self.layout}' "
                              f"outside the playable range ({midi_to_note(MIDI_MIN)} to "
                              f"{midi_to_note(MIDI_MAX)})")

        log(f"Layouts: {', '.join(self.keymaps)} (current: {self.layout}, "
            f"octave shift: {self.octave_shift:+d})", 0)
        log(f"Control keys: {self.control_keys}", 1)

    def note_for_key(self, key: str) -> int:
        """MIDI note currently played by a mapped key (octave shift included)"""
        return self.key_to_midi[key] + 12 * self.octave_shift

    def _octave_shift_fits(self, shift: int) -> bool:
        """True if the current layout, shifted by `shift` octaves, stays
        within the playable range"""
        notes = self.key_to_midi.values()
        return min(notes) + 12 * shift >= MIDI_MIN and max(notes) + 12 * shift <= MIDI_MAX

    def _shift_octave(self, delta: int):
        """Shift the layout by `delta` octaves, unless notes would leave the
        playable range. Held notes keep their pitch (see pressed_keys)."""
        if not self._octave_shift_fits(self.octave_shift + delta):
            log(f"Octave shift limit reached ({self.octave_shift:+d})", 1)
            return
        self.octave_shift += delta
        log(f"Octave shift: {self.octave_shift:+d}", 1)

    def _next_layout(self):
        """Switch to the next available layout. Held notes keep playing
        until their key is released (see pressed_keys)."""
        names = list(self.keymaps)
        self.layout = names[(names.index(self.layout) + 1) % len(names)]
        self.key_to_midi = self.keymaps[self.layout]
        log(f"Layout: {self.layout}", 1)
        # A wide custom layout may not fit the current shift (every layout
        # fits without shift: build_keymap checks the range)
        if not self._octave_shift_fits(self.octave_shift):
            log(f"Octave shift reset to +0: layout '{self.layout}' does not fit a "
                f"{self.octave_shift:+d} shift", 1)
            self.octave_shift = 0

    def set_callbacks(self,
                      on_note_on: Callable = None,
                      on_note_off: Callable = None,
                      on_volume_change: Callable = None,
                      on_quit: Callable = None,
                      on_record_start: Callable = None,
                      on_record_stop: Callable = None,
                      on_instrument_next: Callable = None):
        """Set callback functions for events"""
        self.on_note_on_callback = on_note_on
        self.on_note_off_callback = on_note_off
        self.on_volume_change_callback = on_volume_change
        self.on_quit_callback = on_quit
        self.on_record_start_callback = on_record_start
        self.on_record_stop_callback = on_record_stop
        self.on_instrument_next_callback = on_instrument_next

    # ------------------------------------------------------------------
    # Key event dispatch (shared by both backends)
    # ------------------------------------------------------------------

    def _key_down(self, key_id, key_name: str):
        """Press edge of a physical key (auto-repeats are ignored)"""
        if key_id in self._held:
            return
        self._held[key_id] = key_name
        self._handle_key_down(key_name)

    def _key_up(self, key_id):
        """Release edge of a physical key, dispatched under its press name"""
        key_name = self._held.pop(key_id, None)
        if key_name is not None:
            self._handle_key_up(key_name)

    def _release_all(self):
        """Release every held key (their release events will never come)"""
        for key_id in list(self._held):
            self._key_up(key_id)

    def _handle_key_down(self, key_char: str):
        """Handle a key-down edge for a normalized key string"""
        if not self.running:
            return

        if key_char == self.control_keys['volume_up']:
            with self.volume_lock:
                self.volume = min(1.0, self.volume + 0.05)
                if self.on_volume_change_callback:
                    self.on_volume_change_callback(self.volume)
            return
        if key_char == self.control_keys['volume_down']:
            with self.volume_lock:
                self.volume = max(0.0, self.volume - 0.05)
                if self.on_volume_change_callback:
                    self.on_volume_change_callback(self.volume)
            return
        if key_char == self.control_keys['quit']:
            # The quit callback's owner runs the full shutdown sequence
            if self.on_quit_callback:
                self.on_quit_callback()
            return
        if key_char == self.control_keys['record_start']:
            if self.on_record_start_callback:
                self.on_record_start_callback()
            return
        if key_char == self.control_keys['record_stop']:
            if self.on_record_stop_callback:
                self.on_record_stop_callback()
            return
        if key_char == self.control_keys['instrument_next']:
            if self.on_instrument_next_callback:
                self.on_instrument_next_callback()
            return
        if key_char == self.control_keys['layout_next']:
            self._next_layout()
            return
        if key_char == self.control_keys['octave_down']:
            self._shift_octave(-1)
            return
        if key_char == self.control_keys['octave_up']:
            self._shift_octave(+1)
            return

        if key_char not in self.key_to_midi:
            log(f"Key '{key_char}' is not mapped to any note.", 0)
            return
        midi = self.note_for_key(key_char)
        with self.key_lock:
            if key_char not in self.pressed_keys:
                self.pressed_keys[key_char] = midi
                if self.on_note_on_callback:
                    self.on_note_on_callback(key_char, midi)

    def _handle_key_up(self, key_char: str):
        """Handle a key-up edge for a normalized key string"""
        with self.key_lock:
            midi = self.pressed_keys.pop(key_char, None)
            if midi is not None and self.on_note_off_callback:
                self.on_note_off_callback(key_char, midi)

    # ------------------------------------------------------------------
    # Kitty keyboard protocol backend
    # ------------------------------------------------------------------

    @staticmethod
    def _write_term(seq: str):
        """Write an escape sequence directly to the terminal, bypassing any
        stdout redirection (rich's Live redirects sys.stdout)."""
        try:
            os.write(sys.__stdout__.fileno(), seq.encode('ascii'))
        except OSError as e:
            log(f"Could not write to terminal: {e}", 2)

    def _detect_kitty_protocol(self) -> bool:
        """
        Query kitty keyboard protocol support. We send the protocol query
        (CSI ? u) followed by a primary Device Attributes query (CSI c) that
        virtually every terminal answers: if the DA reply arrives without a
        kitty reply before it, the protocol is not supported. The DA reply
        bounds the wait so unsupporting terminals don't cost the full timeout.
        """
        fd = sys.stdin.fileno()
        self._write_term('\x1b[?u\x1b[c')

        deadline = time.monotonic() + KITTY_QUERY_TIMEOUT
        buf = b''
        while time.monotonic() < deadline:
            timeout = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            data = os.read(fd, 256)
            if not data:
                break
            buf += data
            if re.search(rb'\x1b\[\?[\d;]*u', buf):
                return True
            if re.search(rb'\x1b\[\?[\d;]*c', buf):
                return False
        return False

    def _push_kitty_flags(self):
        """Enable the protocol (pushes onto the terminal's flag stack)"""
        self._write_term(f'\x1b[>{KITTY_FLAGS}u')

    def _pop_kitty_flags(self):
        """Disable the protocol (pops the flag stack; ignored if empty)"""
        self._write_term('\x1b[<u')

    def on_alt_screen_enter(self):
        """Must be called after entering the alternate screen: the main and
        alternate screens have independent kitty flag stacks."""
        if self.backend == 'kitty' and self.running:
            self._push_kitty_flags()

    def on_alt_screen_exit(self):
        """Must be called before leaving the alternate screen (see above)"""
        if self.backend == 'kitty' and self.running:
            self._pop_kitty_flags()

    def _key_from_code(self, keycode: int, text: str) -> Optional[str]:
        """Resolve a kitty key code (+ optional associated text) to a config
        key name. Returns None for keys we don't handle (modifiers, F-keys...)"""
        if keycode in _CODE_NAMES:
            return _CODE_NAMES[keycode]
        if text and text.isprintable():
            return text.lower()
        if keycode >= _FUNCTIONAL_KEY_FLOOR:
            return None
        char = chr(keycode)
        return char.lower() if char.isprintable() else None

    def _dispatch_kitty_event(self, params: str):
        """Parse and dispatch one 'CSI params u' key event.
        Format: keycode[:alternates] [; modifiers[:event-type] [; text-codepoints]]"""
        # _CSI_RE only lets digits, ':' and ';' through: fields are digit
        # strings, possibly empty
        fields = params.split(';')
        code = fields[0].split(':')[0]
        if not code:
            return
        keycode = int(code)

        modifiers = 0
        event = _EVENT_PRESS
        if len(fields) > 1:
            mods, _, event_type = fields[1].partition(':')
            if mods:
                modifiers = int(mods) - 1
            if event_type.isdigit():
                event = int(event_type)

        text = ''
        if len(fields) > 2:
            codepoints = [int(cp) for cp in fields[2].split(':') if cp]
            # Malformed text (beyond Unicode) is ignored: the keycode remains
            if all(cp <= sys.maxunicode for cp in codepoints):
                text = ''.join(map(chr, codepoints))

        # The keycode is the unshifted key of the current layout, identical
        # for press and release: it is the key's physical identity.
        if event == _EVENT_RELEASE:
            # Never filtered on modifiers: pressing one while a note is held
            # must not leave that note stuck.
            self._key_up(keycode)
            return
        if event != _EVENT_PRESS:
            return  # _EVENT_REPEAT: the note is already held

        if modifiers & _MOD_CTRL and keycode == ord('c'):
            # The protocol sends Ctrl+C as an escape code instead of letting
            # the tty raise SIGINT: raise it ourselves so both backends share
            # the same interrupt path.
            signal.raise_signal(signal.SIGINT)
            return
        if modifiers & _SHORTCUT_MODS:
            return

        key_char = self._key_from_code(keycode, text)
        if key_char is not None:
            self._key_down(keycode, key_char)

    def _consume_input(self, buf: str) -> str:
        """Consume parseable input from buf, return the unparsed remainder"""
        while buf:
            if buf[0] != '\x1b':
                # Plain character: terminals honoring only flag 2 still send
                # text-producing presses as plain bytes (releases arrive as
                # CSI u events), so this is a key-down.
                char = buf[0]
                buf = buf[1:]
                key_char = _CHAR_NAMES.get(char)
                if key_char is None and char.isprintable():
                    key_char = char.lower()
                if key_char:
                    self._handle_key_down(key_char)
                continue

            match = _CSI_RE.match(buf)
            if match:
                prefix, params, final = match.groups()
                if final == 'u' and not prefix:
                    self._dispatch_kitty_event(params)
                elif final == 'O' and not prefix and not params:
                    # Focus lost: keys released in another window never
                    # report here, release them now to avoid stuck notes
                    self._release_all()
                # Other sequences (focus in, query replies...) are dropped
                buf = buf[match.end():]
                continue

            if len(buf) >= 16:
                # Unparseable escape sequence: drop the ESC and resync
                buf = buf[1:]
                continue
            break  # Incomplete sequence: wait for more bytes
        return buf

    def _kitty_read_loop(self):
        """Reader thread: blocking select on stdin, parse, dispatch.
        Any exception ends the thread and, through threading.excepthook,
        shuts the synthesizer down (see Synthesizer.run)."""
        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        buf = ''
        log("Keyboard input: kitty keyboard protocol (stdin)", 1)

        while self.running:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                continue
            data = os.read(fd, 256)
            if not data:
                raise SynthError("Keyboard input closed")
            buf += decoder.decode(data)
            buf = self._consume_input(buf)

    # ------------------------------------------------------------------
    # pynput fallback backend
    # ------------------------------------------------------------------

    @staticmethod
    def _pynput_key_id(key):
        """Physical identity of a pynput key: the virtual key code of
        character keys (does not depend on modifiers, unlike key.char),
        or the Key member itself for special keys"""
        vk = getattr(key, 'vk', None)
        return key if vk is None else vk

    def _pynput_key_char(self, key, pk) -> Optional[str]:
        """Extract a normalized key string from a pynput key object"""
        if hasattr(key, 'char') and key.char is not None:
            return key.char.lower()
        for name in ('esc', 'tab', 'space', 'enter'):
            if key == getattr(pk.Key, name):
                return name
        return str(key).replace('Key.', '').replace("'", "").lower()

    def _start_pynput(self):
        """Start the pynput global listener and run macOS diagnostics
        (raises SynthError if pynput is unavailable)"""
        try:
            from pynput import keyboard as pk
        except ImportError as e:
            # Not only "not installed": pynput also raises ImportError when
            # it has no usable backend (e.g. Linux without X display)
            raise SynthError(
                f"Keyboard input unavailable: this terminal lacks the kitty keyboard "
                f"protocol, and pynput cannot be used ({e}). Install pynput, or use a "
                f"terminal supporting the protocol (iTerm2 3.5+, kitty, WezTerm, "
                f"Alacritty, Ghostty).") from e

        # pynput does not report modifiers with key events: track the ones
        # turning a press into a shortcut (no note played), as the kitty
        # backend does. Shift is excluded (needed for '+' on many layouts),
        # and so is AltGr (used to type characters).
        shortcut_mods = {pk.Key.ctrl, pk.Key.ctrl_l, pk.Key.ctrl_r,
                         pk.Key.alt, pk.Key.alt_l, pk.Key.alt_r,
                         pk.Key.cmd, pk.Key.cmd_l, pk.Key.cmd_r}
        held_mods = set()

        def on_press(key):
            if key in shortcut_mods:
                held_mods.add(key)
            elif not held_mods:
                key_char = self._pynput_key_char(key, pk)
                if key_char:
                    self._key_down(self._pynput_key_id(key), key_char)

        def on_release(key):
            held_mods.discard(key)
            self._key_up(self._pynput_key_id(key))

        self._listener = pk.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()
        self._listener.wait()
        log("Keyboard input: global hook (pynput). For native terminal key "
            "handling, use a terminal with the kitty keyboard protocol "
            "(iTerm2 3.5+, kitty, WezTerm, Alacritty, Ghostty).", 1)

        # macOS diagnostics: listen-only event taps need Input Monitoring
        # (pynput itself only checks the unrelated Accessibility permission)
        if sys.platform == 'darwin':
            try:
                import Quartz
                if hasattr(Quartz, 'CGPreflightListenEventAccess'):
                    self._input_monitoring_ok = bool(Quartz.CGPreflightListenEventAccess())
                    if not self._input_monitoring_ok:
                        log("Missing macOS 'Input Monitoring' permission: enable your "
                            "terminal in System Settings > Privacy & Security > Input "
                            "Monitoring, then restart.", 3)
                        Quartz.CGRequestListenEventAccess()
            except ImportError:
                log("Quartz unavailable: cannot check Input Monitoring permission", 2)

            if _secure_input_active():
                log("Secure Keyboard Entry is ACTIVE: keys will NOT be captured "
                    "while this terminal is focused. Disable it in your terminal's "
                    "menu (Terminal.app / iTerm2: 'Secure Keyboard Entry').", 3)

        # Drain stdin so typed characters don't pile up in the tty buffer
        # (events come from the global hook, not from stdin); this thread
        # also watches the listener
        self._reader_thread = threading.Thread(
            target=self._drain_stdin_loop, name='stdin-drain', daemon=True)
        self._reader_thread.start()

    def _drain_stdin_loop(self):
        """
        Discard stdin bytes while the pynput backend is active.

        Also watches the pynput listener: pynput catches the exceptions of
        our callbacks itself, stops the listener and keeps the exception
        until join(). Joining a dead listener re-raises it here, in a thread
        of ours, so that like any exception of this thread it shuts the
        synthesizer down through threading.excepthook (see Synthesizer.run).
        """
        fd = sys.stdin.fileno()
        while self.running:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if ready and not os.read(fd, 4096):
                raise SynthError("Keyboard input closed")
            if not self._listener.is_alive():
                self._listener.join()  # Re-raises the callback exception, if any
                raise SynthError("Keyboard listener (pynput) stopped unexpectedly")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Start keyboard monitoring (detects the best available backend).

        Raises:
            SynthError: no usable keyboard input (the terminal is restored)
        """
        if self.running:
            return

        if not sys.stdin.isatty():
            raise SynthError("Keyboard input unavailable: stdin is not a terminal")

        self.running = True
        self._enter_cbreak()

        if self._detect_kitty_protocol():
            self.backend = 'kitty'
            self._push_kitty_flags()
            self._write_term(_FOCUS_REPORTING_ON)
            self._reader_thread = threading.Thread(
                target=self._kitty_read_loop, name='kitty-keys', daemon=True)
            self._reader_thread.start()
        else:
            self.backend = 'pynput'
            try:
                self._start_pynput()
            except SynthError:
                self.stop()  # Restore the terminal before reporting
                raise

    def stop(self):
        """Stop keyboard monitoring and restore the terminal"""
        self.running = False

        if self.backend == 'kitty':
            self._write_term(_FOCUS_REPORTING_OFF)
            self._pop_kitty_flags()

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

        if self._listener is not None:
            if self._listener.running:
                self._listener.stop()
            self._listener = None

        self._leave_cbreak()
        log("Keyboard monitoring stopped", 0)

    def _enter_cbreak(self):
        """cbreak: char-at-a-time input, no echo. The tty still raises SIGINT
        on a raw Ctrl+C byte; the kitty protocol encodes Ctrl+C as an escape
        code instead, handled in _dispatch_kitty_event."""
        fd = sys.stdin.fileno()
        self._saved_term_attrs = termios.tcgetattr(fd)
        # TCSANOW: apply immediately. The default TCSAFLUSH would wait
        # for pending terminal output to drain, which can block.
        tty.setcbreak(fd, termios.TCSANOW)
        # Belt and braces: ensure echo is off (setcbreak behavior around
        # ECHO changed across Python versions)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _leave_cbreak(self):
        """Restore saved terminal attributes and flush pending input (best
        effort: a failure is logged, the rest of the shutdown must go on)"""
        if self._saved_term_attrs is None:
            return  # cbreak was never entered
        fd = sys.stdin.fileno()
        try:
            termios.tcsetattr(fd, termios.TCSANOW, self._saved_term_attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError) as e:
            log(f"Could not restore terminal mode: {e}", 2)
        self._saved_term_attrs = None

    @property
    def capture_status(self) -> str:
        """Health of keyboard capture, for the TUI status bar"""
        if not self.running or self.backend is None:
            return 'OFF'
        if self.backend == 'kitty':
            if self._reader_thread is None or not self._reader_thread.is_alive():
                return 'DEAD'
            return 'OK (kitty protocol)'
        # pynput backend
        if not self._input_monitoring_ok:
            return 'NO PERMISSION'
        if _secure_input_active():
            return 'BLOCKED (secure input)'
        if self._listener is None or not self._listener.is_alive():
            return 'DEAD'
        return 'OK (global hook)'

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_pressed_keys(self) -> Dict[str, int]:
        """Get currently pressed note keys and their notes thread-safely"""
        with self.key_lock:
            return self.pressed_keys.copy()

    def get_volume(self) -> float:
        """Get current volume thread-safely"""
        with self.volume_lock:
            return self.volume

    def set_volume(self, volume: float):
        """Set volume thread-safely"""
        with self.volume_lock:
            self.volume = max(0.0, min(1.0, volume))

    def is_key_pressed(self, key: str) -> bool:
        """Check if a specific key is currently pressed"""
        with self.key_lock:
            return key in self.pressed_keys

    def clear_pressed_keys(self):
        """Clear all pressed keys (useful for reset)"""
        with self.key_lock:
            self.pressed_keys.clear()

    def get_all_keys(self):
        """Get all mapped keys"""
        return list(self.key_to_midi.keys())

    def print_mappings(self):
        """Print all key mappings"""
        log("\nKey Mappings:", 0)
        for key, midi in self.key_to_midi.items():
            log(f"  {key:2} -> {midi_to_note(midi)}", 0)


def test_keyboard():
    """Interactive keyboard test"""
    log("Testing keyboard handler...", 1)
    log("Press keys (quit key from config to stop)", 1)

    try:
        handler = KeyboardHandler('config.ini')

        def on_note_on(key, midi):
            log(f"♪ {key} -> {midi_to_note(midi)}", 0)

        def on_note_off(key, midi):
            log(f"♫ {key} -> {midi_to_note(midi)} (off)", 0)

        def on_quit():
            log("Quit callback triggered", 0)
            handler.running = False

        handler.set_callbacks(
            on_note_on=on_note_on,
            on_note_off=on_note_off,
            on_quit=on_quit,
        )

        handler.start()
        try:
            while handler.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        handler.stop()
        return True

    except Exception as e:
        log(f"Keyboard test failed: {e}", 3)
        return False
