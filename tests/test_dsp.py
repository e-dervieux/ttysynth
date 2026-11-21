"""Sound synthesis: ADSR envelope, instruments (synth/dsp)"""

import copy
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from synth.dsp import Flute, Lead, Note, PluckedString
from synth.dsp.instruments import Instrument
from synth.dsp.note import Phase

SAMPLE_RATE = 48000
BLOCK = 64


class Constant(Instrument):
    """Outputs 1.0: a note's output is then its envelope"""

    def __init__(self, sample_rate, attack=0.01, decay=0.02, sustain=0.5, release=0.03):
        super().__init__(sample_rate)
        self.attack_time, self.decay_time = attack, decay
        self.sustain_level, self.release_time = sustain, release

    def generate_audio(self, frequency, num_samples, t_start):
        return np.ones(num_samples, dtype=np.float32)


def render(note, seconds=None, until_ended=False, block=BLOCK):
    """Render a note in buffers, for a duration or until it ends"""
    buffers = []
    count = 0
    while (until_ended and not note.ended) or (seconds is not None and count * block < seconds * SAMPLE_RATE):
        buffers.append(note.get_next_buffer(block))
        count += 1
        assert count < 10 * SAMPLE_RATE, "note never ended"
    return np.concatenate(buffers) if buffers else np.zeros(0)


def test_envelope_shape():
    note = Note(440.0, Constant(SAMPLE_RATE))
    held = render(note, seconds=0.1)
    attack, decay = int(0.01 * SAMPLE_RATE), int(0.02 * SAMPLE_RATE)
    assert held[0] == 0.0                                      # Attack starts from silence
    assert held[attack] == pytest.approx(1.0)                  # Peak: first sample of the decay
    assert held[attack + decay:] == pytest.approx(0.5)         # Sustain level
    assert note.phase == Phase.SUSTAIN

    note.release()
    released = render(note, until_ended=True)
    assert released[0] == pytest.approx(0.5, abs=1e-3)         # Release starts from the sustain level
    assert len(released) <= int(0.03 * SAMPLE_RATE) + BLOCK
    assert note.get_next_buffer(BLOCK) == pytest.approx(0.0)  # Silent once ended


@pytest.mark.parametrize('release_after', [0.002, 0.015, 0.1])  # During attack, decay, sustain
def test_envelope_is_continuous(release_after):
    """No jump (click), whenever the key is released"""
    note = Note(440.0, Constant(SAMPLE_RATE))
    held = render(note, seconds=release_after)
    note.release()
    envelope = np.concatenate([held, render(note, until_ended=True)])
    max_step = 1.0 / int(0.01 * SAMPLE_RATE)  # Steepest slope: the attack
    assert np.max(np.abs(np.diff(envelope))) <= max_step * 1.01


def test_envelope_with_odd_buffer_sizes():
    """Phase boundaries falling inside buffers"""
    reference = Note(440.0, Constant(SAMPLE_RATE))
    other = Note(440.0, Constant(SAMPLE_RATE))
    assert render(other, seconds=0.06, block=100)[:2880] == pytest.approx(render(reference, seconds=0.06)[:2880])


def test_release_after_sustain_starts_exactly_at_the_sustain_level():
    """Decay ending exactly on a buffer boundary (1440 samples = 30 x 48):
    the sustain fast path then never computes an envelope value"""
    note = Note(440.0, Constant(SAMPLE_RATE))
    render(note, seconds=0.1, block=48)
    note.release()
    assert note.get_next_buffer(48)[0] == pytest.approx(0.5, abs=1e-7)


def test_flute_matches_the_additive_formula():
    """sum of weight x sin(k x angle), normalized by the sum of the weights"""
    flute = Flute(SAMPLE_RATE)
    t = np.arange(10_000, 10_000 + 4800) / SAMPLE_RATE
    angle = 2 * np.pi * 261.6 * t
    expected = sum(w * np.sin(k * angle) for k, w in enumerate(flute.harmonics, 1)) / sum(flute.harmonics)
    assert flute.generate_audio(261.6, 4800, 10_000) == pytest.approx(expected, abs=1e-6)


def test_flute_stays_within_full_scale():
    note = Note(440.0, Flute(SAMPLE_RATE))
    assert np.max(np.abs(render(note, seconds=0.5))) <= 1.0 + 1e-6


