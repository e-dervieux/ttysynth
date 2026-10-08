"""Entry point of python -m synth"""

import sys

# Guarded, import included: the audio process (multiprocessing, spawn start
# method) imports this module again, as __mp_main__, and must not load the
# application (TUI, keyboard input) it does not use
if __name__ == "__main__":
    from synth.app import main
    sys.exit(main())
