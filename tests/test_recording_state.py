"""Recording state machine tests — B2/B3/B4/B5.

These tests never touch the DVB tuner: ``subprocess.Popen`` is replaced by a
fake ``tsp`` process whose behaviour (when data appears, when it exits, what it
writes on stderr) is fully controlled by the test.

The invariant under test: the recorder must never report a successful
recording without data, must measure the duration limit from the first byte
(not from process launch), and must surface ``tsp`` failures.
"""

import asyncio
import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services import tsduck_service as tsduck

STREAM_RAW = (
    Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
).read_text(encoding="utf-8")


class SlowStderr:
    """File-like object that yields stderr lines slowly, like a real tsp run."""

    def __init__(self, text, delay):
        self._lines = text.splitlines(keepends=True)
        self._delay = delay

    def __iter__(self):
        for line in self._lines:
            time.sleep(self._delay)
            yield line


def fake_tsp(
    *,
    data_after=None,
    payload=b"x" * 4096,
    exit_after=None,
    exit_code=0,
    stderr_text="",
    stderr_delay=0.0,
    touch_empty=False,
):
    """Build a fake Popen class that emulates a tsp process."""

    class FakeProcess:
        last = None

        def __init__(self, argv, **kwargs):
            self.argv = list(argv)
            self.stderr = (
                SlowStderr(stderr_text, stderr_delay)
                if stderr_delay
                else io.StringIO(stderr_text)
            )
            self._t0 = time.monotonic()
            self._written = False
            self.returncode = None
            self.terminated = False
            out_index = self.argv.index("-O")
            self.out_path = self.argv[out_index + 2]
            if touch_empty:
                # tsp creates its output file before any packet arrives.
                open(self.out_path, "wb").close()
            FakeProcess.last = self

        def _tick(self):
            elapsed = time.monotonic() - self._t0
            if data_after is not None and not self._written and elapsed >= data_after:
                with open(self.out_path, "wb") as handle:
                    handle.write(payload)
                self._written = True
            if exit_after is not None and elapsed >= exit_after and self.returncode is None:
                self.returncode = exit_code

        def poll(self):
            self._tick()
            return self.returncode

        def terminate(self):
            self.terminated = True
            if self.returncode is None:
                self.returncode = -1

        def kill(self):
            self.terminate()

        def wait(self, timeout=None):
            self._tick()
            return self.returncode

    return FakeProcess


class RecordingStateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, ignore_errors=True))

    def make_service(self):
        service = tsduck.TSDuckService(recordings_dir=self.tmpdir, poll_interval=0.02)
        self.logs = []
        self.states = []
        service.set_log_callback(self.logs.append)
        service.set_state_callback(self.states.append)
        return service

    def log_text(self):
        return "\n".join(self.logs)

    def wait_for_thread(self, service, timeout=10):
        thread = service._recording_thread
        self.assertIsNotNone(thread, "no recording thread was started")
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), "recording thread did not finish")

    async def start(self, service, **kwargs):
        return await service.start_recording(
            frequency=674000000,
            bandwidth=8,
            adapter=0,
            delivery_system="DVB-T",
            filename="capture.ts",
            **kwargs,
        )


class DurationStartsAtSignalLockTest(RecordingStateTestCase):
    """B2/B3: the duration limit and the 'locked' log must follow the first byte."""

    def test_duration_limit_counts_from_first_byte_not_from_launch(self):
        # tsp needs 0.6s to lock, and the requested duration is shorter than
        # that. A clock started at launch expires before any data exists and
        # leaves a zero-byte capture; a clock started at the first byte waits.
        popen = fake_tsp(data_after=0.6, exit_after=None)
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, duration_seconds=0.3))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(service.state, tsduck.RecordingState.IDLE)
        self.assertGreater(info.file_size_bytes, 0, "recording ended with zero bytes")
        # ~0.3s of recording starting 0.6s after launch, not 0.9s of lifetime.
        self.assertGreaterEqual(info.duration_seconds, 0.25)
        self.assertLess(info.duration_seconds, 0.75, "duration includes the tuning phase")
        self.assertIsNotNone(popen.last)
        self.assertLess(popen.last._t0, time.monotonic())  # type: ignore[union-attr]
        self.assertIn("Duration limit reached", self.log_text())

    def test_lock_log_only_after_data_exists(self):
        popen = fake_tsp(data_after=0.25, exit_after=1.0)
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service))
            time.sleep(0.1)
            self.assertNotIn(
                "Signal locked",
                self.log_text(),
                "claimed signal lock before any data arrived",
            )
            self.wait_for_thread(service)

        self.assertIn("Signal locked", self.log_text())


