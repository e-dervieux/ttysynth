"""
Manual tests: an interactive menu of checks with the real audio device and
keyboard, to be verified by ear and by hand (python manual_tests.py).
The automated tests are in tests/ (run with pytest, see README.md).

A test passes if it returns, fails if it raises (the traceback is printed),
is skipped if it raises Skipped. Ctrl+C interrupts it: back to the menu, or
end of the run with 'Run All Tests'.
"""

# Global imports
import io
import logging
import random
import re
import sys
import threading
import time
import traceback
import types

import numpy as np
from rich.console import Console

# Local imports
from synth.music import MusicEngine, midi_to_note, note_to_midi
from synth.keyboard_handler import KeyboardHandler
from synth.audio_engine import AudioEngine, list_devices
from synth.app import GIL_SWITCH_INTERVAL, Synthesizer
from synth.config import load_config
from synth.logging_setup import console_paused, setup_logging
from synth.tui import SynthTUI

# After synth.audio_engine, whose import adds the PortAudio install hint to
# the error if the library is missing
import sounddevice as sd

# Test outcomes
PASSED, FAILED, SKIPPED, INTERRUPTED = 'PASSED', 'FAILED', 'SKIPPED', 'INTERRUPTED'
OUTCOME_ICONS = {PASSED: '✅', FAILED: '❌', SKIPPED: '⏩', INTERRUPTED: '⛔'}

# Exit code of a program interrupted by Ctrl+C (128 + SIGINT), as shells do
EXIT_INTERRUPTED = 130


class Skipped(Exception):
    """Raised by a test that did not run (declined by the user...)"""


def play_tone(duration=2.0, frequency=440.0):
    """Basic audio playback"""
    print(f"Playing {frequency}Hz tone for {duration} seconds...")

    sample_rate = 48000
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    wave = 0.3 * np.sin(2 * np.pi * frequency * t)

    sd.play(wave, sample_rate)
    sd.wait()

    print("Basic audio test completed")


def play_stream(duration=3.0):
    """Streaming audio"""
    print(f"Testing streaming audio for {duration} seconds...")

    sample_rate = 48000
    block_size = 128

    def callback(outdata, frames, time_info, status):
        t = np.arange(frames) / sample_rate
        t = t + callback.phase
        wave = 0.2 * np.sin(2 * np.pi * 440 * t)
        outdata[:, 0] = wave
        callback.phase += frames / sample_rate

    callback.phase = 0

    with sd.OutputStream(callback=callback,
                         channels=1,
                         samplerate=sample_rate,
                         blocksize=block_size):
        time.sleep(duration)

    print("Streaming audio test completed")


def test_music_engine():
    """Test 1: Music Engine Test"""
    print("=" * 50)
    print("TEST 1: Music Engine")
    print("=" * 50)

    audio_config = load_config().audio
    music = MusicEngine(audio_config.tuning_freq, audio_config.temperament)

    print("\n✅ Music engine loaded successfully")

    # Test some frequencies
    test_notes = ['C4', 'D4', 'E4', 'C#4', 'D#4']
    print("\n🎵 Testing some note frequencies:")
    for note in test_notes:
        freq = music.midi_to_frequency(note_to_midi(note))
        print(f"  {note} -> {freq:.1f} Hz")

    # Test tuning change
    print("\n🎼 Testing tuning change:")
    print(f"  A4 at 440 Hz: {music.midi_to_frequency(note_to_midi('A4')):.1f} Hz")
    retuned = MusicEngine(442.0, audio_config.temperament)  # Slightly higher tuning
    print(f"  A4 at 442 Hz: {retuned.midi_to_frequency(note_to_midi('A4')):.1f} Hz")


def log_keys():
    """Interactive keyboard test: logs note and control keys until quit"""
    config = load_config()
    done = threading.Event()

    def on_note(key, midi, pressed):
        print(f"{'♪' if pressed else '♫'} {key} -> {midi_to_note(midi)}{'' if pressed else ' (off)'}")

    def on_control(name):
        print(f"Control: {name}")
        if name == 'quit':
            done.set()

    handler = KeyboardHandler(config.keyboard, config.controls, on_note, on_control)
    handler.start()
    try:
        while not done.wait(0.1):
            pass
    finally:
        handler.stop()


def test_keyboard_handler():
    """Test 2: Keyboard Handler Test"""
    print("=" * 50)
    print("TEST 2: Keyboard Handler")
    print("=" * 50)

    print("🎹 Testing keyboard handler initialization...")
    config = load_config()
    keyboard = KeyboardHandler(config.keyboard, config.controls,
                               on_note=lambda *args: None, on_control=lambda name: None)

    print("✅ Keyboard handler created successfully")
    print(f"📝 Monitoring {len(keyboard.key_to_midi)} keys")

    # Interactive test
    print("\n🔍 Interactive keyboard test available")
    choice = input("  Would you like to test keyboard input? (y/n): ").lower().strip()
    if choice != 'y':
        raise Skipped("interactive keyboard test declined")
    print("\n⌨️  Starting interactive keyboard test...")
    print("Press keys (ESC to finish):")
    log_keys()


