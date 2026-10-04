"""
Musical aspects module - handles note names, MIDI numbers, frequencies

Notes are identified by their MIDI number (C4 = 60, A4 = 69): integers make
transposition and comparison trivial, and are converted to a name for
display or to a frequency for synthesis.

Temperaments
------------
Twelve pure fifths (frequency ratio 3/2) overshoot seven octaves by the
Pythagorean comma (about 23.46 cents): the circle of fifths cannot close
with pure fifths only. A temperament is a choice of how to spread that comma
over the 12 fifths, trading the purity of some intervals for others:

- Equal (modern): every fifth narrowed by 1/12 comma (exactly 700 cents).
  All keys sound the same; only octaves are pure.
- Pythagorean: 11 pure fifths, the 12th (G#-Eb) takes the whole comma (the
  "wolf" fifth, unusable). Pure fifths and fourths, wide major thirds
  (408 cents instead of 386 for a pure 5/4).
- Quarter-comma meantone (described by Pietro Aron, 1523): 11 fifths
  narrowed by 1/4 syntonic comma, so that four of them stacked make a pure
  major third (5/4). Sweet thirds in common keys, wolf G#-Eb.
- Werckmeister I (III), "correct temperament" (Andreas Werckmeister, 1691):
  the fifths C-G, G-D, D-A and B-F# narrowed by 1/4 Pythagorean comma, the
  other 8 pure. No wolf: every key is usable, each with its own colour.
  Werckmeister numbered it I among his "correct temperaments"; it is also
  known as III, the monochord numbering where just intonation is I and
  quarter-comma meantone II.

Each temperament is defined here by the sizes of its fifths, from which the
note offsets are computed. The historical temperaments are built on C, and
A4 stays at the tuning frequency in all of them (as when tuning a harpsichord
from an A).

References:
- Pythagorean comma: https://en.wikipedia.org/wiki/Pythagorean_comma
- Syntonic comma: https://en.wikipedia.org/wiki/Syntonic_comma
- Equal temperament: https://en.wikipedia.org/wiki/12_equal_temperament
- Pythagorean tuning: https://en.wikipedia.org/wiki/Pythagorean_tuning
- Quarter-comma meantone: https://en.wikipedia.org/wiki/Quarter-comma_meantone
- Werckmeister temperaments: https://en.wikipedia.org/wiki/Werckmeister_temperament
  (A. Werckmeister, Musicalische Temperatur, 1691; Orgel-Probe, 1681)
- J. M. Barbour, Tuning and Temperament: A Historical Survey, 1951
"""

# Global imports
import logging
import math
import re

log = logging.getLogger(__name__)

# Semitone offsets of the natural notes from C, and of the accidentals
_NATURAL_OFFSETS = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
_ACCIDENTAL_OFFSETS = {'': 0, '#': 1, 'b': -1}

# Letter, optional accidental, octave. Lowercase letters are accepted since
# configparser lowercases option names (e.g. 'db4').
_NOTE_RE = re.compile(r'([A-Ga-g])([#b]?)(-?\d+)')

# Display names (sharps only) of the 12 semitones of an octave
_SEMITONE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# MIDI number of A4, the tuning reference
_A4_MIDI = 69

# Playable range: the MIDI range, C-1 (8 Hz) to G9 (12.5 kHz at A4 = 440 Hz)
MIDI_MIN, MIDI_MAX = 0, 127

# Interval sizes in cents (1200 cents = one octave)
_PURE_FIFTH = 1200 * math.log2(3 / 2)                     # 701.955
_PYTHAGOREAN_COMMA = 1200 * math.log2(3 ** 12 / 2 ** 19)  # 23.460: 12 pure fifths - 7 octaves
_SYNTONIC_COMMA = 1200 * math.log2(81 / 80)               # 21.506: 4 pure fifths - 2 octaves - pure third