class FailureSurfacingTest(RecordingStateTestCase):
    """B5: a tsp failure must surface as ERROR with a readable message."""

    def test_tsp_error_is_not_reported_as_success(self):
        popen = fake_tsp(
            data_after=None,
            exit_after=0.1,
            exit_code=1,
            stderr_text=(
                "* Error: dvb: no input DVB signal after 5000 milliseconds\n"
            ),
        )
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(service.state, tsduck.RecordingState.ERROR)
        self.assertIn("no input DVB signal", info.last_error)
        logs = self.log_text()
        self.assertNotIn("Recording stopped", logs, "reported success for an empty capture")
        self.assertNotIn("Signal locked", logs)
        self.assertIn("no input DVB signal", logs)

    def test_failure_without_stderr_still_reports_error(self):
        popen = fake_tsp(data_after=None, exit_after=0.05, exit_code=1, stderr_text="")
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(service.state, tsduck.RecordingState.ERROR)
        self.assertTrue(info.last_error, "no error message recorded")

    def test_real_stderr_message_wins_over_generic_fallback(self):
        # The drain thread needs time to deliver; the failure reason must wait
        # for it instead of reporting a generic "exited with code" message.
        popen = fake_tsp(
            data_after=None,
            exit_after=0.05,
            exit_code=1,
            stderr_text="* Error: dvb: no input DVB signal after 5000 milliseconds\n",
            stderr_delay=0.2,
        )
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertIn("no input DVB signal", info.last_error)

    def test_failed_capture_leaves_no_empty_file(self):
        popen = fake_tsp(
            data_after=None, exit_after=0.1, exit_code=1, touch_empty=True
        )
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service))
            self.wait_for_thread(service)

        leftovers = sorted(path.name for path in Path(self.tmpdir).glob("*.ts"))
        self.assertEqual(leftovers, [], "failed capture left an empty file in the library")


class StopDuringTuningTest(RecordingStateTestCase):
    """B4: Stop must work while tuning, and must not leave tsp running."""

    def test_stop_during_tuning_kills_process(self):
        popen = fake_tsp(data_after=None, exit_after=None)
        service = self.make_service()

        import asyncio

        async def scenario():
            await self.start(service)
            await asyncio.sleep(0.05)
            self.assertEqual(service.state, tsduck.RecordingState.TUNING)
            stopped = await service.stop_recording()
            return stopped

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            stopped = asyncio.run(scenario())
            self.wait_for_thread(service)

        self.assertIsNotNone(stopped, "stop during tuning was ignored")
        self.assertIsNotNone(popen.last)
        self.assertTrue(popen.last.terminated, "tsp process was left running")  # type: ignore[union-attr]
        self.assertNotEqual(service.state, tsduck.RecordingState.STOPPING)
        self.assertNotEqual(service.state, tsduck.RecordingState.TUNING)
        final = service.current_recording
        self.assertIsNotNone(final)
        assert final is not None
        self.assertGreaterEqual(final.file_size_bytes, 0)
        self.assertNotIn("Recording stopped", self.log_text())

    def test_stop_when_idle_is_a_noop(self):
        service = self.make_service()

        import asyncio

        self.assertIsNone(asyncio.run(service.stop_recording()))
        self.assertEqual(service.state, tsduck.RecordingState.IDLE)

    def test_second_start_is_rejected(self):
        popen = fake_tsp(data_after=None, exit_after=None)
        service = self.make_service()

        import asyncio

        async def scenario():
            await self.start(service)
            await asyncio.sleep(0.05)
            with self.assertRaises(RuntimeError):
                await self.start(service)
            await service.stop_recording()

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(scenario())
            self.wait_for_thread(service)


