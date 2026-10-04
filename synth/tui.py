"""
htop-like terminal user interface using rich.

The TUI runs in the main (UI) process and renders on the terminal's alternate
screen buffer (like htop: the normal scrollback is restored on exit). It is
refreshed at ~10 Hz; thanks to the two-process architecture, rendering time
can never delay the audio callback (separate interpreter, separate GIL).

Log records are displayed in a scrolling panel, from the log history (see
logging_setup.py); their console output is paused while the TUI is shown.
"""

# Global imports
import time

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Local imports
from synth.config import CONTROLS
from synth.logging_setup import console_paused, format_record, log_history
from synth.music import midi_to_note

REFRESH_PERIOD = 0.1  # seconds between renders (~10 Hz)

# Keymap panel grid widths: notes and controls per row
NOTE_COLUMNS = 6
CONTROL_COLUMNS = 5


def _rows(count: int, columns: int) -> int:
    """Number of grid rows needed for `count` cells"""
    return (count + columns - 1) // columns


def _grid(cells: list, columns: int) -> Table:
    """Table grid of `cells`, `columns` per row"""
    table = Table.grid(padding=(0, 2))
    for row_start in range(0, len(cells), columns):
        row = cells[row_start:row_start + columns]
        table.add_row(*row, *[""] * (columns - len(row)))
    return table


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

    def _render_status(self) -> Panel:
        """Top status bar: stream parameters and live audio process status"""
        engine = self.synth.audio_engine
        music = self.synth.music_engine
        status = engine.get_status()

        buffer_ms = engine.buffer_duration * 1000.0
        latency_ms = status.get('latency_ms')
        latency_str = f"{latency_ms:.1f} ms" if latency_ms is not None else "n/a"
        xruns = status.get('xruns', 0)
        voices = status.get('voices', 0)

        line1 = Text.assemble(
            ("fs: ", "bold"), f"{engine.sample_rate} Hz   ",
            ("buffer: ", "bold"), f"{engine.buffer_size} ({buffer_ms:.1f} ms)   ",
            ("latency: ", "bold"), f"{latency_str}   ",
            ("xruns: ", "bold"), (f"{xruns}", "red" if xruns else "green"), "   ",
            ("tuning: ", "bold"), f"A4 = {music.tuning_freq:g} Hz, ", (music.temperament, "cyan"),
        )

        handler = self.synth.keyboard_handler
        capture = handler.capture_status
        notes = [midi_to_note(midi) for midi in sorted(handler.get_pressed_keys().values())]
        line2 = Text.assemble(
            ("volume: ", "bold"), f"{engine.get_volume() * 100:.0f}%   ",
            ("instrument: ", "bold"), (self.synth.current_instrument, "cyan"), "   ",
            ("layout: ", "bold"), (handler.layout, "cyan"), "   ",
            ("octave: ", "bold"), f"{handler.octave_shift:+d}   ",
            ("voices: ", "bold"), f"{voices}   ",
            ("keyboard: ", "bold"), (capture, "green" if capture.startswith('OK') else "bold red"), "   ",
            ("notes: ", "bold"), (" ".join(notes) if notes else "-", "magenta"),
        )

        body = Text()
        body.append_text(line1)
        body.append("\n")
        body.append_text(line2)
        return Panel(body, title="Python Command Line Audio Synthesizer", border_style="blue")

    def _render_keymap(self) -> Panel:
        """Middle panel: note keys and control keys"""
        handler = self.synth.keyboard_handler

        # Sorted by pitch: custom layout files may list notes in any order
        notes = sorted(((key, handler.note_for_key(key)) for key in handler.key_to_midi),
                       key=lambda item: item[1])
        note_cells = [Text.assemble((key, "bold cyan"), " -> ", (midi_to_note(midi), "magenta"))
                      for key, midi in notes]
        control_cells = [Text.assemble((handler.control_keys[name], "bold cyan"), f" {label}")
                         for name, label in CONTROLS.items()]

        grid = Table.grid()
        grid.add_row(_grid(note_cells, NOTE_COLUMNS))
        grid.add_row(Text())  # spacer
        grid.add_row(_grid(control_cells, CONTROL_COLUMNS))
        return Panel(grid, title="Keys", border_style="blue")

    def _render_logs(self, max_lines: int) -> Panel:
        """Bottom panel: most recent log lines"""
        text = Text()
        for record in log_history()[-max_lines:]:
            text.append_text(format_record(record))
            text.append("\n")
        return Panel(text, title="Log", border_style="blue")

    def _render(self) -> Layout:
        """Build the full layout"""
        key_count = len(self.synth.keyboard_handler.key_to_midi)
        # note rows + spacer + control rows + borders
        keymap_height = (_rows(key_count, NOTE_COLUMNS) + 1
                         + _rows(len(CONTROLS), CONTROL_COLUMNS) + 2)

        layout = Layout()
        layout.split_column(
            Layout(self._render_status(), name="status", size=4),
            Layout(self._render_keymap(), name="keys", size=keymap_height),
            Layout(self._render_logs(max_lines=30), name="logs"),
        )
        return layout

    def run(self):
        """
        Run the TUI loop (blocking) until the synthesizer stops. Log records
        are only shown in the log panel meanwhile.
        """
        with console_paused():
            with Live(self._render(), screen=True, refresh_per_second=1 / REFRESH_PERIOD,
                      redirect_stderr=False) as live:
                # The kitty keyboard protocol flag stacks are independent for
                # the main and alternate screens: re-enable now that Live has
                # switched to the alternate screen, and disable before leaving.
                self.synth.keyboard_handler.on_alt_screen_enter()
                try:
                    while self.synth.running:
                        live.update(self._render())
                        time.sleep(REFRESH_PERIOD)
                finally:
                    self.synth.keyboard_handler.on_alt_screen_exit()
