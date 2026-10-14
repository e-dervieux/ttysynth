"""
TTYSynth: a Python Command Line Audio Synthesizer.

Process layout: the main process handles keyboard input and the TUI; the
audio process (spawned by AudioEngine) owns the audio stream and synthesizes
sound in its callback. See app.py for the overall coordination and
audio_engine.py for the audio side.
"""

# Shown by the TUI, --help and the startup log
APP_TITLE = "TTYSynth: a Python Command Line Audio Synthesizer"
