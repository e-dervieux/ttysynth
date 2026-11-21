"""
Instrument classes for audio synthesis
"""

import numpy as np
from abc import ABC, abstractmethod

# Random generator of the plucked string noise bursts. Created here, at
# import time: numpy loads its random module on first use only, which took
# several ms - too long inside the audio callback (xruns on the first pluck).
_rng = np.random.default_rng()


class Instrument(ABC):
    """Abstract base class for all instruments"""

    def __init__(self, sample_rate: int):
        """
        Initialize instrument

        Args:
            sample_rate: Audio sample rate in Hz
        """
        self.sample_rate = sample_rate

        # ADSR envelope parameters (to be set by child classes)
        self.attack_time = 0.0  # seconds
        self.decay_time = 0.0   # seconds
        self.sustain_level = 1.0  # 0.0 to 1.0
        self.release_time = 0.0  # seconds

    @abstractmethod
    def generate_audio(self, frequency: float, num_samples: int, t_start: int) -> np.ndarray:
        """
        Generate audio samples for this instrument (without envelope)

        Args:
            frequency: Frequency in Hz
            num_samples: Number of samples to generate
            t_start: Starting sample index for phase continuity

        Returns:
            Audio buffer as numpy array (envelope will be applied by Note)
        """


class AdditiveInstrument(Instrument):
    """
    Additive synthesis: a sum of harmonics (sines at multiples of the note
    frequency), whose weights each subclass gives (_harmonics).
    """

    def __init__(self, sample_rate: int):
        super().__init__(sample_rate)
        # Harmonic numbers and weights, set on the first buffer: they may
        # depend on the note frequency (an instrument instance plays one note)
        self._harmonic_numbers = None
        self._harmonic_weights = None

    @abstractmethod
    def _harmonics(self, frequency: float) -> tuple[np.ndarray, np.ndarray]:
        """Harmonic numbers (1.0: the fundamental) and their weights, for a
        note of this frequency"""

    def generate_audio(self, frequency: float, num_samples: int, t_start: int) -> np.ndarray:
        """
        Generate the sum of the harmonics (no envelope applied)

        Args:
            frequency: Frequency in Hz
            num_samples: Number of samples to generate
            t_start: Starting sample index for phase continuity

        Returns:
            Audio buffer as numpy array (without envelope)
        """
        if self._harmonic_numbers is None:
            self._harmonic_numbers, self._harmonic_weights = self._harmonics(frequency)
        # All harmonics at once: a single np.sin over a (samples x harmonics)
        # matrix, then the weighted sum. At 64 samples, numpy time is mostly
        # per-call overhead: few calls on bigger arrays beat a loop.
        angle = (2 * np.pi * frequency / self.sample_rate) * np.arange(t_start, t_start + num_samples)
        harmonics = np.sin(np.multiply.outer(angle, self._harmonic_numbers))
        return (harmonics @ self._harmonic_weights).astype(np.float32)


class Flute(AdditiveInstrument):
    """Flute instrument - additive synthesis (sine + harmonics) with a soft
    ADSR envelope (breath noise: TODO, see noise_level)"""

    def __init__(self, sample_rate: int):
        """
        Initialize Flute instrument

        Args:
            sample_rate: Audio sample rate in Hz
        """
        super().__init__(sample_rate)

        # ADSR parameters for flute
        self.attack_time = 0.05   # 50ms soft attack
        self.decay_time = 0.2     # 200ms decay
        self.sustain_level = 0.8  # 80% sustain level
        self.release_time = 0.2   # 200ms release

        # TODO: breath noise (pink noise mixed into the tone), not
        # implemented yet: these parameters are unused for now
        self.noise_level = 0.0   # Noise amplitude relative to the tone
        self.noise_state = 0.0   # State of the pink noise filter

        # Harmonics: weight of the fundamental, 2nd harmonic, 3rd...
        self.harmonics = [1, 0.2, 0.14, 0, 0.05]

    def _harmonics(self, frequency: float) -> tuple[np.ndarray, np.ndarray]:
        """The same for every note: normalized weights (summing to 1), zero
        weights left out (they would only cost time)"""
        weights = np.array(self.harmonics) / np.sum(self.harmonics)
        return np.flatnonzero(weights) + 1.0, weights[weights != 0]


def _partial_sum_peaks(slope: float, count: int) -> np.ndarray:
    """
    Peak amplitude of sum(k ** -slope * sin(k x), k = 1..n), for n = 1 to
    `count` (index n - 1). The sum is odd and 2 pi periodic: its peak is
    the maximum of its absolute value over [0, pi], sampled finely enough
    (about 30 points per period of the highest harmonic).
    """
    x = np.linspace(0, np.pi, 16 * count)
    k = np.arange(1, count + 1.0)
    partial_sums = np.cumsum(k ** -slope * np.sin(np.multiply.outer(x, k)), axis=1)
    return np.abs(partial_sums).max(axis=0)


