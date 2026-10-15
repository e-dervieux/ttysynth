"""
Real-time priority for the audio callback thread, on Linux.

macOS runs its audio threads at real-time priority by itself. On Linux, the
PortAudio thread running our callback is an ordinary thread: whenever the
kernel runs something else for a millisecond, the callback misses its
deadline (an xrun), which matters when few buffers are queued
(--min-latency). The kernel only grants real-time priority to users with a
permission they don't have by default; RealtimeKit (rtkit), the desktop's
service for this, grants it to the programs of the local session, without
any permission (PipeWire itself gets its priority from it).

rtkit is a D-Bus service, called here with busctl (part of systemd).
"""

# Global imports
import os
import resource
import subprocess

# Priority requested: rtkit's default maximum, also what PipeWire gets
RTKIT_PRIORITY = 20

# rtkit requires a limit to the CPU time a real-time thread may use without
# blocking (at most its RTTimeUSecMax, 200 ms by default): a runaway
# real-time thread is then killed instead of freezing the machine. The
# callback blocks after every buffer, far below it.
RTTIME_LIMIT_US = 200_000


def make_thread_realtime(thread_id: int):
    """
    Ask rtkit for real-time priority for a thread of this process.

    Args:
        thread_id: kernel id of the thread (threading.get_native_id())

    Raises:
        OSError: rtkit is unavailable or refused (the message says why)
    """
    _, hard_limit = resource.getrlimit(resource.RLIMIT_RTTIME)
    limit = RTTIME_LIMIT_US
    if hard_limit != resource.RLIM_INFINITY:
        limit = min(limit, hard_limit)  # A limit can be lowered, never raised
    resource.setrlimit(resource.RLIMIT_RTTIME, (limit, limit))

    result = subprocess.run(
        ['busctl', '--timeout=2', '--allow-interactive-authorization=no', 'call',
         'org.freedesktop.RealtimeKit1', '/org/freedesktop/RealtimeKit1',
         'org.freedesktop.RealtimeKit1', 'MakeThreadRealtimeWithPID',
         'ttu', str(os.getpid()), str(thread_id), str(RTKIT_PRIORITY)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise OSError(f"rtkit: {result.stderr.strip()}")
