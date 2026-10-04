# -*- coding: utf-8 -*-
"""
Logger helper module.

Thread-safe, single-process logging. Messages are printed to stdout by default;
a "sink" callable can be registered (e.g. by the TUI) to capture log lines
instead of printing them, so the alternate-screen interface stays clean.

Note: the audio process does NOT use this module's print path directly: it
forwards its log messages to the main process through the status queue (see
audio_engine.py), so that a single process owns all terminal output.
"""

# Global imports
import sys
import threading
from collections import deque

import sty

log_level_list = ['[  DEBUG  ]', '[  INFO   ]', '[ WARNING ]', '[  ERROR  ]']

# Module-level state, guarded by _lock
_lock = threading.Lock()
_display_level = 1
_display_level_set = False
_sink = None  # Optional callable(msg: str, level: int), set by the TUI
_history = deque(maxlen=200)  # Recent displayed messages, for late-starting sinks


def get_log_history():
    """Return recent displayed log lines as a list of (msg, level) tuples.
    Used by the TUI to backfill its log panel with pre-startup messages."""
    with _lock:
        return list(_history)


def set_log_sink(sink):
    """
    Register a sink callable that receives (msg, level) instead of printing.
    Pass None to restore printing to stdout.
    """
    global _sink
    with _lock:
        _sink = sink


def log(msg, level=0, display_level=None):
    """
    Logger to help development. Will format the message given as input.

    :param msg: the message to display.
    :param level: the log level, 0 for DEBUG, 1 for INFO, 2 for WARNING, other value for ERROR. -1 will be ignored.
    :param display_level: should be specified only once in main script, all logs below the specified level
                          won't be displayed. E.g. set to '2', all DEBUG and INFO messages will be discarded.
    :return: Nothing (displays a message in console output or sends it to the registered sink).
    """
    global _display_level, _display_level_set

    if level == -1:
        return

    level = log_level_sat(level)

    with _lock:
        if display_level is not None:
            display_level = log_level_sat(display_level)
            if _display_level_set:
                _emit("Log display level should be specified only once! Keeping previously defined one...", 2)
            else:
                _display_level = display_level
                _display_level_set = True
                if _display_level == 0:
                    _emit("All log levels will be displayed.", 1)
                else:
                    # Plain level name (no color codes: the message may be
                    # rendered by the TUI, which expects clean text)
                    _emit("Log less important than " + log_level_list[_display_level]
                          + " will not be displayed.", 1)
                    if _display_level > 2:
                        _emit("This could lead to missed warnings and / or hazardous behavior, beware...", 2)

        if level >= _display_level:
            _emit(msg, level)


def confirm_action(action: str, log_level: int):
    """
    Prompts the user to confirm doing something. Exits execution otherwise.

    :param action: a string describing the action we are about to perform.
    :param log_level: how critic this action is?
    :return: 0 if we continue, exits otherwise.
    """

    log_level = log_level_sat(log_level)

    print_message("You are about to " + action + "!", log_level)
    confirm_str = input("Continue? (Yes/No)\n").upper()
    if confirm_str in ['Y', 'YES']:
        return 0
    else:
        print("Wise decision... Abort mission...")
        sys.exit(0)


########################################################################################################################
# INTERNAL FUNCTIONS, just leave them alone...
########################################################################################################################

def _emit(message, level):
    """Send a log line to the registered sink, or print it. Caller must hold _lock."""
    _history.append((message, level))
    if _sink is not None:
        _sink(message, level)
    else:
        print_message(message, level)


def form_message(message, level): return str_color(log_level_list[level] + ' ' + message, level)


def print_message(message, level): return print(form_message(message, level))


def str_color(message, level):
    """
    Coloring a string.

    :param message: message that cill be colored.
    :param level: a log level which maps to white (0, 1), yellow (2) or red (3)
    :return:
    """

    log_level_styles = ['', '', sty.fg.yellow, sty.fg.red]

    return log_level_styles[level] + message + sty.fg.rs


def log_level_sat(level: int):
    """
    :param level: any number.
    :return: an int between 0 and three (values other than 0, 1 and 2 gives 3)
    """
    if not isinstance(level, int):
        log("Wrong log level type, should be an int !", 3)
        level = 3

    if level > 2 or level < 0:
        level = 3

    return level
