"""
Audio engine module - handles audio generation and playback.

Architecture (hybrid two-process design):

- The AudioEngine class lives in the main (UI) process. It never touches
  audio data: it only sends small command messages ('PLAY_NOTE', 'STOP_NOTE',
  'SET_VOLUME', 'STOP') through a multiprocessing queue.
- The audio process owns the PortAudio stream and synthesizes the audio
  directly inside the stream callback. Its GIL is shared only with a
  near-idle command thread, so UI work (keyboard handling, TUI rendering)
  can never delay the audio callback.
- A low-rate status queue flows back to the main process: log messages,
  xrun count, measured latency and active voice count (displayed by the TUI).

No audio buffers ever cross the process boundary, which keeps latency at
"stream latency + one buffer" and avoids the pacing/teardown issues of
shipping rendered audio through queues.
"""

# Global imports
import configparser
import multiprocessing as mp
import queue
import signal
import threading
import time

import numpy as np
import sounddevice as sd

# Local imports
from logger import log
from classes import Note, Flute, INSTRUMENTS

# Polyphony headroom applied before tanh soft-clipping: with a gain of 1/3,
# a single note keeps a near-linear (low distortion) response and three
# simultaneous full-scale notes land around +/-1 with gentle compression
# instead of hard clipping.
HEADROOM = 1.0 / 3.0

# How often the audio process reports its status to the main process (seconds)
STATUS_PERIOD = 0.1


def saturate_audio(buffer: np.ndarray) -> np.ndarray:
    """
    Soft-clip audio to [-1.0, 1.0] using tanh.

    tanh has unity slope at 0, so quiet signals pass through (almost)
    untouched and compression only kicks in near full scale. The HEADROOM
    pre-gain preserves clean polyphony (see HEADROOM comment above).
    """
    return np.tanh(HEADROOM * buffer)