def test_audio_basic():
    """Test 3: Basic Audio Test"""
    print("=" * 50)
    print("TEST 3: Basic Audio")
    print("=" * 50)

    play_tone(duration=2.0, frequency=440.0)


def test_audio_streaming():
    """Test 4: Streaming Audio Test"""
    print("=" * 50)
    print("TEST 4: Streaming Audio")
    print("=" * 50)

    play_stream(duration=3.0)


def test_audio_engine():
    """Test 5: Audio Engine Test"""
    print("=" * 50)
    print("TEST 5: Audio Engine")
    print("=" * 50)

    print("🔊 Creating audio engine...")
    audio = AudioEngine(load_config().audio)

    print("✅ Audio engine created successfully")

    print("\nAvailable audio devices:")
    print(list_devices())

    print("\n🎵 Testing audio engine with multiple frequencies...")

    try:
        audio.start()  # Raises SynthError on failure

        print("✅ Audio engine started")

        # Test playing notes
        test_freqs = {'C': 261.63, 'E': 329.63, 'G': 392.00}  # C major chord

        print("\n🎶 Playing C major chord (flute) for 2 seconds...")
        for note, freq in test_freqs.items():
            audio.play_note(note, freq, instrument_type='flute')
            print(f"  Added {note}: {freq} Hz")

        time.sleep(2.0)

        print("\n🎵 Releasing notes one by one...")
        for note in test_freqs:
            audio.stop_note(note)
            print(f"  Released {note}")
            time.sleep(0.5)

        print("\n🎸 Playing the same chord with the plucked string...")
        for note, freq in test_freqs.items():
            audio.play_note(note, freq, instrument_type='pluck')
            time.sleep(0.2)

        time.sleep(2.0)
        for note in test_freqs:
            audio.stop_note(note)

        # Test volume
        print("\n🔊 Testing volume changes...")
        audio.play_note('A', 440.0, instrument_type='flute')

        for volume in [0.2, 0.5, 0.8, 0.5]:
            audio.set_volume(volume)
            print(f"  Volume: {volume*100:.0f}%")
            time.sleep(0.5)

        audio.stop_note('A')
        time.sleep(0.5)
    finally:
        audio.stop()  # Also on failure or Ctrl+C: no audio process left behind

    print("✅ Audio engine test completed successfully")


def test_full_integration():
    """Test 6: Full Integration Test"""
    print("=" * 50)
    print("TEST 6: Full Integration")
    print("=" * 50)

    print("🔧 Testing full synthesizer integration...")

    # This would import and test the full synthesizer
    # But we'll do a simpler integration test here

    config = load_config()
    music = MusicEngine(config.audio.tuning_freq, config.audio.temperament)
    audio = AudioEngine(config.audio)

    def on_note(key, midi, pressed):
        if pressed:
            freq = music.midi_to_frequency(midi)
            audio.play_note(key, freq)
            print(f"♪ {key} -> {midi_to_note(midi)} ({freq:.1f} Hz)")
        else:
            audio.stop_note(key)
            print(f"♫ {key} -> {midi_to_note(midi)} (off)")

    KeyboardHandler(config.keyboard, config.controls, on_note=on_note,
                    on_control=lambda name: None)

    print("✅ All components initialized, callbacks configured")

    try:
        audio.start()  # Raises SynthError on failure
        print("✅ Integration test setup completed")
        print("📋 Would test keyboard integration, but that requires interactive input")
    finally:
        audio.stop()


def test_note_latency(notes=300):
    """Test 7: note-on latency while the TUI renders"""
    print("=" * 50)
    print("TEST 7: Note-on latency while the TUI renders")
    print("=" * 50)
    print("Delay from a note key (main process) to the note starting in the audio")
    print("process, while the TUI renders nonstop (off-screen): with Python's")
    print(f"default GIL switch interval, then with the synthesizer's ({GIL_SWITCH_INTERVAL * 1000} ms).")

    started = {}  # Key name -> time the audio process started its note

    class NoteStarts(logging.Handler):
        def emit(self, record):
            match = re.match(r"Audio process: note (\S+) started", record.getMessage())
            if match:
                started[match.group(1)] = record.created  # time.time() in the audio process

    synth = Synthesizer(load_config())
    synth.keyboard_handler.backend = types.SimpleNamespace(status='OK')
    tui = SynthTUI(synth)
    console = Console(width=120, height=40, file=io.StringIO(), force_terminal=True)
    handler = NoteStarts()
    logging.getLogger().addHandler(handler)
    default_interval = sys.getswitchinterval()
    try:
        with console_paused():  # 2 x 300 notes of debug logs
            synth.audio_engine.start()
            for interval in (default_interval, GIL_SWITCH_INTERVAL):
                sys.setswitchinterval(interval)
                started.clear()
                sent = {}
                done = threading.Event()

                def play():
                    """Like the keyboard input thread"""
                    for i in range(notes):
                        time.sleep(random.uniform(0.005, 0.03))
                        key = f"k{i}"
                        sent[key] = time.time()
                        synth.on_note(key, 60, True)
                        synth.on_note(key, 60, False)
                    time.sleep(0.5)  # Last notes reach the audio process
                    done.set()

                player = threading.Thread(target=play)
                player.start()
                while not done.is_set():
                    console.print(tui._render(console))
                player.join()

                delays = np.array([started[key] - sent[key] for key in sent if key in started]) * 1000
                print(f"  switch interval {interval * 1000:3.1f} ms: median {np.median(delays):5.2f} ms, "
                      f"p99 {np.percentile(delays, 99):5.2f} ms, max {delays.max():5.2f} ms "
                      f"({len(delays)}/{notes} notes)")
    finally:
        sys.setswitchinterval(default_interval)
        logging.getLogger().removeHandler(handler)
        synth.audio_engine.stop()

    print("With the synthesizer's interval, p99 and max should stay under about 1 ms.")


