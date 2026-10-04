"""
Keyboard input backends: everything that talks to the terminal or the OS.

A synthesizer needs key *release* events, which a classic tty does not
deliver. Two backends are therefore used, picked automatically at startup
(see start_input):

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

Backends only report key presses and releases, as normalized key names (the
lowercase character, or esc / tab / enter / space, see keymap.NAMED_KEYS):
what keys do is decided by the keyboard handler.
"""

# Global imports
import codecs
import ctypes
import functools
import logging
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from typing import Callable, Optional

# Local imports
from synth.errors import SynthError

log = logging.getLogger(__name__)

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

# Kitty functional key codes live at U+E000 and above (Unicode Private Use
# Area); they are not characters and must not be chr()'d into key names.
_FUNCTIONAL_KEY_FLOOR = 0xE000

# Kitty key event types
_EVENT_PRESS, _EVENT_REPEAT, _EVENT_RELEASE = 1, 2, 3

# Kitty modifier bits (the modifiers field is sent as 1 + bitmask):
# shift 1, alt 2, ctrl 4, super 8, hyper 16, meta 32, caps lock 64, num lock 128
_MOD_CTRL = 4
# Modifiers turning a key press into a shortcut (no key reported). Shift is
# excluded (needed for '+' on many layouts), as are the lock bits.
_SHORTCUT_MODS = 2 | 4 | 8 | 16 | 32

# Focus reporting (DEC private mode 1004): the terminal sends CSI I / CSI O
# when its window gains / loses focus. Unlike the kitty flags, this mode is
# shared by the main and alternate screens.
_FOCUS_REPORTING_ON, _FOCUS_REPORTING_OFF = '\x1b[?1004h', '\x1b[?1004l'

# Hint shown when the kitty protocol is not available
_KITTY_TERMINALS = "iTerm2 3.5+, kitty, WezTerm, Alacritty, Ghostty"


# ----------------------------------------------------------------------
# Terminal helpers
# ----------------------------------------------------------------------

def _write_term(seq: str):
    """Write an escape sequence directly to the terminal, bypassing any
    stdout redirection (rich's Live redirects sys.stdout)."""
    try:
        os.write(sys.__stdout__.fileno(), seq.encode('ascii'))
    except OSError as e:
        log.warning(f"Could not write to terminal: {e}")


def _enter_cbreak() -> list:
    """cbreak: char-at-a-time input, no echo. The tty still raises SIGINT
    on a raw Ctrl+C byte; the kitty protocol encodes Ctrl+C as an escape
    code instead, handled in KittyInput. Returns the attributes to restore."""
    fd = sys.stdin.fileno()
    saved_attrs = termios.tcgetattr(fd)
    # TCSANOW: apply immediately. The default TCSAFLUSH would wait for
    # pending terminal output to drain, which can block.
    tty.setcbreak(fd, termios.TCSANOW)
    # Belt and braces: ensure echo is off (setcbreak behavior around ECHO
    # changed across Python versions)
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return saved_attrs


def _leave_cbreak(saved_attrs: list):
    """Restore the terminal attributes and flush pending input (best effort:
    a failure is logged, the rest of the shutdown must go on)"""
    fd = sys.stdin.fileno()
    try:
        termios.tcsetattr(fd, termios.TCSANOW, saved_attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError) as e:
        log.warning(f"Could not restore terminal mode: {e}")


def _detect_kitty_protocol() -> bool:
    """
    Query kitty keyboard protocol support. We send the protocol query
    (CSI ? u) followed by a primary Device Attributes query (CSI c) that
    virtually every terminal answers: if the DA reply arrives without a
    kitty reply before it, the protocol is not supported. The DA reply
    bounds the wait so unsupporting terminals don't cost the full timeout.
    """
    fd = sys.stdin.fileno()
    _write_term('\x1b[?u\x1b[c')

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