class _AudioProcess:
    """
    State and logic of the audio process. Only ever instantiated inside the
    child process (see audio_process_main).
    """

    def __init__(self, command_queue, status_queue, sample_rate, buffer_size, channels, default_volume):
        self.command_queue = command_queue
        self.status_queue = status_queue
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.channels = channels

        # Shared between the command loop and the audio callback. The lock is
        # only ever held for microseconds (list append/comprehension), so the
        # callback is effectively never blocked.
        self.lock = threading.Lock()
        self.notes = []
        self.volume = default_volume

        self.xruns = 0
        self.running = True

    def send_log(self, msg, level=0):
        """Forward a log message to the main process (single owner of stdout)"""
        self.status_queue.put(('LOG', msg, level))

    def _audio_callback(self, outdata, frames, time_info, status):
        """
        PortAudio callback - runs on the real-time audio thread.
        Mixes all active notes, soft-clips, and writes to the output buffer.
        """
        if status:
            # Underflow/overflow reported by PortAudio: count it, the status
            # loop will report it to the UI (no logging from the RT thread).
            self.xruns += 1

        buffer = np.zeros(frames, dtype=np.float32)

        with self.lock:
            if self.notes:
                for note in self.notes:
                    buffer += note.get_next_buffer(frames)
                # Prune notes whose release phase has completed
                self.notes = [note for note in self.notes if not note.ended]
                buffer = saturate_audio(buffer)

        for ch in range(self.channels):
            outdata[:, ch] = buffer

    def _create_stream(self) -> sd.OutputStream:
        """Create and start the output stream, trying several latency settings"""
        latency_options = ['low', 'high', None]
        last_error = None

        for latency in latency_options:
            stream = None
            try:
                self.send_log(f"Trying latency setting: {latency}", 1)
                stream = sd.OutputStream(
                    callback=self._audio_callback,
                    samplerate=self.sample_rate,
                    blocksize=self.buffer_size,
                    channels=self.channels,
                    dtype=np.float32,
                    latency=latency
                )
                stream.start()
                self.send_log(f"Audio stream started with latency={latency} "
                              f"(measured: {stream.latency * 1000:.1f} ms)", 1)
                return stream

            except sd.PortAudioError as e:
                last_error = e
                self.send_log(f"Failed with latency={latency}: {e}", 2)
                if stream is not None:
                    stream.close()

        raise last_error

    def _handle_command(self, command):
        """Apply a command received from the main process"""
        cmd_type = command[0]

        if cmd_type == 'PLAY_NOTE':
            key, frequency, instrument_type = command[1], command[2], command[3]
            instrument_class = INSTRUMENTS.get(instrument_type)
            if instrument_class is None:
                self.send_log(f"Unknown instrument '{instrument_type}', defaulting to flute", 2)
                instrument_class = Flute

            note = Note(frequency, instrument_class(self.sample_rate), self.volume, key)
            with self.lock:
                self.notes.append(note)
            self.send_log(f"Created note: {key} -> {frequency:.1f}Hz ({instrument_type})", 0)

        elif cmd_type == 'STOP_NOTE':
            key = command[1]
            with self.lock:
                for note in self.notes:
                    if note.key == key and not note.is_released:
                        note.release()
                        break

        elif cmd_type == 'SET_VOLUME':
            volume = max(0.0, min(1.0, command[1]))
            with self.lock:
                self.volume = volume
                for note in self.notes:
                    note.volume = volume

        elif cmd_type == 'STOP':
            self.running = False
            self.send_log("Audio process stopping", 1)

        else:
            self.send_log(f"Unknown command: {cmd_type}", 2)

    def _emit_status(self, stream):
        """Send a status snapshot to the main process (consumed by the TUI)"""
        with self.lock:
            voices = len(self.notes)
        self.status_queue.put(('STATUS', {
            'xruns': self.xruns,
            'latency_ms': stream.latency * 1000.0,
            'voices': voices,
            'volume': self.volume,
        }))

    def run(self):
        """Main loop of the audio process: consume commands, emit status"""
        try:
            stream = self._create_stream()
        except Exception as e:
            self.status_queue.put(('ERROR', f"Could not create audio stream: {e}"))
            self.status_queue.put(('EXIT',))
            return

        self.status_queue.put(('READY',))
        last_status_time = 0.0

        try:
            while self.running:
                # Blocking get with timeout: commands are applied with sub-ms
                # latency, and the timeout paces the periodic status reports.
                try:
                    command = self.command_queue.get(timeout=STATUS_PERIOD)
                    self._handle_command(command)
                except queue.Empty:
                    pass

                now = time.monotonic()
                if now - last_status_time >= STATUS_PERIOD:
                    self._emit_status(stream)
                    last_status_time = now
        finally:
            # The stream is stopped *inside this process*, while the
            # interpreter is still fully alive: no teardown race possible.
            stream.stop()
            stream.close()
            self.send_log("Audio process stopped cleanly", 1)
            self.status_queue.put(('EXIT',))


def audio_process_main(command_queue, status_queue, sample_rate, buffer_size, channels, default_volume):
    """Entry point of the audio process"""
    # Ctrl+C sends SIGINT to the whole foreground process group; ignore it
    # here so the shutdown is solely coordinated by the main process through
    # the 'STOP' command (otherwise a KeyboardInterrupt traceback is printed
    # from inside the command queue wait).
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    process = _AudioProcess(command_queue, status_queue, sample_rate,
                            buffer_size, channels, default_volume)
    process.run()


