"""
Application: the Synthesizer, which coordinates the components (keyboard
input, audio engine, TUI) and maps the control keys to their actions, and
main(), the entry point.

Process layout: this (main) process handles keyboard events and the TUI;
the audio process (spawned by AudioEngine) owns the audio stream and
synthesizes sound in its callback. See audio_engine.py for details.
"""

# Global imports
import argparse
import logging
import signal
import threading
from dataclasses import replace
from pathlib import Path

# Local imports
from synth.config import CONTROLS, DEFAULT_CONFIG_PATH, Config, load_config, parse_device
from synth.music import MusicEngine, midi_to_note
from synth.keyboard_handler import KeyboardHandler
from synth.audio_engine import AudioEngine, list_devices
from synth.dsp import INSTRUMENTS
from synth.errors import SynthError
from synth.logging_setup import setup_logging
from synth.tui import SynthTUI

log = logging.getLogger(__name__)

# Volume change of one press of the volume keys
VOLUME_STEP = 0.05


class Synthesizer:
    """Main synthesizer class that coordinates all components"""

    def __init__(self, config: Config):
        """Initialize the synthesizer (raises ConfigError on an invalid
        keyboard configuration)"""
        self.running = False
        self._thread_error = None  # First exception of a crashed background thread

        self.music_engine = MusicEngine(config.audio.tuning_freq, config.audio.temperament)
        self.audio_engine = AudioEngine(config.audio)
        self.keyboard_handler = KeyboardHandler(config.keyboard, config.controls,
                                                on_note=self.on_note, on_control=self.on_control)

        # Instrument selection (cycled with the instrument-next control key)
        self.instrument_names = list(INSTRUMENTS.keys())
        self.instrument_index = 0

        # What each control does (see config.CONTROLS)
        self.actions = {
            'volume_up': lambda: self.audio_engine.change_volume(+VOLUME_STEP),
            'volume_down': lambda: self.audio_engine.change_volume(-VOLUME_STEP),
            'instrument_next': self.next_instrument,
            'layout_next': self.keyboard_handler.next_layout,
            'temperament_next': self.music_engine.next_temperament,
            'octave_down': lambda: self.keyboard_handler.shift_octave(-1),
            'octave_up': lambda: self.keyboard_handler.shift_octave(+1),
            'record_start': self.audio_engine.start_recording,
            'record_stop': self.audio_engine.stop_recording,
            'quit': self.on_quit,
        }
        assert self.actions.keys() == CONTROLS.keys(), "every control needs exactly one action"

        log.debug("Synthesizer initialized successfully")

    @property
    def current_instrument(self) -> str:
        """Name of the currently selected instrument"""
        return self.instrument_names[self.instrument_index]

    def on_note(self, key: str, midi: int, pressed: bool):
        """A note key was pressed (note on) or released (note off)"""
        if pressed:
            frequency = self.music_engine.midi_to_frequency(midi)
            self.audio_engine.play_note(key, frequency, instrument_type=self.current_instrument)
            log.debug(f"♪ {key} -> {midi_to_note(midi)} ({frequency:.1f} Hz)")
        else:
            self.audio_engine.stop_note(key)
            log.debug(f"♫ {key} -> {midi_to_note(midi)} (off)")

    def on_control(self, name: str):
        """A control key was pressed"""
        self.actions[name]()

    def next_instrument(self):
        """Cycle to the next instrument"""
        self.instrument_index = (self.instrument_index + 1) % len(self.instrument_names)
        log.info(f"🎹 Instrument: {self.current_instrument}")

    def on_quit(self):
        """Handle quit events - only flags the shutdown, the main thread
        performs the actual stop sequence (avoids stopping components from
        within the keyboard listener thread)"""
        log.info("Quit requested - shutting down...")
        self.running = False

    def start(self):
        """Start the synthesizer (raises SynthError if a component cannot
        start; see run() for the cleanup)"""
        log.debug("Starting synthesizer...")
        self.audio_engine.start()
        self.keyboard_handler.start()
        log.debug("✅ Synthesizer started successfully!")

    def stop(self):
        """Stop the synthesizer"""
        self.running = False

        log.debug("Stopping synthesizer...")
        try:
            self.keyboard_handler.stop()
            log.debug("Keyboard handler stopped")
        finally:
            # Even if the keyboard cleanup failed: a still running audio
            # process would block the interpreter exit
            self.audio_engine.stop()
            log.debug("Audio engine stopped")
        log.debug("Synthesizer stopped")

    def _on_thread_crash(self, args):
        """
        threading.excepthook: a background thread died (keyboard input,
        audio status...). Keep the first exception, re-raised by run() once
        everything is stopped, and request that shutdown: with input dead,
        the quit keys may be dead as well.
        """
        if self._thread_error is None:
            self._thread_error = args.exc_value
            thread_name = args.thread.name if args.thread is not None else '?'
            # Debug only: main() reports the error itself after the shutdown
            log.debug(f"Thread '{thread_name}' failed, shutting down: {args.exc_value!r}")
        self.running = False

    def _on_sigint(self, signum, frame):
        """
        SIGINT handler (Ctrl+C): request the quit, like the quit key. Unlike
        the default KeyboardInterrupt, which could strike anywhere, cleanup
        included (repeated Ctrl+C left the terminal unrestored), the shutdown
        then always runs to the end.
        """
        if self.running:
            log.info("Interrupted (Ctrl+C)")
            self.running = False

    def run(self):
        """
        Run the synthesizer (blocking). Components are always stopped on
        exit, including when starting one of them failed.

        Raises:
            SynthError: expected failure (see errors.py)
            Exception: the exception of a crashed background thread, if any
        """
        # Set before the SIGINT handler: a Ctrl+C during the startup must not
        # be overwritten by it (the TUI then exits at once)
        self.running = True
        previous_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        previous_hook = threading.excepthook
        threading.excepthook = self._on_thread_crash
        try:
            self.start()
            SynthTUI(self).run()
        finally:
            self.stop()
            threading.excepthook = previous_hook
            signal.signal(signal.SIGINT, previous_sigint)

        if self._thread_error is not None:
            raise self._thread_error


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command line options (argv: sys.argv[1:] if None)"""
    parser = argparse.ArgumentParser(prog='python main.py',
                                     description="Python Command Line Audio Synthesizer")
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG_PATH,
                        help="configuration file (default: config.ini of the project)")
    parser.add_argument('--device', type=parse_device,
                        help="output device, by index or (part of its) name; "
                             "overrides [AUDIO] DEVICE")
    parser.add_argument('--record-dir', type=Path,
                        help="directory of the recordings; overrides [AUDIO] RECORD_DIR")
    parser.add_argument('--list-devices', action='store_true',
                        help="list the audio devices and exit")
    parser.add_argument('--debug', action='store_true', help="show debug messages")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main function, returns the process exit code"""
    args = parse_args(argv)
    if args.list_devices:
        print(list_devices())
        return 0

    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    log.info("Python Command Line Audio Synthesizer")
    log.info("=====================================")

    try:
        config = load_config(args.config)
        if args.device is not None:
            config = replace(config, audio=replace(config.audio, device=args.device))
        if args.record_dir is not None:
            config = replace(config, audio=replace(config.audio, record_dir=args.record_dir))
        Synthesizer(config).run()
    except SynthError as e:
        # Expected failure (configuration, audio device, keyboard input):
        # a message is enough. Other exceptions are bugs: keep the traceback.
        log.error(str(e))
        return 1

    log.info("Synthesizer exited successfully")
    return 0

