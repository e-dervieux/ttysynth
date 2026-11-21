"""
Sound synthesis: instruments (sound generation) and notes (envelope, state).
"""

from synth.dsp.instruments import Instrument, Flute, Lead, PluckedString
from synth.dsp.note import Note

# Registry of available instruments, used for instrument switching.
# Order matters: the instrument-next control key cycles through this dict.
INSTRUMENTS = {
    'flute': Flute,
    'pluck': PluckedString,
    'lead': Lead,
}

__all__ = ['Instrument', 'Flute', 'Lead', 'PluckedString', 'Note', 'INSTRUMENTS']
