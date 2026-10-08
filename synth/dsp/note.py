"""
Note class for individual note management
"""

# Global imports
from enum import IntEnum

import numpy as np

# Local imports
from synth.dsp.instruments import Instrument


class Phase(IntEnum):
    """ADSR envelope phases (values index Note.phase_duration)"""
    ATTACK = 0
    DECAY = 1
    SUSTAIN = 2
    RELEASE = 3


class Note:
    """Represents a single note with its own state and instrument"""

    def __init__(self, pitch: float, instrument: Instrument, key: str = ''):
        """
        Initialize a Note

        Args:
            pitch: Frequency in Hz
            instrument: Instrument instance to use for sound generation
            key: Keyboard key identifier this note is bound to (used to find
                 the note again when the key is released)
        """
        self.pitch = pitch
        self.instrument = instrument
        self.key = key

        # Time tracking
        self.t_int = 0  # Total samples generated
        self.sample_rate = instrument.sample_rate

        # ADSR state
        self.phase = Phase.ATTACK
        self.phase_start_sample = 0
        self.current_envelope = 0.0  # Track current envelope value for smooth transitions
        self.release_start_envelope = 0.0  # Envelope value when release started
        self.is_released = False
        self.ended = False

        # Read ADSR parameters from instrument
        self.sustain_level = instrument.sustain_level
        self.phase_duration = [
            int(instrument.attack_time * self.sample_rate),   # Phase.ATTACK
            int(instrument.decay_time * self.sample_rate),    # Phase.DECAY
            float('inf'),                                     # Phase.SUSTAIN
            int(instrument.release_time * self.sample_rate)   # Phase.RELEASE
        ]

    def release(self):
        """Trigger the release phase (called when key is released)"""
        if not self.is_released:
            self.is_released = True
            self.phase = Phase.RELEASE
            self.phase_start_sample = self.t_int
            self.release_start_envelope = self.current_envelope  # Start from current value!

    def _calculate_envelope_vectorized(self, phase_progress: np.ndarray) -> np.ndarray:
        """
        Calculate envelope values for an array of progress values (vectorized)

        Args:
            phase_progress: Array of progress values (0.0 to 1.0) within current phase

        Returns:
            Array of envelope values
        """
        if self.phase == Phase.ATTACK:
            return phase_progress

        elif self.phase == Phase.DECAY:
            return 1.0 - (1.0 - self.sustain_level) * phase_progress

        elif self.phase == Phase.SUSTAIN:
            return np.full_like(phase_progress, self.sustain_level)

        elif self.phase == Phase.RELEASE:
            return self.release_start_envelope * (1.0 - phase_progress)

        return np.zeros_like(phase_progress)

    def _transition_to_next_phase(self, sample: int):
        """Transition to the next phase, starting at `sample` (which may lie
        inside the current buffer, not only at its start)"""
        if self.phase == Phase.ATTACK:
            self.phase = Phase.DECAY
            self.phase_start_sample = sample

        elif self.phase == Phase.DECAY:
            self.phase = Phase.SUSTAIN
            self.phase_start_sample = sample
            # Not updated by the sustain fast path: exact from here on
            self.current_envelope = self.sustain_level

        elif self.phase == Phase.RELEASE:
            self.ended = True

    def get_next_buffer(self, num_samples: int) -> np.ndarray:
        """
        Generate the next audio buffer for this note

        Args:
            num_samples: Number of samples to generate

        Returns:
            Audio buffer as numpy array
        """
        if self.ended:
            return np.zeros(num_samples, dtype=np.float32)

        # Generate raw audio from instrument (no envelope applied)
        audio_buffer = self.instrument.generate_audio(self.pitch, num_samples, self.t_int)

        # Fast path for held notes: the sustain envelope is a constant
        if self.phase == Phase.SUSTAIN:
            self.t_int += num_samples
            if self.sustain_level == 1.0:
                return audio_buffer
            return audio_buffer * self.sustain_level

        # Calculate envelope window by looping over phases
        envelope_window = np.zeros(num_samples, dtype=np.float32)
        samples_processed = 0

        while samples_processed < num_samples and not self.ended:
            # How many samples have elapsed in current phase?
            phase_elapsed = self.t_int + samples_processed - self.phase_start_sample

            # How many samples until phase ends?
            phase_duration = self.phase_duration[self.phase]
            samples_remaining_in_phase = phase_duration - phase_elapsed

            # How many samples to process in this iteration?
            samples_remaining_in_buffer = num_samples - samples_processed
            samples_to_process = min(samples_remaining_in_phase, samples_remaining_in_buffer)

            # Handle sustain phase (infinite duration)
            if self.phase == Phase.SUSTAIN:
                samples_to_process = samples_remaining_in_buffer

            # Calculate phase progress for this segment (vectorized)
            start_idx = samples_processed
            end_idx = samples_processed + int(samples_to_process)

            if phase_duration > 0 and phase_duration != float('inf'):
                # Create array of progress values for this segment
                progress_start = phase_elapsed / phase_duration
                progress_end = (phase_elapsed + samples_to_process) / phase_duration
                phase_progress = np.linspace(progress_start, progress_end,
                                            end_idx - start_idx, endpoint=False)
            else:
                # For sustain or zero-duration phases
                phase_progress = np.zeros(end_idx - start_idx)

            # Vectorized envelope calculation for this segment
            envelope_window[start_idx:end_idx] = self._calculate_envelope_vectorized(phase_progress)

            # Update current envelope (last value of segment)
            if end_idx > start_idx:
                self.current_envelope = envelope_window[end_idx - 1]

            samples_processed = end_idx

            # Check if we need to transition to next phase
            if samples_to_process >= samples_remaining_in_phase and self.phase != Phase.SUSTAIN:
                self._transition_to_next_phase(self.t_int + samples_processed)

        # Apply envelope window (the volume is applied to the mix, see
        # audio_engine.py)
        audio_buffer = audio_buffer * envelope_window

        # Update time counter
        self.t_int += num_samples

        return audio_buffer
