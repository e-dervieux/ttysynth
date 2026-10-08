"""
Python Command Line Audio Synthesizer - entry point: python main.py
(or python -m synth). The application itself is in synth/app.py.
"""

import sys

# Imported under the guard: the audio process (multiprocessing, spawn start
# method) runs this file again, as __mp_main__, and must not load the
# application (TUI, keyboard input) it does not use
if __name__ == "__main__":
    from synth.app import main
    sys.exit(main())
