"""
Logging configuration.

Modules log through the standard logging module:
    log = logging.getLogger(__name__)
    log.info("...")

setup_logging() installs two handlers on the root logger:
- console: colored "[  INFO   ] message" lines on stderr, paused while the
  TUI owns the screen (see console_paused)
- history: the most recent records, displayed by the TUI log panel (records
  logged before the TUI starts included)

The audio process forwards its records to the main process through its
status queue (see audio_engine.py).
"""

# Global imports
import logging
from collections import deque
from contextlib import contextmanager

from rich.console import Console
from rich.text import Text

# Number of records kept for the TUI log panel
HISTORY_SIZE = 200

# rich styles of the log levels
LEVEL_STYLES = {
    logging.DEBUG: 'dim',
    logging.INFO: 'default',
    logging.WARNING: 'yellow',
    logging.ERROR: 'red',
    logging.CRITICAL: 'bold red',
}


def format_record(record: logging.LogRecord) -> Text:
    """'[  INFO   ] message' line of a record, styled by level"""
    return Text(f"[{record.levelname:^9}] {record.getMessage()}",
                style=LEVEL_STYLES.get(record.levelno, 'default'))


class _ConsoleHandler(logging.Handler):
    """Writes records to stderr (colored when it is a terminal)"""

    def __init__(self):
        super().__init__()
        self.console = Console(stderr=True, highlight=False)

    def emit(self, record):
        # soft_wrap: no hard line breaks at the console width (log files)
        self.console.print(format_record(record), soft_wrap=True)


class _HistoryHandler(logging.Handler):
    """Keeps the most recent records in memory"""

    def __init__(self):
        super().__init__()
        self.records = deque(maxlen=HISTORY_SIZE)

    def emit(self, record):
        self.records.append(record)


_console_handler = _ConsoleHandler()
_history_handler = _HistoryHandler()


def setup_logging(level: int):
    """Configure the root logger: records of `level` and above go to the
    console and the history"""
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(_console_handler)
    root.addHandler(_history_handler)


def log_history() -> list:
    """Most recent log records, oldest first"""
    with _history_handler.lock:
        return list(_history_handler.records)


@contextmanager
def console_paused():
    """Stop printing records to the console within this block (the history
    keeps them): used by the TUI, whose display would be garbled"""
    root = logging.getLogger()
    root.removeHandler(_console_handler)
    try:
        yield
    finally:
        root.addHandler(_console_handler)
