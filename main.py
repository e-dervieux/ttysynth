"""
Simple Python Command Line Audio Synthesizer
Real-time audio synthesis using keyboard input.

Process layout: this (main) process handles keyboard events and the TUI;
the audio process (spawned by AudioEngine) owns the audio stream and
synthesizes sound in its callback. See audio_engine.py for details.
"""

# Global imports
import time

# Local imports
from music import MusicEngine, midi_to_note
from keyboard_handler import KeyboardHandler
from audio_engine import AudioEngine
from classes import INSTRUMENTS
from logger import log
from tui import SynthTUI


class Synthesizer:
    """Main synthesizer class that coordinates all components"""

    def __init__(self, config_file='config.ini'):
        """Initialize the synthesizer"""
        self.running = False
        self._stopped = False

        # Initialize components with config file
        self.music_engine = MusicEngine(config_file=config_file)
        self.keyboard_handler = KeyboardHandler(config_file=config_file)
        self.audio_engine = AudioEngine(config_file=config_file)

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

        log("Synthesizer initialized successfully", 0)

    @property
    def current_instrument(self) -> str:
        """Name of the currently selected instrument"""
        return self.instrument_names[self.instrument_index]

    def on_note_on(self, key, midi):
        """Handle note on events"""
        frequency = self.music_engine.midi_to_frequency(midi)
        self.audio_engine.play_note(key, frequency, instrument_type=self.current_instrument)
        log(f"♪ {key} -> {midi_to_note(midi)} ({frequency:.1f} Hz)", 0)

    def on_note_off(self, key, midi):
        """Handle note off events"""
        self.audio_engine.stop_note(key)
        log(f"♫ {key} -> {midi_to_note(midi)} (off)", 0)

    def on_volume_change(self, volume):
        """Handle volume change events"""
        self.audio_engine.set_volume(volume)
        log(f"🔊 Volume: {volume*100:.0f}%", 1)

    def on_instrument_next(self):
        """Cycle to the next instrument"""
        self.instrument_index = (self.instrument_index + 1) % len(self.instrument_names)
        log(f"🎹 Instrument: {self.current_instrument}", 1)

    def on_quit(self):
        """Handle quit events - only flags the shutdown, the main thread
        performs the actual stop sequence (avoids stopping components from
        within the keyboard listener thread)"""
        log("Quit requested - shutting down...", 1)
        self.running = False

    def on_record_start(self):
        """Handle record start events"""
        log("🔴 Recording started", 1)
        # TODO: Implement recording functionality
        log("Recording not implemented", 2)

    def on_record_stop(self):
        """Handle record stop events"""
        log("⬜ Recording stopped", 1)
        # TODO: Implement recording functionality
        log("Recording not implemented", 2)

    def start(self):
        """Start the synthesizer"""
        if self.running:
            return True

        log("Starting synthesizer...", 0)
        self.running = True

        # Start audio engine
        if not self.audio_engine.start():
            log("Failed to start audio engine", 3)
            self.running = False
            return False

        # Start keyboard handler
        self.keyboard_handler.start()

        log("✅ Synthesizer started successfully!", 0)

        return True

    def stop(self):
        """Stop the synthesizer"""
        if self._stopped:
            return
        self._stopped = True
        self.running = False

        log("Stopping synthesizer...", 0)
        self.keyboard_handler.stop()
        log("Keyboard handler stopped", 0)
        self.audio_engine.stop()
        log("Audio engine stopped", 0)
        log("Synthesizer stopped", 0)

    def run(self, use_tui=True):
        """Run the synthesizer (blocking)"""
        if not self.start():
            return

        try:
            if use_tui:
                SynthTUI(self).run()
            else:
                while self.running:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            log("Keyboard interrupt received", 2)
        finally:
            self.stop()


def main():
    """Main function"""
    log("Python Command Line Audio Synthesizer", 1)
    log("=====================================", 1)

    synth = Synthesizer()
    synth.run()


if __name__ == "__main__":
    log("Starting synthesizer", level=0, display_level=1)
    main()
    log("Synthesizer exited successfully", 1)
