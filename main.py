"""
TTYSynth, a Python Command Line Audio Synthesizer - entry point: python main.py
(or python -m synth). The application itself is in synth/app.py.
"""

# /!\ ATTENTION: no imports here, put them under the __main__ guard below.
# The audio process (multiprocessing, spawn start method) runs this file
# again, as __mp_main__, and only skips the guarded block: imports up here
# would load the application (TUI, keyboard input...) in the audio process
# for nothing.

if __name__ == "__main__":
    import sys
    from synth.app import main
    sys.exit(main())
