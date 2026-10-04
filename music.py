"""
Musical aspects module - handles note frequencies, temperament, mappings
"""

# Global imports
import configparser
import re

# Local imports
from logger import log


class MusicEngine:
    """Handles pure musical calculations - note names to frequencies"""
    
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
        
        # Define semitone offsets from C
        self.note_offsets = {
            'C': 0, 'C#': 1, 'D': 2, 'D#': 3, 'E': 4, 'F': 5,
            'F#': 6, 'G': 7, 'G#': 8, 'A': 9, 'A#': 10, 'B': 11
        }
    
    def note_to_frequency(self, note_name):
        """
        Convert a note name (e.g., 'C4', 'F#5') to frequency in Hz
        
        Args:
            note_name: Note name like 'C4', 'F#5' (only natural and sharp notes)
            
        Returns:
            Frequency in Hz
        """
        # Parse note name: letter + optional sharp, then octave number
        # (regex instead of slicing so 'C10' or 'A-1' parse correctly)
        match = re.fullmatch(r'([A-G]#?)(-?\d+)', note_name)
        if match is None:
            raise ValueError(f"Invalid note name: '{note_name}'")
        note_base = match.group(1)  # e.g., 'C#' from 'C#4'
        octave = int(match.group(2))  # e.g., 4 from 'C#4'
        
        # Calculate semitone offset from C4
        # C4 is chosen as reference because A4 (tuning standard) is the tuning standard
        semitone_offset = (octave - 4) * 12 + self.note_offsets[note_base]
        
        # Calculate frequency using A4 as reference (12-tone equal temperament)
        # Formula: f = f_ref * 2^(semitones/12)
        # Note: this is for the 12-tone equal temperament, adding other temperaments might be done in the future
        freq = self.tuning_freq * (2 ** ((semitone_offset - 9) / 12.0))
        return freq
    
    def set_tuning(self, freq):
        """Set A4 tuning frequency"""
        self.tuning_freq = freq
        log(f"Retuned to A4 = {freq} Hz", 1)
