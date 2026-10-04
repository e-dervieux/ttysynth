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
- A low-rate status queue flows back to the main process: log records,
  xrun count, measured latency and active voice count (displayed by the TUI).
- Failures of the audio process (stream lost, bug, unexpected death) are
  reported through the status queue too, and shut the synthesizer down (see
  AudioEngine._status_reader).

No audio buffers ever cross the process boundary, which keeps latency at
"stream latency + one buffer" and avoids the pacing/teardown issues of
shipping rendered audio through queues.
"""

# Global imports
import logging
import logging.handlers
import multiprocessing as mp
import queue
import signal
import sys
import threading
import time
import traceback

import numpy as np
import sounddevice as sd

# Local imports
from classes import Note, Flute, INSTRUMENTS
from config import AudioConfig
from errors import SynthError

log = logging.getLogger(__name__)

# Polyphony headroom applied before tanh soft-clipping: with a gain of 1/3,
# a single note keeps a near-linear (low distortion) response and three
# simultaneous full-scale notes land around +/-1 with gentle compression
# instead of hard clipping.
HEADROOM = 1.0 / 3.0

# How often the audio process reports its status to the main process (seconds)
STATUS_PERIOD = 0.1


class _StatusQueueHandler(logging.handlers.QueueHandler):
    """Sends the audio process log records to the main process (the single
    owner of the terminal) through the status queue, as ('LOG', record)"""

    def enqueue(self, record):
        self.queue.put(('LOG', record))


class AudioProcessCrash(RuntimeError):
    """A bug in the audio process. Exceptions cannot cross the process
    boundary: the message holds the audio process traceback."""


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
        # Traceback of an exception raised in the audio callback, reported
        # by the command loop
        self.callback_error = None

    def _audio_callback(self, outdata, frames, time_info, status):
        """
        PortAudio callback - runs on the real-time audio thread.
        Mixes all active notes, soft-clips, and writes to the output buffer.
        """
        # The try block costs nothing until an exception is raised (zero-cost
        # exception handling, Python >= 3.11)
        try:
            if status:
                # Underflow/overflow reported by PortAudio: count it, the
                # status loop will report it to the UI (no logging from the
                # RT thread).
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
        except Exception:
            # Exceptions must not reach PortAudio's C caller (cffi would print
            # them over the UI and abort the stream): keep the traceback for
            # the command loop to report, and stop the stream cleanly.
            self.callback_error = traceback.format_exc()
            raise sd.CallbackAbort

    def _create_stream(self) -> sd.OutputStream:
        """Create and start the output stream, trying several latency settings"""
        latency_options = ['low', 'high', None]
        last_error = None

        for latency in latency_options:
            stream = None
            try:
                log.info(f"Trying latency setting: {latency}")
                stream = sd.OutputStream(
                    callback=self._audio_callback,
                    samplerate=self.sample_rate,
                    blocksize=self.buffer_size,
                    channels=self.channels,
                    dtype=np.float32,
                    latency=latency
                )
                stream.start()
                log.info(f"Audio stream started with latency={latency} "
                         f"(measured: {stream.latency * 1000:.1f} ms)")
                return stream

            except sd.PortAudioError as e:
                last_error = e
                log.warning(f"Failed with latency={latency}: {e}")
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
                log.warning(f"Unknown instrument '{instrument_type}', defaulting to flute")
                instrument_class = Flute

            note = Note(frequency, instrument_class(self.sample_rate), self.volume, key)
            with self.lock:
                self.notes.append(note)
            log.debug(f"Created note: {key} -> {frequency:.1f}Hz ({instrument_type})")

        elif cmd_type == 'STOP_NOTE':
            key = command[1]
            with self.lock:
                for note in self.notes:
                    if note.key == key and not note.is_released:
                        note.release()
                        break

        elif cmd_type == 'SET_VOLUME':
            volume = command[1]  # Already clamped by AudioEngine.set_volume
            with self.lock:
                self.volume = volume
                for note in self.notes:
                    note.volume = volume

        elif cmd_type == 'STOP':
            self.running = False
            log.info("Audio process stopping")

        else:
            log.warning(f"Unknown command: {cmd_type}")

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
        """
        Main loop of the audio process: consume commands, emit status, and
        watch the stream. Returns on 'STOP', or after reporting a failure
        ('ERROR': expected, e.g. device lost; 'CRASH': bug in the callback).
        Other exceptions are bugs, reported by audio_process_main.
        """
        try:
            stream = self._create_stream()
        except sd.PortAudioError as e:
            # Expected failure (device unavailable, unsupported settings...)
            self.status_queue.put(('ERROR', f"Could not create audio stream: {e}"))
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
                    if self.callback_error is not None:
                        self.status_queue.put(('CRASH', self.callback_error))
                        return
                    if not stream.active:
                        self.status_queue.put(('ERROR', "Audio stream stopped unexpectedly "
                                                        "(output device disconnected?)"))
                        return
                    self._emit_status(stream)
                    last_status_time = now
        finally:
            # The stream is stopped *inside this process*, while the
            # interpreter is still fully alive: no teardown race possible.
            stream.stop()
            stream.close()


def audio_process_main(command_queue, status_queue, sample_rate, buffer_size, channels,
                       default_volume, log_level):
    """Entry point of the audio process"""
    # Records below the main process level are not even sent
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(_StatusQueueHandler(status_queue))

    # Ctrl+C sends SIGINT to the whole foreground process group; ignore it
    # here so the shutdown is solely coordinated by the main process through
    # the 'STOP' command (otherwise a KeyboardInterrupt traceback is printed
    # from inside the command queue wait).
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    process = _AudioProcess(command_queue, status_queue, sample_rate,
                            buffer_size, channels, default_volume)
    try:
        process.run()
    except Exception:
        # A bug: report it to the main process, which owns the terminal and
        # raises it, instead of printing it over the UI
        status_queue.put(('CRASH', traceback.format_exc()))
        sys.exit(1)
    log.info("Audio process stopped")
    status_queue.put(('EXIT',))


class AudioEngine:
    """
    Main-process facade of the audio engine. Spawns the audio process and
    communicates with it through command/status queues.
    """

    def __init__(self, audio_config: AudioConfig):
        """
        Initialize audio engine

        Args:
            audio_config: [AUDIO] settings (see config.py)
        """
        self.sample_rate = audio_config.sample_rate
        self.buffer_size = audio_config.buffer_size
        self.channels = audio_config.channels
        self.buffer_duration = self.buffer_size / self.sample_rate

        self.default_volume = audio_config.default_volume
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
        self._start_error = None  # Failure of the audio process before READY

        log.info(f"Audio engine initialized: {self.sample_rate}Hz, {self.buffer_size} samples, "
                 f"{self.channels} channel(s)")

    def set_volume(self, volume: float):
        """Set master volume, clamped to 0.0 - 1.0 (the single place where
        it is)"""
        self.volume = max(0.0, min(1.0, volume))
        if self.running:
            self.command_queue.put(('SET_VOLUME', self.volume))
        log.info(f"🔊 Volume: {self.volume * 100:.0f}%")

    def change_volume(self, delta: float):
        """Change master volume by `delta`"""
        self.set_volume(self.volume + delta)

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

    def _status_reader(self, process: mp.Process):
        """
        Thread consuming the status queue: logs, status snapshots, lifecycle.

        A failure of the audio process (reported 'ERROR' or 'CRASH', or
        unexpected exit / death) is handed to start() if the stream is not
        ready yet. Afterwards it is raised here: through threading.excepthook,
        it shuts the synthesizer down (see Synthesizer.run).
        """
        while True:
            failure = None
            try:
                item = self.status_queue.get(timeout=0.5)
            except queue.Empty:
                # Queue drained: a dead process sent everything it could
                if process.is_alive():
                    continue
                if not self.running:
                    break  # Stopped by stop(), which may have terminated it
                code = process.exitcode
                cause = f"killed by signal {-code}" if code < 0 else f"exit code {code}"
                failure = SynthError(f"Audio process died unexpectedly ({cause})")
            else:
                kind = item[0]
                if kind == 'LOG':
                    record = item[1]
                    logging.getLogger(record.name).handle(record)
                elif kind == 'STATUS':
                    self.latest_status = item[1]
                elif kind == 'READY':
                    self._ready_event.set()
                elif kind == 'ERROR':
                    failure = SynthError(item[1])
                elif kind == 'CRASH':
                    failure = AudioProcessCrash(f"Audio process crashed:\n{item[1]}")
                elif kind == 'EXIT':
                    if not self.running:
                        break  # Normal stop
                    failure = SynthError("Audio process exited unexpectedly")

            if failure is not None:
                if self._ready_event.is_set():
                    raise failure
                self._start_error = failure
                self._ready_event.set()
                return

    def start(self):
        """
        Start audio engine (spawns the audio process).

        Raises:
            SynthError: the audio stream could not be started (the audio
                        process is stopped)
            AudioProcessCrash: bug in the audio process
        """
        if self.running:
            return

        self.running = True
        self._ready_event.clear()
        self._start_error = None

        log.info("Starting audio process")
        self.process = mp.Process(
            target=audio_process_main,
            args=(self.command_queue, self.status_queue, self.sample_rate,
                  self.buffer_size, self.channels, self.default_volume,
                  logging.getLogger().getEffectiveLevel()),
            name='audio-process'
        )
        self.process.start()

        self._status_thread = threading.Thread(target=self._status_reader, args=(self.process,),
                                               name='status-reader', daemon=True)
        self._status_thread.start()

        # Wait until the audio process reports that its stream is up
        if not self._ready_event.wait(timeout=10.0):
            self.stop()
            raise SynthError("Audio process did not start within 10 s")

        if self._start_error is not None:
            self.stop()
            raise self._start_error

        log.info("Audio engine started successfully")

    def stop(self):
        """Stop audio engine (the audio process closes its own stream)"""
        if not self.running:
            return

        self.running = False

        if self.process is not None and self.process.is_alive():
            log.info("Stopping audio process")
            self.command_queue.put(('STOP',))
            self.process.join(timeout=2.0)
            if self.process.is_alive():
                log.warning("Force terminating audio process")
                self.process.terminate()
                self.process.join()
        self.process = None

        if self._status_thread is not None:
            self._status_thread.join(timeout=1.0)
            self._status_thread = None

        log.info("Audio engine stopped")

    def print_device_info(self):
        """Print available audio devices"""
        log.info("\nAvailable audio devices:")
        devices = sd.query_devices()
        log.info(str(devices))


def test_basic_audio(duration=2.0, frequency=440.0):
    """Test basic audio playback"""
    log.info(f"Playing {frequency}Hz tone for {duration} seconds...")

    sample_rate = 48000
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    wave = 0.3 * np.sin(2 * np.pi * frequency * t)

    sd.play(wave, sample_rate)
    sd.wait()

    log.info("Basic audio test completed")
    return True


def test_streaming_audio(duration=3.0):
    """Test streaming audio"""
    log.info(f"Testing streaming audio for {duration} seconds...")

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

    log.info("Streaming audio test completed")
    return True
