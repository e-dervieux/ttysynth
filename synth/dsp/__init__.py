"""
Sound synthesis: instruments (sound generation) and notes (envelope, state).
"""

from synth.dsp.instruments import Instrument, Flute, PluckedString
from synth.dsp.note import Note

# Registry of available instruments, used for instrument switching.
# Order matters: the instrument-next control key cycles through this dict.
INSTRUMENTS = {
    'flute': Flute,
    'pluck': PluckedString,
}

__all__ = ['Instrument', 'Flute', 'PluckedString', 'Note', 'INSTRUMENTS']
