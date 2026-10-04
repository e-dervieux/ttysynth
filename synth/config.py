"""
Configuration: config.ini is read and validated once, at startup, into
frozen dataclasses handed to the components.

Validation is strict: config.ini is the only source of settings (no default
values in the code), so every option must be present, and unknown sections
or options are errors (most likely typos). Any problem raises a ConfigError
naming the section and option.
"""

# Global imports
import configparser
from dataclasses import dataclass
from pathlib import Path

# Local imports
from synth.errors import ConfigError
from synth.keymap import LAYOUT_NAMES, normalize_key

# Default configuration file, at the project root (not relative to the current
# directory, so that the synthesizer can be started from anywhere)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.ini'

# Controls: {name, as in the [CONTROLS] section (lowercase here): label}.
# The TUI lists them in this order; Synthesizer maps each name to its action.
CONTROLS = {
    'volume_up': 'volume up',
    'volume_down': 'volume down',
    'instrument_next': 'next instrument',
    'layout_next': 'next layout',
    'octave_down': 'octave down',
    'octave_up': 'octave up',
    'record_start': 'record start',
    'record_stop': 'record stop',
    'quit': 'quit',
}

# Expected options of each section (configparser lowercases option names)
_SCHEMA = {
    'AUDIO': ('sample_rate', 'buffer_size', 'channels', 'default_volume', 'tuning_freq'),
    'KEYBOARD': ('layout', 'custom_layout', 'octave_shift'),
    'CONTROLS': tuple(CONTROLS),
}


@dataclass(frozen=True)
class AudioConfig:
    """[AUDIO] section"""
    sample_rate: int       # Hz
    buffer_size: int       # samples
    channels: int
    default_volume: float  # 0.0 to 1.0
    tuning_freq: float     # Hz, frequency of A4


@dataclass(frozen=True)
class KeyboardConfig:
    """[KEYBOARD] section"""
    layout: str            # Initial layout, one of keymap.LAYOUT_NAMES
    custom_layout: Path    # Custom layout file, resolved relative to config.ini
    octave_shift: int      # Initial octave shift


@dataclass(frozen=True)
class Config:
    """Whole configuration"""
    audio: AudioConfig
    keyboard: KeyboardConfig
    controls: dict         # {control name: normalized key}, see CONTROLS


def _where(section: str, option: str) -> str:
    """Setting name for error messages, e.g. '[AUDIO] SAMPLE_RATE'"""
    return f"[{section}] {option.upper()}"


def _get(parser, section: str, option: str, convert=str, valid=lambda value: True,
         expected: str = ''):
    """
    Value of an option, converted (e.g. int) and checked with `valid`.
    `expected` describes a valid value, for the error message.
    """
    value = parser[section][option]
    try:
        result = convert(value)
    except ValueError:
        result = None
    if result is None or not valid(result):
        raise ConfigError(f"{_where(section, option)} = '{value}': expected {expected}")
    return result


def _positive(value) -> bool:
    return value > 0


def _check_schema(parser: configparser.ConfigParser, path: Path):
    """Every expected section and option present, nothing else"""
    if parser.defaults():
        raise ConfigError(f"{path.name}: [DEFAULT] section not supported")
    for section in parser.sections():
        if section not in _SCHEMA:
            raise ConfigError(f"{path.name}: unknown section [{section}] "
                              f"(expected: {', '.join(f'[{s}]' for s in _SCHEMA)})")
    for section, options in _SCHEMA.items():
        if not parser.has_section(section):
            raise ConfigError(f"{path.name}: missing section [{section}]")
        for option in parser[section]:
            if option not in options:
                raise ConfigError(f"{path.name}: unknown option {_where(section, option)}")
        for option in options:
            if option not in parser[section]:
                raise ConfigError(f"{path.name}: missing option {_where(section, option)}")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """
    Read and validate a configuration file.

    Raises:
        ConfigError: file unreadable or malformed, missing or unknown section
                     or option, invalid value
    """
    # No interpolation: '%' is a valid key
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # open() rather than parser.read(), which silently skips missing files
        with open(path, encoding='utf-8') as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        raise ConfigError(f"Cannot read configuration: {e}") from e
    _check_schema(parser, path)

    audio = AudioConfig(
        sample_rate=_get(parser, 'AUDIO', 'sample_rate', int, _positive, 'a positive integer'),
        buffer_size=_get(parser, 'AUDIO', 'buffer_size', int, _positive, 'a positive integer'),
        channels=_get(parser, 'AUDIO', 'channels', int, _positive, 'a positive integer'),
        default_volume=_get(parser, 'AUDIO', 'default_volume', float, lambda v: 0 <= v <= 1,
                            'a number between 0 and 1'),
        tuning_freq=_get(parser, 'AUDIO', 'tuning_freq', float, _positive, 'a positive number'),
    )

    keyboard = KeyboardConfig(
        layout=_get(parser, 'KEYBOARD', 'layout', valid=lambda v: v in LAYOUT_NAMES,
                    expected=f"one of {', '.join(LAYOUT_NAMES)}"),
        custom_layout=path.parent / _get(parser, 'KEYBOARD', 'custom_layout', valid=bool,
                                         expected='a file name'),
        # Its valid range depends on the layout: checked by the keyboard handler
        octave_shift=_get(parser, 'KEYBOARD', 'octave_shift', int, expected='an integer'),
    )

    controls = {name: normalize_key(parser['CONTROLS'][name], _where('CONTROLS', name))
                for name in CONTROLS}

    return Config(audio=audio, keyboard=keyboard, controls=controls)
