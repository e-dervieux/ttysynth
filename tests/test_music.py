"""Note names, MIDI numbers, frequencies and temperaments (synth/music.py)"""

import math

import pytest

from synth.music import MIDI_MAX, MIDI_MIN, TEMPERAMENTS, MusicEngine, midi_to_note, note_to_midi

PURE_FIFTH = 1200 * math.log2(3 / 2)
PURE_MAJOR_THIRD = 1200 * math.log2(5 / 4)
PYTHAGOREAN_COMMA = 1200 * math.log2(3 ** 12 / 2 ** 19)

C, E, G, G_SHARP, A, E_FLAT = 0, 4, 7, 8, 9, 3


@pytest.mark.parametrize('name, midi', [
    ('A4', 69), ('C4', 60), ('C#4', 61), ('Db4', 61), ('db4', 61),
    ('Cb4', 59), ('B#3', 60), ('E#4', 65), ('Fb4', 64), ('bb3', 58),
    ('C-1', MIDI_MIN), ('G9', MIDI_MAX), (' G9 ', MIDI_MAX),
])
def test_note_to_midi(name, midi):
    assert note_to_midi(name) == midi


@pytest.mark.parametrize('name', ['H4', 'C', '4', 'C##4', 'Cx4', ''])
def test_note_to_midi_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        note_to_midi(name)


def test_midi_to_note_round_trip():
    assert midi_to_note(61) == 'C#4'
    for midi in range(MIDI_MIN, MIDI_MAX + 1):
        assert note_to_midi(midi_to_note(midi)) == midi


@pytest.mark.parametrize('temperament', TEMPERAMENTS)
def test_a4_is_the_tuning_frequency_in_every_temperament(temperament):
    assert MusicEngine(415.0, temperament).midi_to_frequency(69) == pytest.approx(415.0)


@pytest.mark.parametrize('temperament', TEMPERAMENTS)
def test_octaves_are_pure_in_every_temperament(temperament):
    music = MusicEngine(440.0, temperament)
    for midi in range(MIDI_MIN, MIDI_MAX - 11):
        assert music.midi_to_frequency(midi + 12) == pytest.approx(2 * music.midi_to_frequency(midi))


def test_equal_temperament():
    assert TEMPERAMENTS['equal'] == pytest.approx([100.0 * i for i in range(12)])
    assert MusicEngine(440.0, 'equal').midi_to_frequency(60) == pytest.approx(261.6256, abs=1e-4)


def test_pythagorean_tuning():
    offsets = TEMPERAMENTS['pythagorean']
    assert offsets[G] - offsets[C] == pytest.approx(PURE_FIFTH)
    assert offsets[E] - offsets[C] == pytest.approx(407.820, abs=1e-3)  # Wide third
    # The wolf fifth G#-Eb takes the whole comma
    assert offsets[E_FLAT] + 1200 - offsets[G_SHARP] == pytest.approx(PURE_FIFTH - PYTHAGOREAN_COMMA)
    # A is 27/16 above C
    assert MusicEngine(440.0, 'pythagorean').midi_to_frequency(60) == pytest.approx(440 * 16 / 27)


def test_quarter_comma_meantone_has_pure_major_thirds():
    offsets = TEMPERAMENTS['meantone']
    for low in (C, G):  # C-E and G-B
        assert offsets[low + E] - offsets[low] == pytest.approx(PURE_MAJOR_THIRD)


def test_werckmeister_matches_the_published_table():
    # Werckmeister I (III), cents from C (Wikipedia, Werckmeister temperament)
    published = [0, 90.225, 192.180, 294.135, 390.225, 498.045,
                 588.270, 696.090, 792.180, 888.270, 996.090, 1092.180]
    assert TEMPERAMENTS['werckmeister'] == pytest.approx(published, abs=1e-3)


def test_next_temperament_cycles_through_all():
    music = MusicEngine(440.0, 'equal')
    seen = []
    for _ in TEMPERAMENTS:
        music.next_temperament()
        seen.append(music.temperament)
    assert seen[-1] == 'equal'
    assert sorted(seen) == sorted(TEMPERAMENTS)
