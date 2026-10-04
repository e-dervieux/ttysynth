"""Keyboard input backends: kitty parser, pynput callbacks, terminal restore
(synth/input_backends.py)"""

import enum
import os
import pty
import select
import signal
import subprocess
import sys
import time
import types

import pytest

from synth.input_backends import KittyInput, PynputInput


# ----------------------------------------------------------------------
# Kitty keyboard protocol
# ----------------------------------------------------------------------

def press(code, mods='', text=None):
    """CSI u press event (mods: kitty modifiers field, 1 + bitmask)"""
    return f"\x1b[{code};{mods}{f';{text}' if text else ''}u"


def release(code, mods='1'):
    return f"\x1b[{code};{mods}:3u"


SHIFT, CTRL, ALT, CAPS_LOCK = '2', '5', '3', '65'


@pytest.fixture
def kitty():
    """Kitty backend recording its (key, pressed) events in .events"""
    events = []
    backend = KittyInput(lambda key, pressed: events.append((key, pressed)), None)
    backend.events = events
    return backend


def test_press_and_release(kitty):
    kitty._consume_input(press(97) + release(97))
    assert kitty.events == [('a', True), ('a', False)]


@pytest.mark.parametrize('code, name', [(27, 'esc'), (9, 'tab'), (13, 'enter'), (32, 'space')])
def test_named_keys(kitty, code, name):
    kitty._consume_input(press(code) + release(code))
    assert kitty.events == [(name, True), (name, False)]


@pytest.mark.parametrize('mods', [CTRL, ALT])
def test_shortcuts_are_ignored(kitty, mods):
    kitty._consume_input(press(113, mods) + release(113, mods))
    assert kitty.events == []


def test_ctrl_c_raises_sigint(kitty):
    received = []
    previous = signal.signal(signal.SIGINT, lambda *args: received.append('SIGINT'))
    try:
        kitty._consume_input(press(99, CTRL))
    finally:
        signal.signal(signal.SIGINT, previous)
    assert received == ['SIGINT'] and kitty.events == []


def test_shifted_symbol_is_released_under_its_press_name(kitty):
    """Shift+= gives '+'; the release (no text) still reports '+'"""
    kitty._consume_input(press(61, SHIFT, text=43) + release(61))
    assert kitty.events == [('+', True), ('+', False)]


def test_release_with_a_modifier_held_is_not_filtered(kitty):
    kitty._consume_input(press(113) + release(113, CTRL))
    assert kitty.events == [('q', True), ('q', False)]


def test_caps_lock_does_not_block_keys(kitty):
    kitty._consume_input(press(113, CAPS_LOCK, text=81) + release(113, CAPS_LOCK))
    assert kitty.events == [('q', True), ('q', False)]


def test_auto_repeat_is_ignored(kitty):
    kitty._consume_input(press(97) + '\x1b[97;1:2u' + press(97) + release(97))
    assert kitty.events == [('a', True), ('a', False)]


def test_focus_loss_releases_held_keys(kitty):
    kitty._consume_input(press(97) + press(98) + '\x1b[O')
    assert kitty.events == [('a', True), ('b', True), ('a', False), ('b', False)]
    kitty._consume_input('\x1b[I' + release(97))  # Focus back, late release: nothing
    assert len(kitty.events) == 4


def test_functional_keys_are_ignored(kitty):
    kitty._consume_input(press(57441) + release(57441))  # Left Shift alone
    assert kitty.events == []


@pytest.mark.parametrize('sequence, expected', [
    ('\x1b[;5u', []),                                            # Empty keycode
    ('\x1b[98;1:3:9u', [('b', True)]),                           # Extra event subfield
    ('\x1b[99;;99999999u' + release(99), [('c', True), ('c', False)]),  # Text beyond Unicode
    ('\x1b[100;;u' + release(100), [('d', True), ('d', False)]),         # Empty text field
])
def test_malformed_sequences(kitty, sequence, expected):
    kitty._consume_input(sequence)
    assert kitty.events == expected


def test_incomplete_sequence_waits_for_more_input(kitty):
    rest = kitty._consume_input('\x1b[97')
    assert rest == '\x1b[97' and kitty.events == []
    kitty._consume_input(rest + 'u')
    assert kitty.events == [('a', True)]


def test_unparseable_sequence_is_skipped(kitty):
    assert kitty._consume_input('\x1b' + 'x' * 20 + press(97)) == ''
    assert kitty.events == [('a', True)]


# ----------------------------------------------------------------------
# pynput (with a fake pynput.keyboard module)
# ----------------------------------------------------------------------

