"""
Fake sounddevice module: the automated tests never touch the audio device.

tests/conftest.py puts this directory first in sys.path, which the audio
processes spawned by the tests inherit (multiprocessing passes sys.path
along). The FAKE_SD environment variable, read when a stream is created,
selects a failure to simulate:

    open-fail      opening the stream fails (PortAudioError)
    no-queued-buffers opening fails with a latency in seconds ('low' / 'high' work)
    early-death    the audio process dies before the stream is ready
    callback-bug   the audio callback receives an invalid buffer after 0.3 s
    device-lost    the stream stops by itself after 0.3 s (device unplugged)
    killed         the audio process is killed (SIGKILL) after 0.3 s
    loop-bug       the command loop fails (reading the latency raises)
"""

import os
import signal
import threading
import time

import numpy as np

# Seconds of normal operation before the simulated failures
FAILURE_DELAY = 0.3


class PortAudioError(Exception):
    pass


class CallbackAbort(Exception):
    pass


FAKE_DEVICE = {'name': 'Fake Speakers', 'index': 0}


def query_devices(device=None, kind=None):
    """The device list, or one output device: 'Fake Speakers', index 0,
    also the default"""
    if device is None and kind is None:
        return 'fake device list'
    if device in (None, 0) or (isinstance(device, str) and device.lower() in 'fake speakers'):
        return FAKE_DEVICE
    if isinstance(device, int):
        raise PortAudioError(f'Error querying device {device}')
    raise ValueError(f'No output device matching {device!r}')


class OutputStream:
    """Calls the callback from a thread, at the pace of the audio buffers"""

    def __init__(self, callback, device, samplerate, blocksize, channels, dtype, latency):
        self.mode = os.environ.get('FAKE_SD', '')
        if self.mode == 'open-fail':
            raise PortAudioError('Error opening OutputStream: Invalid sample rate [PaErrorCode -9997]')
        if self.mode == 'no-queued-buffers' and not isinstance(latency, str):
            raise PortAudioError('Error opening OutputStream: Unanticipated host error [PaErrorCode -9999]')
        if self.mode == 'early-death':
            os._exit(3)
        assert device == FAKE_DEVICE['index'], device
        self.callback, self.samplerate = callback, samplerate
        self.blocksize, self.channels = blocksize, channels
        self.active = False
        self._latency_reads = 0

    @property
    def latency(self):
        self._latency_reads += 1
        if self.mode == 'loop-bug' and self._latency_reads > 3:
            raise KeyError('bug in the command loop')
        return 0.005

    def start(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        start = time.monotonic()
        while self.active:
            failing = time.monotonic() - start > FAILURE_DELAY
            if failing and self.mode == 'device-lost':
                self.active = False
                return
            if failing and self.mode == 'killed':
                os.kill(os.getpid(), signal.SIGKILL)
            outdata = np.zeros((self.blocksize, self.channels), dtype=np.float32)
            if failing and self.mode == 'callback-bug':
                outdata = None  # The callback fails writing to it
            try:
                self.callback(outdata, self.blocksize, None, None)
            except CallbackAbort:
                self.active = False
                return
            time.sleep(self.blocksize / self.samplerate)

    def stop(self, ignore_errors=True):
        self.active = False

    def close(self, ignore_errors=True):
        pass
