"""
Simple Python Command Line Audio Synthesizer
Real-time audio synthesis using keyboard input.

Process layout: this (main) process handles keyboard events and the TUI;
the audio process (spawned by AudioEngine) owns the audio stream and
synthesizes sound in its callback. See audio_engine.py for details.
"""

# Global imports
import logging
import sys
import threading
import time

# Local imports
from config import Config, load_config
from music import MusicEngine, midi_to_note
from keyboard_handler import KeyboardHandler
from audio_engine import AudioEngine
from classes import INSTRUMENTS
from errors import SynthError
from logging_setup import setup_logging
from tui import SynthTUI

log = logging.getLogger(__name__)


class Synthesizer:
    """Main synthesizer class that coordinates all components"""

    def __init__(self, config: Config):
        """Initialize the synthesizer (raises ConfigError on an invalid
        keyboard configuration)"""
        self.running = False
        self._stopped = False
        self._thread_error = None  # First exception of a crashed background thread

        self.music_engine = MusicEngine(config.audio.tuning_freq)
        self.keyboard_handler = KeyboardHandler(config.keyboard, config.controls,
                                                config.audio.default_volume)
        self.audio_engine = AudioEngine(config.audio)

        # Instrument selection (cycled with the instrument-next control key)
        self.instrument_names = list(INSTRUMENTS.keys())
        self.instrument_index = 0

        self.keyboard_handler.set_callbacks(
            on_note_on=self.on_note_on,
            on_note_off=self.on_note_off,
            on_volume_change=self.on_volume_change,
            on_quit=self.on_quit,
            on_record_start=self.on_record_start,
            on_record_stop=self.on_record_stop,
            on_instrument_next=self.on_instrument_next
        )

        log.debug("Synthesizer initialized successfully")

    @property
    def current_instrument(self) -> str:
        """Name of the currently selected instrument"""
        return self.instrument_names[self.instrument_index]

    def on_note_on(self, key, midi):
        """Handle note on events"""
        frequency = self.music_engine.midi_to_frequency(midi)
        self.audio_engine.play_note(key, frequency, instrument_type=self.current_instrument)
        log.debug(f"♪ {key} -> {midi_to_note(midi)} ({frequency:.1f} Hz)")

    def on_note_off(self, key, midi):
        """Handle note off events"""
        self.audio_engine.stop_note(key)
        log.debug(f"♫ {key} -> {midi_to_note(midi)} (off)")

    def on_volume_change(self, volume):
        """Handle volume change events"""
        self.audio_engine.set_volume(volume)
        log.info(f"🔊 Volume: {volume*100:.0f}%")

    def on_instrument_next(self):
        """Cycle to the next instrument"""
        self.instrument_index = (self.instrument_index + 1) % len(self.instrument_names)
        log.info(f"🎹 Instrument: {self.current_instrument}")

    def on_quit(self):
        """Handle quit events - only flags the shutdown, the main thread
        performs the actual stop sequence (avoids stopping components from
        within the keyboard listener thread)"""
        log.info("Quit requested - shutting down...")
        self.running = False

    def on_record_start(self):
        """Handle record start events"""
        log.info("🔴 Recording started")
        # TODO: Implement recording functionality
        log.warning("Recording not implemented")

    def on_record_stop(self):
        """Handle record stop events"""
        log.info("⬜ Recording stopped")
        # TODO: Implement recording functionality
        log.warning("Recording not implemented")

    def start(self):
        """Start the synthesizer (raises SynthError if a component cannot
        start; see run() for the cleanup)"""
        log.debug("Starting synthesizer...")
        self.running = True
        self.audio_engine.start()
        self.keyboard_handler.start()
        log.debug("✅ Synthesizer started successfully!")

    def stop(self):
        """Stop the synthesizer"""
        if self._stopped:
            return
        self._stopped = True
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

    def run(self, use_tui=True):
        """
        Run the synthesizer (blocking). Components are always stopped on
        exit, including when starting one of them failed.

        Raises:
            SynthError: expected failure (see errors.py)
            Exception: the exception of a crashed background thread, if any
        """
        previous_hook = threading.excepthook
        threading.excepthook = self._on_thread_crash
        try:
            self.start()
            if use_tui:
                SynthTUI(self).run()
            else:
                while self.running:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            # Ctrl+C is a normal way to quit
            log.info("Interrupted (Ctrl+C)")
        finally:
            self.stop()
            threading.excepthook = previous_hook

        if self._thread_error is not None:
            raise self._thread_error


def main() -> int:
    """Main function, returns the process exit code"""
    setup_logging(logging.INFO)
    log.info("Python Command Line Audio Synthesizer")
    log.info("=====================================")

    try:
        Synthesizer(load_config()).run()
    except SynthError as e:
        # Expected failure (configuration, audio device, keyboard input):
        # a message is enough. Other exceptions are bugs: keep the traceback.
        log.error(str(e))
        return 1

    log.info("Synthesizer exited successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