class RecordingsLibraryTest(RecordingStateTestCase):
    """Regression guard: the library must keep ignoring non-.ts and outside paths."""

    def test_delete_rejects_path_outside_recordings(self):
        import asyncio

        service = self.make_service()
        victim = Path(self.tmpdir).parent / "victim.ts"
        victim.write_text("keep")
        self.addCleanup(lambda: victim.unlink(missing_ok=True))

        self.assertFalse(asyncio.run(service.delete_recording("..\\victim.ts")))
        self.assertTrue(victim.exists())

    def test_list_only_returns_ts_files(self):
        import asyncio

        service = self.make_service()
        Path(self.tmpdir, "a.ts").write_bytes(b"1")
        Path(self.tmpdir, "b.txt").write_bytes(b"1")

        listed = asyncio.run(service.list_recordings())
        self.assertEqual([item["filename"] for item in listed], ["a.ts"])

class FilteredCaptureFailureTest(RecordingStateTestCase):
    """A filter that matches nothing must not be reported as a success.

    ``tsp`` exits **0** after writing only the tables when ``zap`` cannot find
    the requested services, so a 188-byte file was presented as a finished
    capture. The rule: a payload smaller than the floor, together with a tsp
    error, is a failure and the artifact is discarded.
    """

    ZAP_ERROR = "* Error: zap: service 101 not found in PAT"


    def test_a_filtered_capture_without_payload_is_an_error(self):
        popen = fake_tsp(
            data_after=0.05,
            payload=b"x" * 188,
            exit_after=0.3,
            exit_code=0,
            stderr_text=self.ZAP_ERROR,
        )
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, service_ids=[101, 102, 103]))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(service.state, tsduck.RecordingState.ERROR)
        self.assertIn("not found in PAT", info.last_error)
        self.assertEqual(list(Path(self.tmpdir).glob("*.ts")), [], "kept a table-only artifact")

    def test_a_tsp_error_after_real_payload_keeps_the_file(self):
        popen = fake_tsp(
            data_after=0.05,
            payload=b"x" * 200_000,
            exit_after=0.3,
            exit_code=0,
            stderr_text=self.ZAP_ERROR,
        )
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, service_ids=[101]))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertGreater(info.file_size_bytes, 0)
        self.assertTrue((Path(self.tmpdir) / "capture.ts").exists(), "deleted real data")


class ServiceFilterTest(RecordingStateTestCase):
    """M4: a service filter that names no service must never fall back to the mux.

    Unchecking every service and pressing record used to record the whole mux,
    because ``service_ids`` was empty and the filter was simply dropped. That is
    the opposite of what the mode promises, and it fills the disk silently.
    """

    def test_selected_mode_without_services_is_rejected(self):
        popen = fake_tsp(data_after=0.1, exit_after=1.0)
        service = self.make_service()

        import asyncio

        with (
            mock.patch("app.services.tsduck_service.subprocess.Popen", popen),
            self.assertRaises(tsduck.NoServicesSelectedError) as caught,
        ):
            asyncio.run(self.start(service, service_ids=[]))

        self.assertIn("full mux", str(caught.exception))
        self.assertIsNone(popen.last, "tsp was started for an empty selection")
        self.assertIsNone(service._recording_thread)
        self.assertEqual(service.state, tsduck.RecordingState.IDLE)
        self.assertEqual(list(Path(self.tmpdir).glob("*.ts")), [])

    def test_selected_services_reach_the_zap_plugin(self):
        popen = fake_tsp(data_after=0.1, exit_after=1.0)
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, service_ids=[101, 102]))
            self.wait_for_thread(service)

        self.assertIsNotNone(popen.last)
        argv = popen.last.argv  # type: ignore[union-attr]
        zap_at = argv.index("zap")
        self.assertEqual(argv[zap_at - 1], "-P")
        self.assertEqual(argv[zap_at + 1:zap_at + 3], ["101", "102"])
        self.assertIn("--no-ecm", argv)

    def test_requested_duration_is_kept_on_the_recording(self):
        # The panel redraws from duration_limit, so the requested number has to
        # survive on the recording state and not only inside the tsp command.
        popen = fake_tsp(data_after=0.1, exit_after=1.0)
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, duration_seconds=120))
            self.wait_for_thread(service)

        info = service.current_recording
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.duration_limit, 120)

    def test_full_mux_records_without_a_zap_plugin(self):
        popen = fake_tsp(data_after=0.1, exit_after=1.0)
        service = self.make_service()

        import asyncio

        with mock.patch("app.services.tsduck_service.subprocess.Popen", popen):
            asyncio.run(self.start(service, service_ids=None))
            self.wait_for_thread(service)

        self.assertIsNotNone(popen.last)
        self.assertNotIn("zap", popen.last.argv)  # type: ignore[union-attr]


