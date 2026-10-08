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
  xrun count, output latency (as reported by PortAudio), active voice
  count and recording duration (displayed by the TUI).
- Recording ('RECORD_START', 'RECORD_STOP') happens in the audio process
  too: the callback queues copies of its output buffers, which a recorder
  thread writes to a FLAC file (see _Recorder).
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
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

# Local imports
from synth.dsp import Note, INSTRUMENTS
from synth.config import AudioConfig
from synth.errors import SynthError

log = logging.getLogger(__name__)

# Polyphony headroom applied before tanh soft-clipping. With a gain of 1/3,
# a single full-scale note peaks at tanh(1/3) = 0.32 (about -10 dBFS) with a
# near-linear (low distortion) response; three in phase peak at tanh(1) =
# 0.76 and five at 0.93: chords are gently compressed instead of hard
# clipped.
HEADROOM = 1.0 / 3.0

# How often the audio process reports its status to the main process (seconds)
STATUS_PERIOD = 0.1

# How often the recorder thread writes the queued buffers to the file (seconds)
RECORD_WRITE_PERIOD = 0.1

# Expected errors of a recording file (cannot be created, disk full...):
# reported, the synthesizer keeps playing
RECORDING_ERRORS = (OSError, sf.SoundFileError)


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


def format_duration(seconds: float) -> str:
    """'m:ss' duration"""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