# Menu key -> (name, test)
TESTS = {
    '1': ("Music Engine", test_music_engine),
    '2': ("Keyboard Handler", test_keyboard_handler),
    '3': ("Basic Audio", test_audio_basic),
    '4': ("Streaming Audio", test_audio_streaming),
    '5': ("Audio Engine", test_audio_engine),
    '6': ("Full Integration", test_full_integration),
    '7': ("Note-on Latency", test_note_latency),
}


def run_test(name, test_func) -> str:
    """Run one test, print and return its outcome"""
    try:
        test_func()
        outcome = PASSED
    except Skipped as e:
        print(f"Skipped: {e}")
        outcome = SKIPPED
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C)")
        outcome = INTERRUPTED
    except Exception:
        traceback.print_exc()
        outcome = FAILED
    print(f"\n{OUTCOME_ICONS[outcome]} {name} test {outcome}")
    return outcome


def exit_code(outcomes) -> int:
    """Exit code of a run: interrupted, else failed if any test failed"""
    if INTERRUPTED in outcomes:
        return EXIT_INTERRUPTED
    return 1 if FAILED in outcomes else 0


def run_all_tests() -> int:
    """Run all tests (Ctrl+C ends the run), return the exit code"""
    print("🧪 SYNTHESIZER MANUAL TESTS")
    print("=" * 60)

    results = []
    for name, test_func in TESTS.values():
        print(f"\n🔍 Running {name} test...")
        outcome = run_test(name, test_func)
        results.append((name, outcome))
        if outcome == INTERRUPTED:
            break

    # Summary
    print("\n" + "=" * 60)
    print("📊 TEST RESULTS SUMMARY")
    print("=" * 60)

    for name, outcome in results:
        print(f"  {name:20} {OUTCOME_ICONS[outcome]} {outcome}")

    outcomes = [outcome for _, outcome in results]
    not_run = len(TESTS) - len(results)
    print(f"\n🎯 Overall: {outcomes.count(PASSED)} passed, {outcomes.count(FAILED)} failed, "
          f"{outcomes.count(SKIPPED)} skipped, {not_run} not run")

    code = exit_code(outcomes)
    if code == 0:
        print("🎉 No test failed! Your synthesizer should work correctly.")
    else:
        print("⚠️  Not all tests ran or passed. Check the details above.")
    return code


def menu() -> int:
    """Interactive menu, return the exit code"""
    print("🧪 SYNTHESIZER MANUAL TESTS")
    print("=" * 40)
    print("Select a test to run:")
    for key, (name, _) in TESTS.items():
        print(f"  {key}: {name} Test")
    print("  a: Run All Tests")
    print("  q: Quit")

    while True:
        # Ctrl+C or Ctrl+D at the prompt: quit, without a traceback
        try:
            choice = input("\nEnter your choice: ").lower().strip()
        except KeyboardInterrupt:
            print("\nGoodbye!")
            return EXIT_INTERRUPTED
        except EOFError:
            print("\nGoodbye!")
            return 0

        if choice == 'q':
            print("Goodbye!")
            return 0
        elif choice == 'a':
            return run_all_tests()
        elif choice in TESTS:
            run_test(*TESTS[choice])
        else:
            print("Invalid choice. Please try again.")


def main() -> int:
    """Main test runner"""
    if len(sys.argv) == 1:
        return menu()

    # Run specific test
    test_num = sys.argv[1]
    if test_num not in TESTS:
        print("Invalid test number. Available tests:")
        for num, (name, _) in TESTS.items():
            print(f"  {num}: {name}")
        return 1
    name, test_func = TESTS[test_num]
    print(f"Running {name} test...")
    return exit_code([run_test(name, test_func)])


if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    print("Running manual tests...")
    sys.exit(main())