class AudioEngine:
    """
    Main-process facade of the audio engine. Spawns the audio process and
    communicates with it through command/status queues.
    """

    def __init__(self, config_file='config.ini', sample_rate=None, buffer_size=None, channels=None):
        """
        Initialize audio engine

        Args:
            config_file: Path to configuration file
            sample_rate: Audio sample rate in Hz (overrides config)
            buffer_size: Audio buffer size in samples (overrides config)
            channels: Number of audio channels (overrides config)
        """
        # Load configuration
        config = configparser.ConfigParser()
        config.read(config_file)

        # Use provided parameters or load from config
        self.sample_rate = sample_rate or config.getint('AUDIO', 'SAMPLE_RATE', fallback=48000)
        self.buffer_size = buffer_size or config.getint('AUDIO', 'BUFFER_SIZE', fallback=128)
        self.channels = channels or config.getint('AUDIO', 'CHANNELS', fallback=2)
        self.buffer_duration = self.buffer_size / self.sample_rate

        # Load default volume from config
        self.default_volume = config.getfloat('AUDIO', 'DEFAULT_VOLUME', fallback=0.5)
        self.volume = self.default_volume

        # Multiprocessing communication
        self.command_queue = mp.Queue()
        self.status_queue = mp.Queue()
        self.process = None
        self.running = False

        # Status reporting
        self.latest_status = {}
        self._status_thread = None
        self._ready_event = threading.Event()
        self._start_error = None

        log(f"Audio engine initialized: {self.sample_rate}Hz, {self.buffer_size} samples, "
            f"{self.channels} channel(s)", 1)

    def set_volume(self, volume: float):
        """Set master volume (0.0 to 1.0)"""
        volume = max(0.0, min(1.0, volume))
        self.volume = volume
        if self.running:
            self.command_queue.put(('SET_VOLUME', volume))

    def get_volume(self) -> float:
        """Get current master volume"""
        return self.volume

    def get_status(self) -> dict:
        """Get the latest status snapshot from the audio process (for the TUI)"""
        return dict(self.latest_status)

    def play_note(self, key: str, frequency: float, instrument_type: str = 'flute'):
        """
        Create and play a new note

        Args:
            key: Key identifier
            frequency: Frequency in Hz
            instrument_type: Type of instrument to use (default: 'flute')
        """
        if self.running:
            self.command_queue.put(('PLAY_NOTE', key, frequency, instrument_type))

    def stop_note(self, key: str):
        """
        Stop a note (trigger release phase)

        Args:
            key: Key identifier of note to release
        """
        if self.running:
            self.command_queue.put(('STOP_NOTE', key))

    def _status_reader(self):
        """Thread consuming the status queue: logs, status snapshots, lifecycle"""
        while True:
            try:
                item = self.status_queue.get(timeout=0.5)
            except queue.Empty:
                if not self.running and (self.process is None or not self.process.is_alive()):
                    break
                continue

            kind = item[0]
            if kind == 'LOG':
                log(item[1], item[2])
            elif kind == 'STATUS':
                self.latest_status = item[1]
            elif kind == 'READY':
                self._ready_event.set()
            elif kind == 'ERROR':
                self._start_error = item[1]
                self._ready_event.set()
            elif kind == 'EXIT':
                break

    def start(self):
        """Start audio engine (spawns the audio process)"""
        if self.running:
            return True

        self.running = True
        self._ready_event.clear()
        self._start_error = None

        log("Starting audio process", 1)
        self.process = mp.Process(
            target=audio_process_main,
            args=(self.command_queue, self.status_queue, self.sample_rate,
                  self.buffer_size, self.channels, self.default_volume),
            name='audio-process'
        )
        self.process.start()

        self._status_thread = threading.Thread(target=self._status_reader,
                                               name='status-reader', daemon=True)
        self._status_thread.start()

        # Wait until the audio process reports that its stream is up
        if not self._ready_event.wait(timeout=10.0):
            log("Audio process did not start in time", 3)
            self.stop()
            return False

        if self._start_error is not None:
            log(f"Failed to start audio engine: {self._start_error}", 3)
            self.stop()
            return False

        log("Audio engine started successfully", 1)
        return True

    def stop(self):
        """Stop audio engine (the audio process closes its own stream)"""
        if not self.running:
            return

        self.running = False

        if self.process is not None and self.process.is_alive():
            log("Stopping audio process", 1)
            self.command_queue.put(('STOP',))
            self.process.join(timeout=2.0)
            if self.process.is_alive():
                log("Force terminating audio process", 2)
                self.process.terminate()
                self.process.join()
        self.process = None

        if self._status_thread is not None:
            self._status_thread.join(timeout=1.0)
            self._status_thread = None

        log("Audio engine stopped", 1)

    def print_device_info(self):
        """Print available audio devices"""
        log("\nAvailable audio devices:", 1)
        devices = sd.query_devices()
        log(str(devices), 1)


def test_basic_audio(duration=2.0, frequency=440.0):
    """Test basic audio playback"""
    log(f"Playing {frequency}Hz tone for {duration} seconds...", 1)

    sample_rate = 48000
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    wave = 0.3 * np.sin(2 * np.pi * frequency * t)

    sd.play(wave, sample_rate)
    sd.wait()

    log("Basic audio test completed", 1)
    return True


def test_streaming_audio(duration=3.0):
    """Test streaming audio"""
    log(f"Testing streaming audio for {duration} seconds...", 1)

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

    log("Streaming audio test completed", 1)
    return True
