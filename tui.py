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
from logging_setup import console_paused, format_record, log_history
from music import midi_to_note

REFRESH_PERIOD = 0.1  # seconds between renders (~10 Hz)


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
            ("xruns: ", "bold"), (f"{xruns}", "red" if xruns else "green"),
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

        notes_table = Table.grid(padding=(0, 2))
        # Sorted by pitch: custom layout files may list notes in any order
        items = sorted(((key, handler.note_for_key(key)) for key in handler.key_to_midi),
                       key=lambda item: item[1])
        columns = 6
        for row_start in range(0, len(items), columns):
            row = []
            for key, midi in items[row_start:row_start + columns]:
                row.append(Text.assemble((key, "bold cyan"), " -> ", (midi_to_note(midi), "magenta")))
            row += [""] * (columns - len(row))
            notes_table.add_row(*row)

        controls = handler.control_keys
        controls_text = Text.assemble(
            (controls['volume_up'], "bold cyan"), "/", (controls['volume_down'], "bold cyan"),
            " volume   ",
            (controls['instrument_next'], "bold cyan"), " next instrument   ",
            (controls['layout_next'], "bold cyan"), " next layout   ",
            (controls['octave_down'], "bold cyan"), "/", (controls['octave_up'], "bold cyan"),
            " octave down/up   ",
            (controls['record_start'], "bold cyan"), "/", (controls['record_stop'], "bold cyan"),
            " record start/stop   ",
            (controls['quit'], "bold cyan"), " quit",
        )

        grid = Table.grid()
        grid.add_row(notes_table)
        grid.add_row(Text())  # spacer
        grid.add_row(controls_text)
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
        keymap_height = (key_count + 5) // 6 + 4  # note rows + spacer + controls + borders

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
