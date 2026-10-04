"""Entry point of python -m synth"""

import sys

from synth.app import main

# Guarded: the audio process (multiprocessing, spawn start method) imports
# this module again, as __mp_main__
if __name__ == "__main__":
    sys.exit(main())
