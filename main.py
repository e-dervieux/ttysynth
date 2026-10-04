"""
Python Command Line Audio Synthesizer - entry point: python main.py
(or python -m synth). The application itself is in synth/app.py.
"""

import sys

from synth.app import main

if __name__ == "__main__":
    sys.exit(main())
