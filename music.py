"""
Musical aspects module - handles note names, MIDI numbers, frequencies

Notes are identified by their MIDI number (C4 = 60, A4 = 69): integers make
transposition and comparison trivial, and are converted to a name for
display or to a frequency for synthesis.
"""

# Global imports
import configparser
import logging
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

    def __init__(self, config_file='config.ini'):
        """
        Initialize music engine

        Args:
            config_file: Path to configuration file
        """
        # Load configuration
        config = configparser.ConfigParser()
        config.read(config_file)

        self.tuning_freq = config.getfloat('AUDIO', 'TUNING_FREQ', fallback=440.0)

    def midi_to_frequency(self, midi: int) -> float:
        """
        Frequency in Hz of a MIDI note, in 12-tone equal temperament
        (other temperaments might be added in the future):
        f = f_A4 * 2^((midi - 69) / 12)
        """
        return self.tuning_freq * 2 ** ((midi - _A4_MIDI) / 12)

    def note_to_frequency(self, note_name: str) -> float:
        """Frequency in Hz of a note name (e.g., 'C4', 'F#5', 'Bb3')"""
        return self.midi_to_frequency(note_to_midi(note_name))

    def set_tuning(self, freq):
        """Set A4 tuning frequency"""
        self.tuning_freq = freq
        log.info(f"Retuned to A4 = {freq} Hz")
