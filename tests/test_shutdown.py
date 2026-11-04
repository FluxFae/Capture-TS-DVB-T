"""Shutdown safety: a capture must never outlive the server.

On Windows a child process survives its parent, so closing the terminal (or a
crash) in the middle of a capture used to leave ``tsp`` writing to disk with
nobody watching. Reproduced by killing the server mid-capture: the file grew
8 MB in 3 seconds with no server attached, and nothing in the UI could stop it.
"""

import shutil
import tempfile
import unittest
from unittest import mock

from app import main
from app.services import tsduck_service as tsduck
from tests.test_recording_state import fake_tsp


class ShutdownRecordingsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.service = tsduck.TSDuckService(recordings_dir=self.tmpdir, poll_interval=0.02)

    async def test_an_active_capture_is_stopped_on_shutdown(self):
        popen = fake_tsp(data_after=0.05, exit_after=None)

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            await self.service.start_recording(
                frequency=610_000_000,
                adapter=0,
                filename="capture.ts",
            )
            await main.shutdown_recordings(self.service)

        self.assertIsNotNone(popen.last)
        assert popen.last is not None
        self.assertTrue(popen.last.terminated, "tsp was left running after shutdown")
        self.assertEqual(self.service.state, tsduck.RecordingState.IDLE)

    async def test_shutdown_without_a_capture_changes_nothing(self):
        await main.shutdown_recordings(self.service)

        self.assertEqual(self.service.state, tsduck.RecordingState.IDLE)

    async def test_shutdown_stops_a_capture_that_is_still_tuning(self):
        popen = fake_tsp(data_after=None, exit_after=None)

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            await self.service.start_recording(
                frequency=610_000_000,
                adapter=0,
                filename="capture.ts",
            )
            self.assertEqual(self.service.state, tsduck.RecordingState.TUNING)
            await main.shutdown_recordings(self.service)

        self.assertIsNotNone(popen.last)
        assert popen.last is not None
        self.assertTrue(popen.last.terminated)


if __name__ == "__main__":
    unittest.main()