class _Recorder:
    """
    Records the audio output to a FLAC file (24-bit, maximum compression).
    The audio callback only queues copies of its buffers (put never blocks);
    a thread of its own writes them, libsndfile encoding without the GIL.
    """

    def __init__(self, path: Path, sample_rate: int, channels: int):
        """Raises one of RECORDING_ERRORS if the file cannot be created (an
        existing file is never overwritten)"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.sample_rate = sample_rate
        # compression_level: 0.0 to 1.0, mapped by libsndfile to FLAC levels 0 to 8
        self.file = sf.SoundFile(path, 'x', sample_rate, channels, subtype='PCM_24',
                                 format='FLAC', compression_level=1.0)
        self.blocks = queue.SimpleQueue()  # Output buffers, from the audio callback
        self.frames = 0                    # Frames written so far
        self.error = None                  # Write failure of the thread, if any
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name='recorder', daemon=True)
        self._thread.start()

    @property
    def duration(self) -> float:
        """Seconds written so far"""
        return self.frames / self.sample_rate

    def _run(self):
        try:
            while not self._stopping.wait(RECORD_WRITE_PERIOD):
                self._write_pending()
            self._write_pending()
        except RECORDING_ERRORS as e:
            self.error = e  # Reported by stop()

    def _write_pending(self):
        """Write the queued buffers, in one go"""
        blocks = []
        while not self.blocks.empty():  # Single consumer: never blocks
            blocks.append(self.blocks.get())
        if blocks:
            data = np.concatenate(blocks)
            self.file.write(data)
            self.frames += len(data)

    def stop(self):
        """Write the remaining buffers and close the file (raises the write
        error, if any)"""
        self._stopping.set()
        self._thread.join()
        self.file.close()
        if self.error is not None:
            raise self.error


class _AudioProcess:
    """
    State and logic of the audio process. Only ever instantiated inside the
    child process (see audio_process_main).
    """

    def __init__(self, command_queue, status_queue, audio_config: AudioConfig):
        self.command_queue = command_queue
        self.status_queue = status_queue
        self.device = audio_config.device
        self.sample_rate = audio_config.sample_rate
        self.buffer_size = audio_config.buffer_size
        self.channels = audio_config.channels

        # Shared between the command loop and the audio callback. The lock is
        # only ever held for microseconds (list append/comprehension), so the
        # callback is effectively never blocked.
        self.lock = threading.Lock()
        self.notes = []
        self.volume = audio_config.default_volume

        self.xruns = 0
        self.running = True
        # Traceback of an exception raised in the audio callback, reported
        # by the command loop
        self.callback_error = None
        # Current recording, None when not recording (read by the callback)
        self.recorder: _Recorder | None = None

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

            recorder = self.recorder  # Read once: the command loop may reset it
            if recorder is not None:
                recorder.blocks.put(outdata.copy())
        except Exception:
            # Exceptions must not reach PortAudio's C caller (cffi would print
            # them over the UI and abort the stream): keep the traceback for
            # the command loop to report, and stop the stream cleanly.
            self.callback_error = traceback.format_exc()
            raise sd.CallbackAbort

    def _create_stream(self) -> sd.OutputStream:
        """
        Create and start the output stream: low latency first, then high
        latency, which a few devices require (some ALSA / USB ones). Other
        failures (device busy, unsupported settings...) fail both alike.
        (latency=None would mean the sounddevice default: 'high' again.)
        """
        latency_options = ['low', 'high']
        last_error = None

        for latency in latency_options:
            stream = None
            try:
                log.info(f"Trying latency setting: {latency}")
                stream = sd.OutputStream(
                    callback=self._audio_callback,
                    device=self.device,
                    samplerate=self.sample_rate,
                    blocksize=self.buffer_size,
                    channels=self.channels,
                    dtype=np.float32,
                    latency=latency
                )
                stream.start()
                log.info(f"Audio stream started with latency={latency} "
                         f"(reported: {stream.latency * 1000:.1f} ms)")
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
            instrument = INSTRUMENTS[instrument_type](self.sample_rate)
            note = Note(frequency, instrument, self.volume, key)
            with self.lock:
                self.notes.append(note)
            # Confirms the command reached the audio process (the main process
            # logs the key -> note -> frequency side)
            log.debug(f"Audio process: note {key} started ({frequency:.1f} Hz, {instrument_type})")

        elif cmd_type == 'STOP_NOTE':
            key = command[1]
            with self.lock:
                for note in self.notes:
                    if note.key == key and not note.is_released:
                        note.release()
                        log.debug(f"Audio process: note {key} released")
                        break

        elif cmd_type == 'SET_VOLUME':
            volume = command[1]  # Already clamped by AudioEngine.set_volume
            with self.lock:
                self.volume = volume
                for note in self.notes:
                    note.volume = volume

        elif cmd_type == 'RECORD_START':
            self._start_recording(command[1])

        elif cmd_type == 'RECORD_STOP':
            self._stop_recording()

        elif cmd_type == 'STOP':
            self.running = False
            log.info("Audio process stopping")

        else:
            raise ValueError(f"Unknown command: {cmd_type}")

    def _start_recording(self, path: Path):
        if self.recorder is not None:
            log.info(f"Already recording ({self.recorder.path})")
            return
        try:
            self.recorder = _Recorder(path, self.sample_rate, self.channels)
        except RECORDING_ERRORS as e:
            log.error(f"Cannot record: {e}")
            return
        log.info(f"🔴 Recording to {path}")

    def _stop_recording(self):
        if self.recorder is None:
            log.info("Not recording")
            return
        recorder, self.recorder = self.recorder, None  # The callback stops queueing
        try:
            recorder.stop()
        except RECORDING_ERRORS as e:
            log.error(f"Recording stopped, {recorder.path} may be incomplete: {e}")
            return
        log.info(f"⬜ Recording saved: {recorder.path} ({format_duration(recorder.duration)})")

    def _emit_status(self, stream):
        """Send a status snapshot to the main process (consumed by the TUI)"""
        with self.lock:
            voices = len(self.notes)
        self.status_queue.put(('STATUS', {
            'xruns': self.xruns,
            'latency_ms': stream.latency * 1000.0,
            'voices': voices,
            'volume': self.volume,
            # Seconds recorded, None when not recording
            'recording': self.recorder.duration if self.recorder is not None else None,
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
                    if self.recorder is not None and self.recorder.error is not None:
                        self._stop_recording()  # Write failure (disk full...): reported
                    self._emit_status(stream)
                    last_status_time = now
        finally:
            # The stream is stopped *inside this process*, while the
            # interpreter is still fully alive: no teardown race possible.
            stream.stop()
            stream.close()
            if self.recorder is not None:  # Quitting while recording: finalize the file
                self._stop_recording()


def list_devices() -> str:
    """Table of the audio devices, for --list-devices"""
    return f"{sd.query_devices()}\n(> default input, < default output)"


def audio_process_main(command_queue, status_queue, audio_config: AudioConfig, log_level):
    """Entry point of the audio process (audio_config.device: a device index)"""
    # Records below the main process level are not even sent
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(_StatusQueueHandler(status_queue))

    # Ctrl+C sends SIGINT to the whole foreground process group; ignore it
    # here so the shutdown is solely coordinated by the main process through
    # the 'STOP' command (otherwise a KeyboardInterrupt traceback is printed
    # from inside the command queue wait).
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    process = _AudioProcess(command_queue, status_queue, audio_config)
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
        self.config = audio_config
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

    def start_recording(self):
        """Start recording the output to a new file of the recording
        directory, named after the current time"""
        if self.running:
            path = self.config.record_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.flac"
            self.command_queue.put(('RECORD_START', path))

    def stop_recording(self):
        """Stop recording (the file is finalized)"""
        if self.running:
            self.command_queue.put(('RECORD_STOP',))

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
            SynthError: no such output device, or the audio stream could
                        not be started (the audio process is stopped)
            AudioProcessCrash: bug in the audio process
        """
        # Resolved here, where a wrong device is an expected error, not in
        # the audio process
        try:
            device = sd.query_devices(self.config.device, 'output')
        except (ValueError, sd.PortAudioError) as e:
            raise SynthError(f"Output device: {e} (see --list-devices)") from e
        log.info(f"Output device: {device['name']}")

        self.running = True
        self._ready_event.clear()
        self._start_error = None

        log.info("Starting audio process")
        self.process = mp.Process(
            target=audio_process_main,
            args=(self.command_queue, self.status_queue,
                  replace(self.config, device=device['index']),
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

