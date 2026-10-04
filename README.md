# Python Command Line Audio Synthesizer

A small real-time polyphonic synthesizer played with your computer keyboard,
with an htop-like full-screen terminal interface.

## Features

- Real-time, low-latency audio synthesis (48 kHz, 128-sample buffers by default)
- Polyphonic: play chords, notes overlap naturally through their release phase
- Two instruments, switchable live:
  - **Flute**: additive synthesis (sine + harmonics) with a soft ADSR envelope
  - **Pluck**: harp / classical guitar-like sound using the dirt-cheap 80s
    [Karplus-Strong algorithm](https://en.wikipedia.org/wiki/Karplus%E2%80%93Strong_string_synthesis)
- Full-screen TUI (alternate screen buffer, like htop) showing sample rate,
  buffer size, measured latency, xrun count, volume, active notes and a
  scrolling log panel
- Everything configurable through `config.ini` (key mapping, tuning, controls)

## Architecture

Two processes, so UI work can never glitch the audio:

- The **main process** handles keyboard input from stdin and renders the TUI with `rich`.
- The **audio process** owns the PortAudio output stream and synthesizes
  audio directly inside the stream callback.

Only tiny command messages (note on/off, volume, stop) cross the process
boundary; no audio data is ever transferred. A low-rate status queue flows
back (latency, xruns, voice count) to feed the TUI.

## Installation

Requires Python >= 3.11.

### With pip

```bash
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
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
python main.py
```

The screen switches to a full-screen interface; your scrollback is restored
when you quit.

### Default controls (see `config.ini`)

- Notes: two chromatic octaves starting at C4, laid out on letter keys
  (`q` = C4, `z` = C#4, `s` = D4, ... AZERTY-friendly layout)
- `+` / `-`: volume up / down
- `tab`: switch instrument (flute / pluck)
- `w` / `x`: start / stop recording (not implemented yet)
- `esc`: quit

### Configuration

Edit `config.ini` to change the sample rate, buffer size, tuning frequency
(A4), base note, key-to-note mapping and control keys.

## Tests

An interactive test suite covers the music engine, keyboard handler and
audio engine:

```bash
python test_suite.py        # interactive menu
python test_suite.py 1      # run a specific test
```

## Known limitations

- Linux: stdin keyboard input works in any normal terminal.
- Windows: not tested yet; terminal cbreak handling would need a small platform guard.
- Recording is not implemented yet.
