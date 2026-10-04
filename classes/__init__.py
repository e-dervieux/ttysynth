"""
Classes subpackage for synthesizer components
"""

from .instrument import Instrument, Flute, PluckedString
from .note import Note
from utils.constants import ATTACK, DECAY, SUSTAIN, RELEASE

# Registry of available instruments, used for instrument switching.
# Order matters: the instrument-next control key cycles through this dict.
INSTRUMENTS = {
    'flute': Flute,
    'pluck': PluckedString,
}

__all__ = ['Instrument', 'Flute', 'PluckedString', 'Note', 'INSTRUMENTS',
           'ATTACK', 'DECAY', 'SUSTAIN', 'RELEASE']