@functools.cache
def _carbon() -> Optional[ctypes.CDLL]:
    """macOS Carbon framework, loaded on first use only. None if it cannot
    be loaded: logged once, Secure Keyboard Entry detection is then off
    (a mere diagnostic, not worth stopping the synthesizer)."""
    try:
        return ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
    except OSError as e:
        log.warning(f"Secure Keyboard Entry detection unavailable: {e}")
        return None


def _secure_input_active() -> bool:
    """True if macOS Secure Keyboard Entry is currently active (it blocks
    global event taps while the enabling app - usually a terminal - is
    focused). Cheap enough to call from the TUI refresh loop."""
    if sys.platform != 'darwin':
        return False
    carbon = _carbon()
    return carbon is not None and bool(carbon.IsSecureEventInputEnabled())


# ----------------------------------------------------------------------
# Backends
# ----------------------------------------------------------------------

class InputBackend:
    """
    Common base of the backends: held keys and terminal mode.

    Each backend identifies keys physically (kitty keycode, pynput virtual
    key code) and resolves their name at press time. The release is
    reported under that same name, whatever modifiers changed in between
    (Shift turning '=' into '+', Ctrl turning 'q' into a control
    character...): presses and releases always come in pairs, and
    auto-repeats are dropped.
    """

    def __init__(self, on_key: Callable[[str, bool], None], saved_term_attrs: list):
        """
        Args:
            on_key: called with (key name, pressed) from the input thread
            saved_term_attrs: terminal attributes restored by stop()
        """
        self._on_key = on_key
        self._saved_term_attrs = saved_term_attrs
        # Physical key id -> key name resolved at press time. Only ever
        # accessed from the backend's input thread.
        self._held = {}
        self._thread = None
        self.running = False

    def _key_down(self, key_id, key_name: str):
        """Press edge of a physical key (auto-repeats are ignored)"""
        if key_id in self._held:
            return
        self._held[key_id] = key_name
        self._on_key(key_name, True)

    def _key_up(self, key_id):
        """Release edge of a physical key, reported under its press name"""
        key_name = self._held.pop(key_id, None)
        if key_name is not None:
            self._on_key(key_name, False)

    def _release_all(self):
        """Release every held key (their release events will never come)"""
        for key_id in list(self._held):
            self._key_up(key_id)

    def stop(self):
        """Stop the input thread and restore the terminal"""
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        _leave_cbreak(self._saved_term_attrs)
        log.debug("Keyboard input stopped")

    @property
    def status(self) -> str:
        """Health of key capture, for the TUI status bar"""
        if not self.running:
            return 'OFF'
        if self._thread is None or not self._thread.is_alive():
            return 'DEAD'
        return 'OK'

    def on_alt_screen_enter(self):
        """Called after the TUI entered the alternate screen"""

    def on_alt_screen_exit(self):
        """Called before the TUI leaves the alternate screen"""


