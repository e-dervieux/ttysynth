"""
htop-like terminal user interface using rich.

The TUI runs in the main (UI) process and renders on the terminal's alternate
screen buffer (like htop: the normal scrollback is restored on exit). It is
redrawn as soon as the display changes (key pressed or released, control),
at most FRAME_RATE times per second, and otherwise at IDLE_RATE (audio
status, logs). Thanks to the two-process architecture, rendering time can
never delay the audio callback (separate interpreter, separate GIL); within
this process, see app.GIL_SWITCH_INTERVAL.

Each frame is laid out for the current terminal size: the status and keys
panels take the height their content needs at this width, the log panel the
rest. Below a minimum size, a message asks to enlarge the window.

Log records are displayed in the log panel, from the log history (see
logging_setup.py); their console output is paused while the TUI is shown.
"""

# Global imports
import time

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# Local imports
from synth import APP_TITLE
from synth.audio_engine import format_duration
from synth.config import CONTROLS
from synth.logging_setup import console_paused, format_record, log_history
from synth.music import midi_to_note

FRAME_RATE = 30  # Hz, maximum: changes within a frame are drawn together
IDLE_RATE = 4    # Hz, when nothing is played

# Minimum terminal width, and minimum number of log lines shown: below
# either, the window is too small
MIN_WIDTH = 70
MIN_LOG_LINES = 3

FIELD_SEPARATOR = "   "
CELL_SEPARATOR = "  "
PANEL_FRAME = 4  # columns taken by a panel's borders and padding


def _flow(fields: list, width: int) -> Text:
    """Fields packed into lines of at most `width` cells, a field is never
    split across lines"""
    lines = [Text()]
    for field in fields:
        if not lines[-1]:
            lines[-1].append_text(field)
        elif lines[-1].cell_len + len(FIELD_SEPARATOR) + field.cell_len <= width:
            lines[-1].append(FIELD_SEPARATOR)
            lines[-1].append_text(field)
        else:
            lines.append(field.copy())
    return Text("\n").join(lines)


