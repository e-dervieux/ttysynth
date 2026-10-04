"""The test session must use the fake sounddevice module"""

import sounddevice


def test_sounddevice_is_the_fake():
    assert sounddevice.query_devices() == 'fake device list'
