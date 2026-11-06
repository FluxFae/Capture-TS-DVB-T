"""The on-request analysis belongs to one capture — finding F1.

``analyze_pending_capture`` is the path behind the "Analyze capture" button that the
size gate leaves on the card (S4). It had no test at all, and that showed: it analysed
``_current_recording`` — whatever was recorded last — instead of the capture the offer
was made for, and it never recorded which file its report described.

What these tests pin:

1. A capture in flight has no complete file to describe, so the request waits.
2. The offer carries its own target; a later capture must not hijack it.
3. The request works even when there is no "current recording" to lean on.
4. The report that comes out is tied to the file it describes, so deleting that file
   drops it and deleting a different one does not (L6).
5. Starting a capture clears the stale offer instead of leaving a button that analyses
   the wrong file.
"""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.services import report
from app.services import tsduck_service as tsduck

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
PARKED_SOURCE = "file capture_parked.ts"


def sample_report(source=PARKED_SOURCE):
    return report.parse_tsanalyze_json(
        FIXTURE.read_text(encoding="utf-8"), source=source
    )


def recording_for(filepath, state):
    return tsduck.RecordingInfo(
        filename=Path(filepath).name,
        filepath=str(filepath),
        frequency=674_000_000,
        bandwidth=8,
        delivery_system="DVB-T",
        started_at=datetime(2026, 11, 6, 15, 0, 0),
        state=state,
    )


class NoThread:
    """A thread that never runs, so ``start_recording`` needs no ``tsp`` process."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class PendingAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.announcements = []
        self.service.set_report_callback(self.announcements.append)
        self.parked = Path(self.directory.name, "capture_parked.ts")
        self.parked.write_bytes(b"\x47" * 4096)
        self.other = Path(self.directory.name, "capture_other.ts")
        self.other.write_bytes(b"\x47" * 4096)

    def park(self):
        """Leave ``capture_parked.ts`` above the size gate, waiting for the operator."""
        with mock.patch.object(
            tsduck, "should_analyze_automatically", return_value=False
        ):
            self.service._analyze_finished_capture(self.parked)

    def test_a_pending_analysis_waits_while_a_capture_is_in_flight(self):
        self.park()
        self.service._current_recording = recording_for(
            self.other, tsduck.RecordingState.RECORDING
        )

        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ) as run:
            result = asyncio.run(self.service.analyze_pending_capture())

        self.assertIsNone(result, "a capture being written cannot be reported on")
        run.assert_not_called()
        self.assertIsNone(self.service.current_report)

    def test_a_pending_analysis_uses_the_capture_it_was_offered_for(self):
        self.park()
        # A later capture is the "current recording" but has nothing to do with the
        # offer, which was made for the capture that ended first.
        self.service._current_recording = recording_for(
            self.other, tsduck.RecordingState.IDLE
        )

        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ) as run:
            result = asyncio.run(self.service.analyze_pending_capture())

        self.assertIsNotNone(result)
        self.assertEqual(self.parked.resolve(), run.call_args.args[0])

    def test_a_pending_analysis_does_not_need_a_current_recording(self):
        self.park()
        self.service._current_recording = None

        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            result = asyncio.run(self.service.analyze_pending_capture())

        if result is None:
            self.fail("the offer carries its own target")
        self.assertEqual(PARKED_SOURCE, result.source)

    def test_the_pending_report_dies_with_the_file_it_describes(self):
        self.park()
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            asyncio.run(self.service.analyze_pending_capture())
        self.assertIsNotNone(self.service.current_report)

        # Deleting an unrelated capture must not take the report with it...
        self.other.unlink()
        self.assertIsNotNone(self.service.current_report)

        # ...and deleting the described file must (L6).
        self.parked.unlink()
        self.assertIsNone(self.service.current_report)

    def test_starting_a_capture_drops_the_stale_offer(self):
        self.park()
        self.assertIsNotNone(self.service.report_pending_bytes)

        with mock.patch.object(tsduck.threading, "Thread", NoThread):
            asyncio.run(self.service.start_recording(frequency=674_000_000, adapter=1))

        self.assertIsNone(
            self.service.report_pending_bytes,
            "the card still offers to analyse the capture that already ended",
        )


if __name__ == "__main__":
    unittest.main()