def _columns(cells: list, width: int) -> Text:
    """Cells in as many equal-width columns as fit in `width`, row by row
    (much cheaper than rich's Columns, which measures every cell again and
    again to find the best layout)"""
    cell_width = max(cell.cell_len for cell in cells)
    count = max(1, (width + len(CELL_SEPARATOR)) // (cell_width + len(CELL_SEPARATOR)))
    rows = []
    for start in range(0, len(cells), count):
        row = Text(CELL_SEPARATOR).join(
            Text.assemble(cell, " " * (cell_width - cell.cell_len)) for cell in cells[start:start + count])
        rows.append(row)
    return Text("\n").join(rows)


def _field(name: str, value, style: str = "") -> Text:
    """'name: value' status field"""
    return Text.assemble((f"{name}: ", "bold"), (str(value), style))


def _height(console: Console, renderable: RenderableType, width: int) -> int:
    """Number of lines `renderable` takes at `width`"""
    return len(console.render_lines(renderable, console.options.update(width=width, height=None)))


class SynthTUI:
    """Full-screen status interface for the synthesizer"""

    def __init__(self, synth):
        """
        Initialize the TUI

        Args:
            synth: the Synthesizer instance to display (provides audio engine
                   status, keyboard mappings, current instrument and volume)
        """
        self.synth = synth

    def _render_status(self, width: int) -> Panel:
        """Top status bar: stream parameters, live audio process status, and
        held notes (on their own line)"""
        engine = self.synth.audio_engine
        music = self.synth.music_engine
        handler = self.synth.keyboard_handler
        status = engine.get_status()

        latency_ms = status.get('latency_ms')
        xruns = status.get('xruns', 0)
        capture = handler.capture_status
        fields = [
            _field("fs", f"{engine.sample_rate} Hz"),
            _field("buffer", f"{engine.buffer_size} ({engine.buffer_duration * 1000.0:.1f} ms)"),
            _field("latency", f"{latency_ms:.1f} ms" if latency_ms is not None else "n/a"),
            _field("xruns", xruns, "red" if xruns else "green"),
            Text.assemble(("tuning: ", "bold"), f"A4 = {music.tuning_freq:g} Hz, ",
                          (music.temperament, "cyan")),
            _field("volume", f"{engine.get_volume() * 100:.0f}%"),
            _field("instrument", self.synth.current_instrument, "cyan"),
            _field("layout", handler.layout, "cyan"),
            _field("octave", f"{handler.octave_shift:+d}"),
            # Two characters wide: the next fields stay in place from 9 to 10 voices
            _field("voices", f"{status.get('voices', 0):2d}"),
            _field("keyboard", capture, "green" if capture.startswith('OK') else "bold red"),
        ]
        recording = status.get('recording')
        if recording is not None:
            fields.insert(0, Text(f"● REC {format_duration(recording)}", "bold red"))
        notes = [midi_to_note(midi) for midi in sorted(handler.get_pressed_keys().values())]

        body = _flow(fields, width - PANEL_FRAME)
        body.append("\n")
        body.append_text(_field("notes", " ".join(notes) if notes else "-", "magenta"))
        return Panel(body, title=APP_TITLE, border_style="blue")

    def _note_cells(self) -> list:
        """'key -> note' cells, sorted by pitch (custom layout files may list
        notes in any order), held keys highlighted"""
        handler = self.synth.keyboard_handler
        held = handler.get_pressed_keys()
        notes = sorted(((key, handler.note_for_key(key)) for key in handler.key_to_midi),
                       key=lambda item: item[1])
        return [Text.assemble((key, "bold cyan"), " -> ", (midi_to_note(midi), "magenta"),
                              style="reverse" if key in held else "")
                for key, midi in notes]

    def _render_keymap(self, width: int) -> Panel:
        """Middle panel: note keys and control keys, in as many columns as
        the width allows"""
        handler = self.synth.keyboard_handler
        control_cells = [Text.assemble((handler.control_keys[name], "bold cyan"), f" {label}")
                         for name, label in CONTROLS.items()]
        inner_width = width - PANEL_FRAME
        return Panel(Group(_columns(self._note_cells(), inner_width),
                           Text(),  # spacer
                           _columns(control_cells, inner_width)),
                     title="Keys", border_style="blue")

    def _render_logs(self, console: Console, width: int, height: int) -> Panel:
        """Bottom panel of `height` lines: the most recent log lines that fit"""
        lines = height - 2  # borders
        # Each record takes at least one line
        text = Text("\n").join(format_record(record) for record in log_history()[-lines:])
        wrapped = text.wrap(console, width - PANEL_FRAME)
        return Panel(Text("\n").join(wrapped[-lines:]), title="Log", border_style="blue",
                     height=height)

    def _render(self, console: Console) -> RenderableType:
        """Build the full screen for the console's current size"""
        width, height = console.size
        if width < MIN_WIDTH:
            return self._render_too_small(width, height, f"{MIN_WIDTH} columns")

        status = self._render_status(width)
        keymap = self._render_keymap(width)
        top_height = _height(console, status, width) + _height(console, keymap, width)
        needed_height = top_height + 2 + MIN_LOG_LINES
        if height < needed_height:
            return self._render_too_small(width, height, f"{needed_height} rows")
        return Group(status, keymap, self._render_logs(console, width, height - top_height))

    @staticmethod
    def _render_too_small(width: int, height: int, needed: str) -> RenderableType:
        """Replaces the TUI when the window is too small for it"""
        message = Text(f"Window too small ({width}×{height}): please enlarge it "
                       f"(at least {needed})", style="bold yellow", justify="center")
        return Align.center(message, vertical="middle", height=height)

    def run(self):
        """
        Run the TUI loop (blocking) until the synthesizer stops. Log records
        are only shown in the log panel meanwhile.
        """
        with console_paused():
            # No auto refresh: rich would refresh from its own thread, out of
            # step with this loop; each render is drawn here, once
            with Live(screen=True, auto_refresh=False, redirect_stderr=False) as live:
                # The kitty keyboard protocol flag stacks are independent for
                # the main and alternate screens: re-enable now that Live has
                # switched to the alternate screen, and disable before leaving.
                self.synth.keyboard_handler.on_alt_screen_enter()
                changed = self.synth.display_changed
                try:
                    while self.synth.running:
                        changed.clear()  # Changes from now on: next frame
                        live.update(self._render(live.console), refresh=True)
                        # Frame rate cap, then wait for a change (drawn at
                        # once) or the next idle refresh
                        time.sleep(1 / FRAME_RATE)
                        changed.wait(1 / IDLE_RATE - 1 / FRAME_RATE)
                finally:
                    self.synth.keyboard_handler.on_alt_screen_exit()
