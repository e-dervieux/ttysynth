# TTYSynth: a Python Command Line Audio Synthesizer

[![tests](https://github.com/e-dervieux/ttysynth/actions/workflows/tests.yml/badge.svg)](https://github.com/e-dervieux/ttysynth/actions/workflows/tests.yml)

> ⚠️ Developed and tested on macOS. Linux support is **experimental**: it has
> not been tested on a real Linux system yet (only the automated tests run on
> Linux). See *Known limitations*.

A small real-time polyphonic synthesizer played with your computer keyboard,
with an htop-like full-screen terminal interface.

## Features

- Real-time, low-latency audio synthesis (configurable buffer size: 64 samples,
  i.e. 1.3 ms at 48 kHz, in the shipped `config.ini`)
- Polyphonic: play chords, notes overlap naturally through their release phase
- Two instruments, switchable live:
  - **Flute**: additive synthesis (sine + harmonics) with a soft ADSR envelope
  - **Pluck**: harp / classical guitar-like sound using the dirt-cheap 80s
    [Karplus-Strong algorithm](https://en.wikipedia.org/wiki/Karplus%E2%80%93Strong_string_synthesis)
- Four temperaments, switchable live: equal (modern), Pythagorean,
  quarter-comma meantone and Werckmeister I (III) "correct temperament"
  (see `synth/music.py` for how each distributes the Pythagorean comma)
- Full-screen TUI (alternate screen buffer, like htop) showing sample rate,
  buffer size, output latency (as reported by PortAudio), xrun count, volume,
  held notes (also highlighted in the key map) and a scrolling log panel. It
  adapts to the window size; the minimum depends on the width and the layout
  (70×22 for the built-in layouts at 70 columns), and a smaller window shows
  the size needed
- Recording of the output to FLAC files (24-bit, maximum compression), in
  `recordings/` by default
- Everything configurable through `config.ini` (keyboard layout, tuning,
  temperament, control keys)

## Architecture

Two processes, so UI work can never glitch the audio:

- The **main process** handles keyboard input (see *Keyboard input* below) and
  renders the TUI with `rich`.
- The **audio process** owns the PortAudio output stream and synthesizes
  audio directly inside the stream callback.

Only tiny command messages (note on/off, volume, recording, stop) cross the
process boundary; no audio data is ever transferred (recordings are written
by the audio process itself). A low-rate status queue flows back (latency,
`xruns`, voice count, recording duration) to feed the TUI.

## Project layout

```
main.py              entry point (python main.py, or python -m synth)
config.ini           settings (audio, keyboard layout, control keys)
custom_layout.ini    custom note layout (LAYOUT = custom)
synth/
  app.py             Synthesizer: coordinates the components, control actions
  audio_engine.py    audio process (PortAudio stream, synthesis in its callback)
  dsp/               instruments and notes (sound generation, envelopes)
  keyboard_handler.py, keymap.py, input_backends.py   keyboard input and layouts
  music.py           note names, MIDI numbers, frequencies
  tui.py             full-screen terminal interface (rich)
  config.py, errors.py, logging_setup.py
tests/               automated tests (pytest), see For developers
manual_tests.py      interactive checks with the real audio device and keyboard
.github/workflows/   continuous integration (automated tests on Linux and macOS)
```

## Installation

Requires Python >= 3.11.

On Linux, also install the PortAudio library (bundled with the Python
package on macOS and Windows, not on Linux):

```bash
sudo apt install libportaudio2   # Debian / Ubuntu
```

### With pip

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### With uv

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### Keyboard input

A synth needs key *release* events, which a classic tty does not provide.
Two backends are auto-detected at startup (shown in the status bar):

- **Kitty keyboard protocol** (preferred): terminals such as iTerm2 3.5+,
  kitty, WezTerm, Alacritty and Ghostty report key press/release as escape
  codes over stdin. Event-driven, layout-aware (AZERTY/QWERTY/...), no OS
  permissions, works while the terminal is focused.
- **Global hook (pynput)** as a fallback for terminals without the protocol
  (Terminal.app, VS Code/Cursor integrated terminal, ...). On macOS this
  requires the **Input Monitoring** permission, and is blocked while
  **Secure Keyboard Entry** is active in the focused terminal — both
  conditions are detected and displayed in the status bar.

For the best experience, run the synth in a kitty-protocol-capable terminal.

## Usage

```bash
python main.py                    # with config.ini
python main.py --list-devices     # list the audio devices and exit
python main.py --device 2         # output device, by index or (part of its) name
python main.py --min-latency      # lowest output latency the device accepts (see below)
python main.py --config my.ini    # another configuration file
python main.py --record-dir ~/takes  # where recordings go
python main.py --debug            # also show debug messages in the log panel
```

The screen switches to a full-screen interface; your scrollback is restored
when you quit.

### Latency

The `latency` field of the status bar is the output latency reported by the
audio device; the synthesizer itself adds about 2 to 3 ms (64-sample buffer,
key handling). Most of it depends on the output:

- built-in MacBook speakers: about 19 ms, whatever the settings (macOS
  processes their sound before playing it)
- wired headphones, USB audio interfaces: much lower (a few ms for an
  interface)
- Bluetooth headphones: 150 ms or more, unusable for playing

By default, the stream uses the device's default low latency.
`--min-latency` requests the lowest one the device accepts instead (e.g.
1.3 ms instead of 2.7 ms on some devices; no change on the built-in
speakers): less delay, but less margin against dropouts. Watch the `xruns`
count of the status bar. If the device refuses it, the default is used.

### Default controls (see `config.ini`)

- Notes: C4 to E5, laid out like a piano keyboard (natural notes on the
  home row, sharps above): `a` = C4, `w` = C#4, `s` = D4, ... with the
  default QWERTY layout, `q` = C4, `z` = C#4, `s` = D4, ... with AZERTY
- `+` / `-`: volume up / down
- `tab`: switch instrument (flute / pluck)
- `v`: switch layout (azerty / qwerty / custom)
- `c`: switch temperament (equal / pythagorean / meantone / werckmeister)
- `(` / `)`: octave down / up
- `b` / `n`: start / stop recording (shown as `● REC` in the status bar)
- `esc`: quit

### Configuration

Edit `config.ini` to change the output device (system default unless set;
`--device` overrides it), sample rate, buffer size, tuning frequency (A4),
temperament, recording directory (`--record-dir` overrides it), initial
keyboard layout (`azerty`, `qwerty` or `custom`), octave shift and control
keys.

With `LAYOUT = custom`, notes are mapped to keys in `custom_layout.ini`:
any note to any character key, sharps or flats. The file explains its
format. Mappings are checked at startup: an invalid note or key, or a key
used twice (by notes or controls), is an error.

## For developers

### Development install

On top of the installation above, install the development tools (pytest):

```bash
python -m pip install -r requirements-dev.txt   # or: uv pip install -r requirements-dev.txt
```

### Automated tests

```bash
python -m pytest              # all tests (a few seconds)
python -m pytest -q           # quieter
python -m pytest tests/test_music.py -k werckmeister  # one file, tests matching a name
```

They also run on GitHub (Linux and macOS, Python 3.11, 3.12 and 3.14) on every
push and pull request, see `.github/workflows/tests.yml`.

`python -m pytest` runs pytest with the Python of the active environment,
even when another `pytest` (e.g. from a base conda installation) comes first
on the `PATH`.

The tests need no audio device, keyboard or terminal: a fake `sounddevice`
module (`tests/fakes/sounddevice.py`) stands in for the audio device, also
in the audio processes the tests start, and simulates its failures (stream
that cannot be opened, device lost, crashes...). They cover note names and
temperaments, layouts and configuration validation, the keyboard input
parsing, the musical keyboard, the envelope and instruments (including the
Karplus-Strong pitch), the audio process lifecycle and failure reporting,
recording, the application (control actions, Ctrl+C, crashed threads, exit
codes, command line options) and the TUI layout at various window sizes.

### Manual tests

What only an ear and a real keyboard can check (actual sound, key capture
in your terminal) is in an interactive menu:

```bash
python manual_tests.py      # menu
python manual_tests.py 3    # one test (here: a tone on the audio device)
python manual_tests.py 7    # note-on latency while the TUI renders
```

## Known limitations

- Linux: partially supported. Keyboard input works in terminals supporting the
  kitty keyboard protocol (kitty, foot, WezTerm, Alacritty, Ghostty); the pynput
  fallback only works under X11, not Wayland. PortAudio must be installed
  (see *Installation*).
- Windows: not supported (the terminal handling relies on termios, Unix only).
- Losing the output device (e.g. unplugging headphones) stops the synthesizer
  with an error: it does not reconnect, restart it.

## License

MIT, see [LICENSE](LICENSE).