class KittyInput(InputBackend):
    """Kitty keyboard protocol: key events read from stdin"""

    def start(self):
        self.running = True
        self._push_flags()
        _write_term(_FOCUS_REPORTING_ON)
        self._thread = threading.Thread(target=self._read_loop, name='kitty-keys', daemon=True)
        self._thread.start()
        log.info("Keyboard input: kitty keyboard protocol (stdin)")

    def stop(self):
        if self.running:
            _write_term(_FOCUS_REPORTING_OFF)
            self._pop_flags()
        super().stop()

    @property
    def status(self) -> str:
        status = super().status
        return 'OK (kitty protocol)' if status == 'OK' else status

    @staticmethod
    def _push_flags():
        """Enable the protocol (pushes onto the terminal's flag stack)"""
        _write_term(f'\x1b[>{KITTY_FLAGS}u')

    @staticmethod
    def _pop_flags():
        """Disable the protocol (pops the flag stack; ignored if empty)"""
        _write_term('\x1b[<u')

    def on_alt_screen_enter(self):
        """The main and alternate screens have independent kitty flag
        stacks: enable the protocol on the alternate one too."""
        if self.running:
            self._push_flags()

    def on_alt_screen_exit(self):
        if self.running:
            self._pop_flags()

    @staticmethod
    def _key_from_code(keycode: int, text: str) -> Optional[str]:
        """Resolve a kitty key code (+ optional associated text) to a key
        name. Returns None for keys we don't handle (modifiers, F-keys...)"""
        if keycode in _CODE_NAMES:
            return _CODE_NAMES[keycode]
        if text and text.isprintable():
            return text.lower()
        if keycode >= _FUNCTIONAL_KEY_FLOOR:
            return None
        char = chr(keycode)
        return char.lower() if char.isprintable() else None

    def _dispatch_event(self, params: str):
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
            # Never filtered on modifiers: pressing one while a key is held
            # must not leave that key (and its note) stuck.
            self._key_up(keycode)
            return
        if event != _EVENT_PRESS:
            return  # _EVENT_REPEAT: the key is already held

        if modifiers & _MOD_CTRL and keycode == ord('c'):
            # The protocol sends Ctrl+C as an escape code instead of letting
            # the tty raise SIGINT: raise it ourselves so both backends share
            # the same interrupt path.
            signal.raise_signal(signal.SIGINT)
            return
        if modifiers & _SHORTCUT_MODS:
            return

        key_name = self._key_from_code(keycode, text)
        if key_name is not None:
            self._key_down(keycode, key_name)

    def _consume_input(self, buf: str) -> str:
        """Consume parseable input from buf, return the unparsed remainder"""
        while buf:
            if buf[0] != '\x1b':
                # With 'report all keys as escape codes', key events never
                # come as plain characters: ignore them
                buf = buf[1:]
                continue

            match = _CSI_RE.match(buf)
            if match:
                prefix, params, final = match.groups()
                if final == 'u' and not prefix:
                    self._dispatch_event(params)
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

    def _read_loop(self):
        """Input thread: blocking select on stdin, parse, dispatch. Any
        exception ends the thread and, through threading.excepthook, shuts
        the synthesizer down (see Synthesizer.run)."""
        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        buf = ''
        while self.running:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                continue
            data = os.read(fd, 256)
            if not data:
                raise SynthError("Keyboard input closed")
            buf += decoder.decode(data)
            buf = self._consume_input(buf)