class Lead(AdditiveInstrument):
    """
    Bright lead, made to be heard over loud music or a singing voice: a
    sawtooth-like spectrum (every harmonic, weights k ** -SLOPE), rich in
    the 1-4 kHz range the ear is most sensitive to. Built harmonic by
    harmonic up to CUTOFF only: band-limited, hence free of the aliasing
    (inharmonic, out of tune components) of a naive digital sawtooth, which
    no filter can remove afterwards.
    """

    # Spectrum, to tune by ear. SLOPE: 1.0 is a sawtooth, harsh at high
    # volume; higher is softer. CUTOFF: highest harmonic frequency (Hz).
    # MAX_HARMONICS bounds the cost of low notes (about 0.3 us per harmonic
    # per voice and 64-sample buffer).
    SLOPE = 1.4
    CUTOFF = 14000.0
    MAX_HARMONICS = 32
    # Peak amplitude for each number of harmonics: notes are normalized to a
    # peak of 1, like the other instruments (computed once, at import time:
    # too slow for the audio callback)
    _PEAKS = _partial_sum_peaks(SLOPE, MAX_HARMONICS)

    def __init__(self, sample_rate: int):
        super().__init__(sample_rate)
        # Clear attack, full sustain, short release
        self.attack_time = 0.008
        self.decay_time = 0.0
        self.sustain_level = 1.0
        self.release_time = 0.1

    def _harmonics(self, frequency: float) -> tuple[np.ndarray, np.ndarray]:
        # Below the Nyquist frequency in any case (low sample rates)
        cutoff = min(self.CUTOFF, 0.45 * self.sample_rate)
        count = max(1, min(self.MAX_HARMONICS, int(cutoff / frequency)))
        numbers = np.arange(1, count + 1.0)
        return numbers, numbers ** -self.SLOPE / self._PEAKS[count - 1]


class PluckedString(Instrument):
    """
    Plucked string instrument (harp / classical guitar-like) using the
    Karplus-Strong algorithm.

    The algorithm (https://en.wikipedia.org/wiki/Karplus-Strong_string_synthesis)
    was designed in the early 80s to run on extremely cheap hardware:
      1. Fill a delay line of N = sample_rate / frequency samples with white
         noise. This burst of noise is the "pluck" transient.
      2. Each output sample is read from the delay line, then replaced by the
         average of itself and its neighbour, scaled by a damping factor:
             y[n] = damping * 0.5 * (y[n - N] + y[n - N - 1])
      3. The 2-point average acts as a low-pass filter inside the feedback
         loop: high harmonics die quickly (bright attack), low ones ring on
         (warm sustain). The loop period N sets the perceived pitch.

    A nice side effect: amplitude loses a factor `damping` per period, so low
    notes ring longer than high ones - just like real strings.
    """

    def __init__(self, sample_rate: int):
        """
        Initialize PluckedString instrument

        Args:
            sample_rate: Audio sample rate in Hz
        """
        super().__init__(sample_rate)

        # The Karplus-Strong loop produces its own attack (noise burst) and
        # decay, so the ADSR envelope is nearly transparent. The release only
        # fades the note out slowly after key release, letting the string
        # keep ringing harp-style.
        self.attack_time = 0.0
        self.decay_time = 0.0
        self.sustain_level = 1.0
        self.release_time = 1.5

        # Loop gain per period: closer to 1.0 = longer ring.
        self.damping = 0.996

        # Delay line state, created lazily on first generate_audio() call
        # because the frequency is only known then.
        self.delay_line = None
        self.delay_idx = 0

    def generate_audio(self, frequency: float, num_samples: int, t_start: int) -> np.ndarray:
        """
        Generate plucked string audio via Karplus-Strong (no envelope applied)

        Args:
            frequency: Frequency in Hz
            num_samples: Number of samples to generate
            t_start: Starting sample index (unused: state lives in the delay line)

        Returns:
            Audio buffer as numpy array (without envelope)
        """
        if self.delay_line is None:
            # The averaging filter adds half a sample of delay, making the
            # effective loop period N - 0.5 samples: compensate by adding 0.5
            # when sizing the delay line. Residual pitch error is bounded by
            # the +/-0.5 sample quantization (a few cents, fine for this use).
            n = max(2, int(round(self.sample_rate / frequency + 0.5)))
            noise = _rng.uniform(-1.0, 1.0, n)
            # Remove the DC component of the burst: the averaging filter does
            # not attenuate DC, so any offset would ring forever.
            noise -= noise.mean()
            self.delay_line = noise.astype(np.float32)
            self.delay_idx = 0

        delay_line = self.delay_line
        n = len(delay_line)
        out = np.empty(num_samples, dtype=np.float32)

        # Vectorized processing: a sample written at position i is only read
        # again one full period (N samples) later, so chunks of up to N - 1
        # samples have no feedback within them and can be computed with pure
        # numpy operations instead of a per-sample Python loop. This matters
        # because we run inside the real-time audio callback.
        produced = 0
        while produced < num_samples:
            chunk = min(num_samples - produced, n - 1)

            # Read chunk + 1 consecutive samples from the circular buffer
            read_indices = np.arange(self.delay_idx, self.delay_idx + chunk + 1) % n
            reads = delay_line[read_indices]

            # Output the current delay line content
            out[produced:produced + chunk] = reads[:-1]

            # Feedback: replace each sample with the damped average of itself
            # and its neighbour (the low-pass filter at the heart of the algorithm)
            delay_line[read_indices[:-1]] = self.damping * 0.5 * (reads[:-1] + reads[1:])

            self.delay_idx = (self.delay_idx + chunk) % n
            produced += chunk

        return out

