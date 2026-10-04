"""
Exceptions for expected failures.

They are caught by main() and reported to the user as a plain message,
without traceback. Any other exception is a bug, and keeps its traceback.
"""


class SynthError(Exception):
    """Expected failure (invalid configuration, audio device or keyboard
    input unavailable...): reported to the user as a message"""


class ConfigError(SynthError):
    """Invalid configuration"""