class PynputInput(InputBackend):
    """pynput global keyboard hook (events from the OS, not from stdin)"""

    def __init__(self, on_key, saved_term_attrs):
        super().__init__(on_key, saved_term_attrs)
        self._listener = None
        self._input_monitoring_ok = True
        self._pk = None             # pynput.keyboard module, see _setup_keys
        self._shortcut_mods = set()
        self._held_mods = set()

    def _setup_keys(self, pk):
        """
        pynput does not report modifiers with key events: track the ones
        turning a press into a shortcut (no key reported), as the kitty
        backend does. Shift is excluded (needed for '+' on many layouts),
        and so is AltGr (used to type characters).
        """
        self._pk = pk
        self._shortcut_mods = {pk.Key.ctrl, pk.Key.ctrl_l, pk.Key.ctrl_r,
                               pk.Key.alt, pk.Key.alt_l, pk.Key.alt_r,
                               pk.Key.cmd, pk.Key.cmd_l, pk.Key.cmd_r}
        self._held_mods = set()

    def _on_press(self, key):
        """pynput press callback (listener thread)"""
        if key in self._shortcut_mods:
            self._held_mods.add(key)
        elif not self._held_mods:
            key_name = self._key_name(key, self._pk)
            if key_name:
                self._key_down(self._key_id(key), key_name)

    def _on_release(self, key):
        """pynput release callback (listener thread)"""
        self._held_mods.discard(key)
        self._key_up(self._key_id(key))

    def start(self):
        """Start the global listener and run macOS diagnostics (raises
        SynthError if pynput is unavailable)"""
        try:
            from pynput import keyboard as pk
        except ImportError as e:
            # Not only "not installed": pynput also raises ImportError when
            # it has no usable backend (e.g. Linux without X display)
            raise SynthError(
                f"Keyboard input unavailable: this terminal lacks the kitty keyboard "
                f"protocol, and pynput cannot be used ({e}). Install pynput, or use a "
                f"terminal supporting the protocol ({_KITTY_TERMINALS}).") from e

        self._setup_keys(pk)
        self.running = True
        self._listener = pk.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()
        self._listener.wait()
        log.info(f"Keyboard input: global hook (pynput). For native terminal key handling, "
                 f"use a terminal with the kitty keyboard protocol ({_KITTY_TERMINALS}).")

        # macOS diagnostics: listen-only event taps need Input Monitoring
        # (pynput itself only checks the unrelated Accessibility permission)
        if sys.platform == 'darwin':
            try:
                import Quartz
                if hasattr(Quartz, 'CGPreflightListenEventAccess'):
                    self._input_monitoring_ok = bool(Quartz.CGPreflightListenEventAccess())
                    if not self._input_monitoring_ok:
                        log.error("Missing macOS 'Input Monitoring' permission: enable your "
                                  "terminal in System Settings > Privacy & Security > Input "
                                  "Monitoring, then restart.")
                        Quartz.CGRequestListenEventAccess()
            except ImportError:
                log.warning("Quartz unavailable: cannot check Input Monitoring permission")

            if _secure_input_active():
                log.error("Secure Keyboard Entry is ACTIVE: keys will NOT be captured "
                          "while this terminal is focused. Disable it in your terminal's "
                          "menu (Terminal.app / iTerm2: 'Secure Keyboard Entry').")

        # Drain stdin so typed characters don't pile up in the tty buffer
        # (events come from the global hook, not from stdin); this thread
        # also watches the listener
        self._thread = threading.Thread(target=self._drain_loop, name='stdin-drain', daemon=True)
        self._thread.start()

    def stop(self):
        super().stop()
        if self._listener is not None:
            if self._listener.running:
                self._listener.stop()
            self._listener = None

    @property
    def status(self) -> str:
        status = super().status
        if status != 'OK':
            return status
        if not self._input_monitoring_ok:
            return 'NO PERMISSION'
        if _secure_input_active():
            return 'BLOCKED (secure input)'
        if self._listener is None or not self._listener.is_alive():
            return 'DEAD'
        return 'OK (global hook)'

    @staticmethod
    def _key_id(key):
        """Physical identity of a pynput key: the virtual key code of
        character keys (does not depend on modifiers, unlike key.char),
        or the Key member itself for special keys"""
        vk = getattr(key, 'vk', None)
        return key if vk is None else vk

    @staticmethod
    def _key_name(key, pk) -> Optional[str]:
        """Normalized key name of a pynput key object"""
        if hasattr(key, 'char') and key.char is not None:
            return key.char.lower()
        for name in ('esc', 'tab', 'space', 'enter'):
            if key == getattr(pk.Key, name):
                return name
        return str(key).replace('Key.', '').replace("'", "").lower()

    def _drain_loop(self):
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


def start_input(on_key: Callable[[str, bool], None]) -> InputBackend:
    """
    Start the best available backend: the kitty protocol if the terminal
    supports it, the pynput global hook otherwise.

    Args:
        on_key: called with (key name, pressed) from the input thread

    Raises:
        SynthError: no usable keyboard input
        Any exception raised while starting (Ctrl+C during the detection,
        bug...): the terminal is restored in every case
    """
    if not sys.stdin.isatty():
        raise SynthError("Keyboard input unavailable: stdin is not a terminal")

    saved_term_attrs = _enter_cbreak()
    backend = None
    try:
        backend_class = KittyInput if _detect_kitty_protocol() else PynputInput
        backend = backend_class(on_key, saved_term_attrs)
        backend.start()
    except BaseException:
        # Restore the terminal before propagating, whatever went wrong:
        # otherwise it stays in cbreak mode (no echo, and on some Python
        # versions Enter no longer ends a line) after the program exits
        if backend is not None:
            backend.stop()
        else:
            _leave_cbreak(saved_term_attrs)
        raise
    return backend
