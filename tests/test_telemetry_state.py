"""The telemetry counters describe a file, so they do not survive it — L8a.

``openspec/changes/stream-report-ui`` L8, verbatim: "hice la prueba de registrar un
file TS luego lo analice, lo abri y luego borre, al hacer refresh de la pagina, se
quedo la informacion en telemetry de la registracion anterior."

The four counters answer "how is the capture going", and after a reload they were
still answering it about a file that had been deleted. Two things were wrong at once:
the finished recording stayed in process state forever, and the empty status did not
carry the text the browser prints, so the page had nothing to overwrite the numbers
with. The rule is the one the report already follows (L6): the card talks about a
file, so it dies with it — while a capture that is still running, and a failure that
never produced a file, are left alone.

(The other half of L8 — the duplicated text under the counters — lives in
``test_telemetry_copy``.)
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.services import tsduck_service as tsduck


def finished_capture(filepath, size_bytes=120 * 1024 * 1024, seconds=12.0):
    """A capture that ran and stopped, which is what the counters describe."""
    return tsduck.RecordingInfo(
        filename=Path(filepath).name,
        filepath=str(filepath),
        frequency=674_000_000,
        bandwidth=8,
        delivery_system="DVB-T",
        started_at=datetime(2026, 11, 6, 13, 0, 0),
        duration_seconds=seconds,
        file_size_bytes=size_bytes,
        state=tsduck.RecordingState.IDLE,
        duration_limit=None,
    )


class TelemetryFollowsItsFileTest(unittest.TestCase):
    """L8a: deleting the capture takes the counters with it, on purpose."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.capture = Path(self.directory.name) / "capture_a.ts"
        self.capture.write_bytes(b"\x47" * 4096)
        self.service._current_recording = finished_capture(self.capture)
        self.announcements = []
        self.service.set_state_callback(self.announcements.append)

    def test_a_deleted_capture_leaves_no_counters_behind(self):
        asyncio.run(self.service.delete_recording("capture_a.ts"))

        status = self.service._get_recording_status()

        self.assertEqual(tsduck.RecordingState.IDLE, status["state"])
        self.assertEqual(0, status["file_size"])
        self.assertEqual(0, status["duration"])

    def test_the_file_removed_behind_the_app_also_clears_the_counters(self):
        self.capture.unlink()

        self.assertIsNone(self.service.current_recording)
        self.assertEqual("0 B", self.service._get_recording_status()["file_size_display"])

    def test_the_reset_is_announced_so_the_browser_redraws(self):
        asyncio.run(self.service.delete_recording("capture_a.ts"))

        self.assertTrue(self.announcements, "the counters were reset silently")
        last = self.announcements[-1]
        self.assertEqual("0 B", last["file_size_display"])
        self.assertEqual("00:00", last["duration_display"])

    def test_the_empty_status_still_carries_what_the_browser_prints(self):
        """Without the display fields the page keeps the stale numbers (L8a)."""
        status = tsduck.TSDuckService(recordings_dir=self.directory.name)._get_recording_status()

        self.assertEqual("0 B", status["file_size_display"])
        self.assertEqual("00:00", status["duration_display"])

    def test_deleting_a_different_recording_keeps_the_counters(self):
        other = Path(self.directory.name) / "capture_b.ts"
        other.write_bytes(b"\x47")

        asyncio.run(self.service.delete_recording("capture_b.ts"))

        self.assertEqual(120 * 1024 * 1024, self.service._get_recording_status()["file_size"])

    def test_a_capture_that_is_still_running_is_never_dropped(self):
        """tsp writes the file after tuning; the live counters must survive that gap."""
        recording = finished_capture(self.capture)
        recording.state = tsduck.RecordingState.RECORDING
        self.capture.unlink()
        self.service._current_recording = recording

        self.assertIsNotNone(self.service.current_recording)
        self.assertEqual(tsduck.RecordingState.RECORDING, self.service._get_recording_status()["state"])

    def test_a_failure_keeps_its_message_even_without_a_file(self):
        """A capture that failed before writing anything has no file, only an error."""
        recording = finished_capture(self.capture, size_bytes=0)
        recording.state = tsduck.RecordingState.ERROR
        recording.last_error = "no signal"
        self.capture.unlink()
        self.service._current_recording = recording

        status = self.service._get_recording_status()

        self.assertEqual("no signal", status["last_error"])
        self.assertEqual(tsduck.RecordingState.ERROR, status["state"])


if __name__ == "__main__":
    unittest.main()