class Key(enum.Enum):
    ctrl, ctrl_l, ctrl_r = 'ctrl', 'ctrl_l', 'ctrl_r'
    alt, alt_l, alt_r = 'alt', 'alt_l', 'alt_r'
    cmd, cmd_l, cmd_r = 'cmd', 'cmd_l', 'cmd_r'
    shift, esc, tab, space, enter = 'shift', 'esc', 'tab', 'space', 'enter'


class KeyCode:
    def __init__(self, vk, char):
        self.vk, self.char = vk, char


FAKE_PK = types.SimpleNamespace(Key=Key, KeyCode=KeyCode)
Q_VK = 12  # macOS virtual key code of the Q key


@pytest.fixture
def pynput():
    events = []
    backend = PynputInput(lambda key, pressed: events.append((key, pressed)), None)
    backend._setup_keys(FAKE_PK)
    backend.events = events
    return backend


def test_pynput_release_with_ctrl_held(pynput):
    """The release under Ctrl produces a control character, same physical key"""
    pynput._on_press(KeyCode(Q_VK, 'q'))
    pynput._on_press(Key.ctrl)
    pynput._on_release(KeyCode(Q_VK, '\x11'))
    pynput._on_release(Key.ctrl)
    assert pynput.events == [('q', True), ('q', False)]


@pytest.mark.parametrize('modifier', [Key.ctrl, Key.alt_l, Key.cmd])
def test_pynput_shortcuts_are_ignored(pynput, modifier):
    pynput._on_press(modifier)
    pynput._on_press(KeyCode(Q_VK, 'q'))
    pynput._on_release(KeyCode(Q_VK, 'q'))
    pynput._on_release(modifier)
    assert pynput.events == []


def test_pynput_shift_is_allowed(pynput):
    pynput._on_press(Key.shift)
    pynput._on_press(KeyCode(Q_VK, 'Q'))
    pynput._on_release(Key.shift)
    pynput._on_release(KeyCode(Q_VK, 'q'))
    assert ('q', True) in pynput.events and ('q', False) in pynput.events


def test_pynput_virtual_key_code_zero(pynput):
    """vk 0 is the A key on macOS: not to be mistaken for 'no vk'"""
    pynput._on_press(KeyCode(0, 'a'))
    pynput._on_release(KeyCode(0, 'A'))
    assert pynput.events == [('a', True), ('a', False)]


def test_pynput_named_keys(pynput):
    pynput._on_press(Key.tab)
    pynput._on_release(Key.tab)
    assert pynput.events == [('tab', True), ('tab', False)]


# ----------------------------------------------------------------------
# Terminal restored when the startup fails (in a pseudo-terminal)
# ----------------------------------------------------------------------

STARTUP_FAILURE_SCRIPT = '''
import sys, termios
from synth import input_backends
from synth.errors import SynthError

input_backends._write_term = lambda seq: None
before = termios.tcgetattr(0)
case = sys.argv[1]
if case == 'ctrl-c':
    def detect():
        raise KeyboardInterrupt
    input_backends._detect_kitty_protocol = detect
elif case == 'bug':
    input_backends._detect_kitty_protocol = lambda: True
    def start(self):
        raise RuntimeError('bug')
    input_backends.KittyInput.start = start
elif case == 'no-pynput':
    input_backends._detect_kitty_protocol = lambda: False
    sys.modules['pynput'] = None  # import pynput raises ImportError
try:
    input_backends.start_input(lambda key, pressed: None)
except (KeyboardInterrupt, RuntimeError, SynthError) as e:
    print(type(e).__name__, 'restored' if termios.tcgetattr(0) == before else 'NOT restored')
'''


def run_in_pty(*args, timeout=15.0) -> str:
    """
    Run python with args, its stdin / stdout / stderr on a pseudo-terminal,
    and return its output. Started with subprocess rather than pty.fork():
    forking this multi-threaded process (threads left by other tests) from
    Python risks deadlocks in the child, and Python >= 3.12 warns about it.
    """
    master, slave = pty.openpty()
    # Same import path as the test session (synth package, fakes)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(path for path in sys.path if path))
    process = subprocess.Popen([sys.executable, *args], stdin=slave, stdout=slave, stderr=slave,
                               env=env, start_new_session=True)
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:  # EIO: the child closed the terminal (Linux)
                    break
                if not data:
                    break
                output += data
            elif process.poll() is not None:
                break
        process.wait(timeout=5)
    finally:
        os.close(master)
    return output.decode(errors='replace')


@pytest.mark.parametrize('case, error', [
    ('ctrl-c', 'KeyboardInterrupt'), ('bug', 'RuntimeError'), ('no-pynput', 'SynthError')])
def test_terminal_restored_when_startup_fails(case, error):
    output = run_in_pty('-c', STARTUP_FAILURE_SCRIPT, case)
    assert f'{error} restored' in output, output
