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
import os
import re
import select
import sys
import termios
import threading
import time
import tty
from typing import Callable, Optional, Set

# Local imports
from logger import log

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

_carbon = None


def _secure_input_active() -> bool:
    """True if macOS Secure Keyboard Entry is currently active (it blocks
    global event taps while the enabling app - usually a terminal - is
    focused). Cheap enough to call from the TUI refresh loop."""
    global _carbon
    if sys.platform != 'darwin':
        return False
    try:
        if _carbon is None:
            _carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        return bool(_carbon.IsSecureEventInputEnabled())
    except OSError as e:
        log(f"Could not query Secure Keyboard Entry state: {e}", 2)
        return False


class KeyboardHandler:
    """Keyboard events, layout mapping, and base note conversion"""

    def __init__(self, config_file):
        self.config_file = config_file
        config = configparser.ConfigParser()
        config.read(config_file)
        self.base_note = config.get('AUDIO', 'BASE_NOTE', fallback='C4')
        self.key_to_note = {}
        self.pressed_keys: Set[str] = set()
        self.key_lock = threading.Lock()
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
        """Load key mapping from configuration file and convert to actual notes"""
        config = configparser.ConfigParser()
        config.read(self.config_file)

        self.control_keys = {
            'volume_up': config.get('CONTROLS', 'VOLUME_UP', fallback='+'),
            'volume_down': config.get('CONTROLS', 'VOLUME_DOWN', fallback='-'),
            'quit': config.get('CONTROLS', 'QUIT', fallback='esc'),
            'record_start': config.get('CONTROLS', 'RECORD_START', fallback='w'),
            'record_stop': config.get('CONTROLS', 'RECORD_STOP', fallback='x'),
            'instrument_next': config.get('CONTROLS', 'INSTRUMENT_NEXT', fallback='tab'),
        }

        base_octave = int(self.base_note[-1])
        for layout_note, key in config['KEYS'].items():
            note_name = layout_note[:-1]
            octave_offset = int(layout_note[-1])
            actual_note = f"{note_name}{base_octave + octave_offset}"
            self.key_to_note[key] = actual_note.upper()

        log(f"Loaded {len(self.key_to_note)} key mappings from {self.config_file}", 0)
        log(f"Control keys: {self.control_keys}", 1)

    def get_note_for_key(self, key_stroke):
        """Get the actual note for a keystroke, or None if not mapped"""
        note = self.key_to_note.get(key_stroke)
        if note is None:
            log(f"Key '{key_stroke}' is not mapped to any note.", 0)
        return note

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

        actual_note = self.get_note_for_key(key_char)
        if actual_note:
            with self.key_lock:
                if key_char not in self.pressed_keys:
                    self.pressed_keys.add(key_char)
                    if self.on_note_on_callback:
                        self.on_note_on_callback(key_char, actual_note)

    def _handle_key_up(self, key_char: str):
        """Handle a key-up edge for a normalized key string"""
        actual_note = self.key_to_note.get(key_char)
        if not actual_note:
            return
        with self.key_lock:
            if key_char in self.pressed_keys:
                self.pressed_keys.discard(key_char)
                if self.on_note_off_callback:
                    self.on_note_off_callback(key_char, actual_note)

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
            try:
                ready, _, _ = select.select([fd], [], [], timeout)
            except (OSError, ValueError):
                return False
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
        try:
            char = chr(keycode)
        except ValueError:
            return None
        return char.lower() if char.isprintable() else None

    def _dispatch_kitty_event(self, params: str):
        """Parse and dispatch one 'CSI params u' key event.
        Format: keycode[:alternates] [; modifiers[:event-type] [; text-codepoints]]"""
        fields = params.split(';')
        try:
            keycode = int(fields[0].split(':')[0])
        except (ValueError, IndexError):
            return

        event = _EVENT_PRESS
        if len(fields) > 1 and ':' in fields[1]:
            try:
                event = int(fields[1].split(':')[1])
            except (ValueError, IndexError):
                pass

        text = ''
        if len(fields) > 2 and fields[2]:
            try:
                text = ''.join(chr(int(cp)) for cp in fields[2].split(':') if cp)
            except (ValueError, OverflowError):
                text = ''

        key_char = self._key_from_code(keycode, text)
        if key_char is None:
            return

        if event == _EVENT_PRESS:
            self._handle_key_down(key_char)
        elif event == _EVENT_RELEASE:
            self._handle_key_up(key_char)
        # _EVENT_REPEAT is ignored: the note is already held

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
                # Other sequences (query replies, etc.) are dropped
                buf = buf[match.end():]
                continue

            if len(buf) >= 16:
                # Unparseable escape sequence: drop the ESC and resync
                buf = buf[1:]
                continue
            break  # Incomplete sequence: wait for more bytes
        return buf

    def _kitty_read_loop(self):
        """Reader thread: blocking select on stdin, parse, dispatch"""
        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        buf = ''
        log("Keyboard input: kitty keyboard protocol (stdin)", 1)

        while self.running:
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 256)
            except OSError:
                break
            if not data:
                break
            try:
                buf += decoder.decode(data)
                buf = self._consume_input(buf)
            except Exception as e:
                log(f"Error processing keyboard input: {e}", 3)
                buf = ''

    # ------------------------------------------------------------------
    # pynput fallback backend
    # ------------------------------------------------------------------

    def _pynput_key_char(self, key, pk) -> Optional[str]:
        """Extract a normalized key string from a pynput key object"""
        if hasattr(key, 'char') and key.char is not None:
            return key.char.lower()
        for name in ('esc', 'tab', 'space', 'enter'):
            if key == getattr(pk.Key, name):
                return name
        return str(key).replace('Key.', '').replace("'", "").lower()

    def _start_pynput(self) -> bool:
        """Start the pynput global listener and run macOS diagnostics"""
        try:
            from pynput import keyboard as pk
        except ImportError:
            log("Terminal lacks the kitty keyboard protocol and pynput is not "
                "installed: keyboard input unavailable.", 3)
            log("Either `pip install pynput` or use a terminal supporting the "
                "kitty keyboard protocol (iTerm2 3.5+, kitty, WezTerm, ...).", 3)
            return False

        def on_press(key):
            try:
                key_char = self._pynput_key_char(key, pk)
                if key_char:
                    self._handle_key_down(key_char)
            except Exception as e:
                log(f"Error in key press handler ({key}): {e}", 3)

        def on_release(key):
            try:
                key_char = self._pynput_key_char(key, pk)
                if key_char:
                    self._handle_key_up(key_char)
            except Exception as e:
                log(f"Error in key release handler ({key}): {e}", 3)

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
        # (events come from the global hook, not from stdin)
        self._reader_thread = threading.Thread(
            target=self._drain_stdin_loop, name='stdin-drain', daemon=True)
        self._reader_thread.start()
        return True

    def _drain_stdin_loop(self):
        """Discard stdin bytes while the pynput backend is active"""
        fd = sys.stdin.fileno()
        while self.running:
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
                if ready and not os.read(fd, 4096):
                    break
            except (OSError, ValueError):
                break

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start keyboard monitoring (detects the best available backend)"""
        if self.running:
            return

        if not sys.stdin.isatty():
            log("stdin is not a terminal: keyboard input unavailable", 3)
            return

        self.running = True
        self._enter_cbreak()

        if self._detect_kitty_protocol():
            self.backend = 'kitty'
            self._push_kitty_flags()
            self._reader_thread = threading.Thread(
                target=self._kitty_read_loop, name='kitty-keys', daemon=True)
            self._reader_thread.start()
        else:
            self.backend = 'pynput'
            if not self._start_pynput():
                self.backend = None

    def stop(self):
        """Stop keyboard monitoring and restore the terminal"""
        self.running = False

        if self.backend == 'kitty':
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
        """cbreak: char-at-a-time input, no echo, Ctrl+C still raises SIGINT"""
        try:
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
        except (termios.error, ValueError, OSError) as e:
            log(f"Could not set terminal cbreak mode: {e}", 3)

    def _leave_cbreak(self):
        """Restore saved terminal attributes and flush pending input"""
        try:
            fd = sys.stdin.fileno()
            if self._saved_term_attrs is not None:
                termios.tcsetattr(fd, termios.TCSANOW, self._saved_term_attrs)
                self._saved_term_attrs = None
            if os.isatty(fd):
                termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, ValueError, OSError) as e:
            log(f"Could not restore terminal mode: {e}", 2)

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

    def get_pressed_keys(self) -> Set[str]:
        """Get currently pressed keys thread-safely"""
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
        return list(self.key_to_note.keys())

    def get_note_name(self, key):
        """Get actual note name for a given key"""
        return self.get_note_for_key(key)

    def print_mappings(self):
        """Print all key mappings"""
        log("\nKey Mappings:", 0)
        for key, actual_note in self.key_to_note.items():
            log(f"  {key:2} -> {actual_note}", 0)

    def set_base_note(self, base_note):
        """Change the base note"""
        self.base_note = base_note
        log(f"Base note changed to {base_note}", 1)


def test_keyboard():
    """Interactive keyboard test"""
    log("Testing keyboard handler...", 1)
    log("Press keys (quit key from config to stop)", 1)

    try:
        handler = KeyboardHandler('config.ini')

        def on_note_on(key, note_name):
            log(f"♪ {key} -> {note_name}", 0)

        def on_note_off(key, note_name):
            log(f"♫ {key} -> {note_name} (off)", 0)

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