class AutoAnalyzeGateTest(unittest.TestCase):
    """S4: the gate is a pure function of the finished file size.

    512 MiB is a few seconds of analysis; a 30 minute unlimited capture at
    24.9 Mbit/s is ~5.6 GB and ~40 s, which reads as a hang if nobody asked for
    it.
    """

    def test_a_small_capture_is_analyzed_automatically(self):
        self.assertTrue(tsduck.should_analyze_automatically(200 * 1024 * 1024))

    def test_the_gate_includes_the_threshold_itself(self):
        self.assertTrue(
            tsduck.should_analyze_automatically(tsduck.AUTO_ANALYZE_MAX_BYTES)
        )

    def test_one_byte_over_the_gate_needs_the_operator(self):
        self.assertFalse(
            tsduck.should_analyze_automatically(tsduck.AUTO_ANALYZE_MAX_BYTES + 1)
        )

    def test_an_empty_file_is_never_analyzed(self):
        self.assertFalse(tsduck.should_analyze_automatically(0))


class AutoReportAfterCaptureTest(RecordingStateTestCase):
    """S4: a finished capture reports on itself; a failed one does not."""

    def completed_report(self):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=STREAM_RAW, stderr=""
        )

    def test_a_finished_capture_produces_a_report_without_user_action(self):
        popen = fake_tsp(data_after=0.1, exit_after=None)
        service = self.make_service()

        with mock.patch(
            "app.services.tsduck_service.subprocess.Popen", popen
        ), mock.patch(
            "app.services.tsduck_service.subprocess.run",
            return_value=self.completed_report(),
        ):
            asyncio.run(self.start(service, duration_seconds=0.3))
            self.wait_for_thread(service)

        built = service.current_report
        self.assertIsNotNone(built, "a finished capture left the report empty")
        assert built is not None
        self.assertEqual("file capture.ts", built.source)
        self.assertEqual(19, built.integrity.service_count)
        self.assertEqual(0, built.integrity.packets.transport_errors)
        self.assertIsNone(service.report_pending_bytes)

    def test_a_capture_over_the_gate_waits_for_the_operator(self):
        popen = fake_tsp(data_after=0.1, exit_after=None)
        service = self.make_service()

        with mock.patch(
            "app.services.tsduck_service.subprocess.Popen", popen
        ), mock.patch.object(
            tsduck, "AUTO_ANALYZE_MAX_BYTES", 10
        ), mock.patch(
            "app.services.tsduck_service.subprocess.run"
        ) as run:
            asyncio.run(self.start(service, duration_seconds=0.3))
            self.wait_for_thread(service)

        self.assertIsNone(service.current_report)
        self.assertEqual(4096, service.report_pending_bytes)
        run.assert_not_called()

    def test_a_failed_capture_produces_no_report_at_all(self):
        popen = fake_tsp(
            data_after=None,
            exit_after=0.3,
            stderr_text="* Error: no input DVB signal\n",
        )
        service = self.make_service()

        with mock.patch(
            "app.services.tsduck_service.subprocess.Popen", popen
        ), mock.patch("app.services.tsduck_service.subprocess.run") as run:
            asyncio.run(self.start(service, duration_seconds=5))
            self.wait_for_thread(service)

        self.assertEqual(tsduck.RecordingState.ERROR, service.state)
        self.assertIsNone(service.current_report)
        self.assertIsNone(service.report_pending_bytes)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