def measured_pitch(signal, expected):
    """Frequency of the strongest FFT peak within a semitone of `expected`
    (a plucked string is rich in harmonics, which may be stronger than the
    fundamental), refined by parabolic interpolation"""
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    bin_width = SAMPLE_RATE / len(signal)
    low, high = (int(expected * 2 ** (sign / 12) / bin_width) for sign in (-1, 1))
    peak = low + int(np.argmax(spectrum[low:high + 1]))
    left, center, right = np.log(spectrum[peak - 1:peak + 2])
    offset = 0.5 * (left - right) / (left - 2 * center + right)
    return (peak + offset) * SAMPLE_RATE / len(signal)


def spectrum(signal):
    """Amplitude spectrum of one second of signal (1 Hz bins)"""
    return np.abs(np.fft.rfft(signal * np.blackman(len(signal)))) / (0.42 * len(signal) / 2)


@pytest.mark.parametrize('frequency', [262, 1046])  # Whole Hz: harmonics on FFT bins
def test_lead_is_band_limited(frequency):
    """Every harmonic up to the cutoff, weighted k ** -SLOPE; nothing above
    it (no aliasing)"""
    amplitudes = spectrum(Lead(SAMPLE_RATE).generate_audio(frequency, SAMPLE_RATE, 0))
    count = min(Lead.MAX_HARMONICS, int(Lead.CUTOFF / frequency))
    harmonics = [amplitudes[k * frequency] for k in range(1, count + 1)]
    assert harmonics == pytest.approx([harmonics[0] * k ** -Lead.SLOPE for k in range(1, count + 1)],
                                      rel=0.02)
    assert amplitudes[count * frequency + 100:].max() < 1e-4 * harmonics[0]


@pytest.mark.parametrize('frequency', [65.4, 261.6, 1046.5, 12543.9])  # C2 (capped) to G9
def test_lead_peaks_at_full_scale(frequency):
    signal = Lead(SAMPLE_RATE).generate_audio(frequency, SAMPLE_RATE // 10, 0)
    assert 0.98 < np.max(np.abs(signal)) <= 1.0 + 1e-3


def test_lead_harmonic_count_is_capped():
    lead = Lead(SAMPLE_RATE)
    lead.generate_audio(32.7, 64, 0)  # C1: 489 harmonics below the cutoff
    assert len(lead._harmonic_numbers) == Lead.MAX_HARMONICS


def test_lead_stays_below_nyquist_at_low_sample_rates():
    lead = Lead(16000)
    lead.generate_audio(1000.0, 64, 0)
    assert lead._harmonic_numbers.max() * 1000.0 < 8000


@pytest.mark.parametrize('frequency', [110.0, 220.0, 440.0])
def test_plucked_string_pitch(frequency):
    """Karplus-Strong: within a few cents (delay line length quantization)"""
    signal = PluckedString(SAMPLE_RATE).generate_audio(frequency, SAMPLE_RATE, 0)
    cents = 1200 * math.log2(measured_pitch(signal, frequency) / frequency)
    assert abs(cents) < 10


def test_plucked_string_has_no_dc_and_decays():
    signal = PluckedString(SAMPLE_RATE).generate_audio(220.0, 2 * SAMPLE_RATE, 0)
    assert abs(np.mean(signal)) < 0.01
    first, last = signal[:SAMPLE_RATE // 10], signal[-SAMPLE_RATE // 10:]
    assert np.sqrt(np.mean(last ** 2)) < 0.5 * np.sqrt(np.mean(first ** 2))


def test_plucked_string_buffer_size_does_not_change_the_sound():
    """The vectorized delay line must give the same samples for any buffer size"""
    string = PluckedString(SAMPLE_RATE)
    string.generate_audio(440.0, 0, 0)  # Creates the (random) noise burst
    twin = copy.deepcopy(string)
    reference = string.generate_audio(440.0, 4800, 0)
    chunks, produced = [], 0
    for size in [1, 37, 64, 109, 110, 111, 500] * 20:
        size = min(size, 4800 - produced)
        if size == 0:
            break
        chunks.append(twin.generate_audio(440.0, size, produced))
        produced += size
    assert np.array_equal(np.concatenate(chunks), reference)


def test_numpy_random_is_loaded_at_import():
    """numpy loads its random module on first use: it must not be inside the
    audio callback, on the first plucked note (several ms, xruns). Checked
    in a fresh interpreter: this test session has already loaded it."""
    code = "import sys, synth.dsp; assert 'numpy.random' in sys.modules"
    subprocess.run([sys.executable, '-c', code], check=True, cwd=Path(__file__).parents[1])