def _offsets_from_fifths(fifths: list) -> tuple:
    """
    Note offsets of a temperament, in cents from C (index 0 = C ... 11 = B),
    from the sizes of the 11 fifths of the chain Eb-Bb-F-C-G-D-A-E-B-F#-C#-G#.
    The 12th fifth, G#-Eb, closes the circle with what remains.
    """
    pitch_class = 3  # Eb
    cents = {pitch_class: 0.0}
    for fifth in fifths:
        next_pitch_class = (pitch_class + 7) % 12
        cents[next_pitch_class] = cents[pitch_class] + fifth
        pitch_class = next_pitch_class
    return tuple((cents[i] - cents[0]) % 1200 for i in range(12))


_TEMPERED = _PURE_FIFTH - _PYTHAGOREAN_COMMA / 4  # Werckmeister's narrowed fifth

# {name: note offsets in cents from C}, see the module docstring. Order
# matters: the temperament-next control key cycles through this dict.
# TODO: TEMPERAMENT_ROOT option, to build the historical temperaments on
#       another key than C (a rotation of the offsets)
TEMPERAMENTS = {
    # 700 cents: the pure fifth narrowed by 1/12 Pythagorean comma
    'equal': _offsets_from_fifths([700.0] * 11),
    'pythagorean': _offsets_from_fifths([_PURE_FIFTH] * 11),
    'meantone': _offsets_from_fifths([_PURE_FIFTH - _SYNTONIC_COMMA / 4] * 11),
    # Fifths Eb-Bb, Bb-F, F-C pure; C-G, G-D, D-A narrowed; A-E, E-B pure;
    # B-F# narrowed; F#-C#, C#-G# (and G#-Eb) pure
    'werckmeister': _offsets_from_fifths([_PURE_FIFTH] * 3 + [_TEMPERED] * 3 + [_PURE_FIFTH] * 2
                                         + [_TEMPERED] + [_PURE_FIFTH] * 2),
}


def note_to_midi(note_name: str) -> int:
    """
    Convert a note name to its MIDI number: 'A4' -> 69.
    Sharps and flats are accepted ('C#4' and 'Db4' -> 61), including across
    octave boundaries ('Cb4' is B3 -> 59, 'B#3' is C4 -> 60).

    Raises:
        ValueError: if the note name is invalid
    """
    match = _NOTE_RE.fullmatch(note_name.strip())
    if match is None:
        raise ValueError(f"Invalid note name: '{note_name}'")
    letter, accidental, octave = match.groups()
    # MIDI octaves start at C-1 = 0
    return (int(octave) + 1) * 12 + _NATURAL_OFFSETS[letter.upper()] + _ACCIDENTAL_OFFSETS[accidental]


def midi_to_note(midi: int) -> str:
    """Convert a MIDI number to a note name, spelled with sharps: 61 -> 'C#4'"""
    octave, semitone = divmod(midi, 12)
    return f"{_SEMITONE_NAMES[semitone]}{octave - 1}"


class MusicEngine:
    """Handles pure musical calculations - notes to frequencies"""

    def __init__(self, tuning_freq: float, temperament: str):
        """
        Initialize music engine

        Args:
            tuning_freq: frequency of A4 in Hz
            temperament: one of TEMPERAMENTS
        """
        self.tuning_freq = tuning_freq
        self.temperament = temperament

    @staticmethod
    def _cents(midi: int, offsets: tuple) -> float:
        """Pitch of a MIDI note in cents from C-1, in a temperament"""
        octave, pitch_class = divmod(midi, 12)
        return 1200 * octave + offsets[pitch_class]

    def midi_to_frequency(self, midi: int) -> float:
        """
        Frequency in Hz of a MIDI note in the current temperament, A4 being
        the reference: f = f_A4 * 2^((cents(note) - cents(A4)) / 1200)
        """
        offsets = TEMPERAMENTS[self.temperament]
        cents = self._cents(midi, offsets) - self._cents(_A4_MIDI, offsets)
        return self.tuning_freq * 2 ** (cents / 1200)

    def next_temperament(self):
        """Switch to the next temperament (notes already playing keep their
        frequency)"""
        names = list(TEMPERAMENTS)
        self.temperament = names[(names.index(self.temperament) + 1) % len(names)]
        log.info(f"Temperament: {self.temperament}")
