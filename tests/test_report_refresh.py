"""The report card must be told about a report the moment it exists.

``openspec/changes/stream-report-ui`` S4, finding L3a and L4. Two attempts died here
before, so the rule is spelled out:

1. The old card only changed when the operator pressed "Analyze capture".
2. The first fix delivered the card inside the recorder-panel response. It looked
   right and still failed, because ``_run_recording`` emits the finished ``IDLE``
   state *before* it analyses the file: the browser redrew the panel while the
   previous report was still the current one, and the new analysis arrived with
   nobody listening.

So the announcement must come from the analysis itself, after it is done — which is
what these tests pin: the service emits a report status when a report is built, when
a capture is parked above the size gate, and when the analysis fails (the card must
never be left showing a report that is no longer current).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The behaviour under test lives in the service; the template side of the contract
# (the card endpoint and the library button) is pinned in test_report_card and
# test_dashboard_layout.
from app.services import report
from app.services import tsduck_service as tsduck

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"


def sample_report(source="file capture_new.ts"):
    return report.parse_tsanalyze_json(
        FIXTURE.read_text(encoding="utf-8"), source=source
    )


class ReportAnnouncementTest(unittest.TestCase):
    """``_analyze_finished_capture`` is the only thing that knows a report exists."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.announcements = []
        self.service.set_report_callback(self.announcements.append)
        self.capture = Path(self.directory.name) / "capture_new.ts"
        self.capture.write_bytes(b"\x47" * 32_768)
        self.previous = sample_report("file capture_previous.ts")
        self.service._current_report = self.previous

    def test_a_built_report_is_announced_with_its_source(self):
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            self.service._analyze_finished_capture(self.capture)

        self.assertEqual(1, len(self.announcements), "the card was never told")
        self.assertEqual("file capture_new.ts", self.announcements[0]["source"])
        self.assertIsNone(self.announcements[0]["pending_bytes"])

    def test_a_capture_above_the_gate_is_announced_as_waiting(self):
        with mock.patch.object(
            tsduck, "should_analyze_automatically", return_value=False
        ):
            self.service._analyze_finished_capture(self.capture)

        self.assertEqual(1, len(self.announcements))
        self.assertIsNone(self.announcements[0]["source"])
        self.assertEqual(32_768, self.announcements[0]["pending_bytes"])

    def test_a_failed_analysis_is_announced_as_no_report(self):
        """L3a: leaving the previous report on screen is the defect being fixed."""

        with mock.patch.object(
            self.service,
            "_run_tsanalyze_report",
            side_effect=report.ReportParseError("tsanalyze failed"),
        ):
            self.service._analyze_finished_capture(self.capture)

        self.assertEqual(1, len(self.announcements))
        self.assertIsNone(self.announcements[0]["source"])
        self.assertIsNone(self.announcements[0]["pending_bytes"])
        self.assertIsNone(self.service.current_report)

    def test_the_announcement_happens_after_the_report_is_current(self):
        """The old ordering bug, stated directly: announce, then be stale."""

        seen = []
        self.service.set_report_callback(
            lambda status: seen.append((status["source"], self.service.current_report))
        )
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            self.service._analyze_finished_capture(self.capture)

        announced_source, current = seen[0]
        self.assertEqual("file capture_new.ts", announced_source)
        self.assertIsNotNone(current)
        self.assertEqual(announced_source, current.source)

    def test_a_service_without_callbacks_still_analyses(self):
        self.service.set_report_callback(lambda status: None)
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            self.service._analyze_finished_capture(self.capture)

        self.assertIsNotNone(self.service.current_report)


class OnDemandReportTest(unittest.TestCase):
    """L4: any recording in the library can be analysed on request (S10)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.target = Path(self.directory.name) / "capture_old.ts"
        self.target.write_bytes(b"\x47" * 4096)

    def test_an_existing_recording_can_be_analysed_by_name(self):
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ) as analyzer:
            built = asyncio_run(self.service.analyze_recording("capture_old.ts"))

        self.assertIsNotNone(built)
        self.assertEqual(self.target, Path(analyzer.call_args.args[0]))
        self.assertIs(built, self.service.current_report)

    def test_it_replaces_the_previous_report_instead_of_keeping_both(self):
        self.service._current_report = sample_report("file capture_previous.ts")
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            built = asyncio_run(self.service.analyze_recording("capture_old.ts"))

        if built is None:
            self.fail("the recording was not analysed")
        self.assertEqual("file capture_new.ts", built.source)
        self.assertIs(built, self.service.current_report)

    def test_a_failed_analysis_leaves_no_report_behind(self):
        self.service._current_report = sample_report("file capture_previous.ts")
        with mock.patch.object(
            self.service,
            "_run_tsanalyze_report",
            side_effect=report.ReportParseError("tsanalyze failed"),
        ):
            built = asyncio_run(self.service.analyze_recording("capture_old.ts"))

        self.assertIsNone(built)
        self.assertIsNone(self.service.current_report)

    def test_a_name_outside_the_recordings_directory_is_refused(self):
        victim = Path(self.directory.name).parent / "victim.ts"
        victim.write_bytes(b"\x47")

        for name in ("..\\victim.ts", "../victim.ts", str(victim)):
            with self.subTest(name=name), mock.patch.object(
                self.service, "_run_tsanalyze_report"
            ) as analyzer:
                self.assertIsNone(asyncio_run(self.service.analyze_recording(name)))
                analyzer.assert_not_called()

    def test_an_unknown_name_is_refused_without_running_anything(self):
        with mock.patch.object(self.service, "_run_tsanalyze_report") as analyzer:
            self.assertIsNone(asyncio_run(self.service.analyze_recording("nope.ts")))

            analyzer.assert_not_called()

    def test_a_non_ts_name_is_refused(self):
        (Path(self.directory.name) / "notes.txt").write_text("x")

        with mock.patch.object(self.service, "_run_tsanalyze_report") as analyzer:
            self.assertIsNone(asyncio_run(self.service.analyze_recording("notes.txt")))

            analyzer.assert_not_called()


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


class PanelStaysSinglePurposeTest(unittest.TestCase):
    """The out-of-band copy is gone; the card has exactly one home on the page.

    The first fix delivered the card inside the panel response. It is not only the
    wrong moment (L3a); it also has to be removed to keep the panel partial usable
    in the dashboard, where a second ``#report-section-body`` would appear.
    """

    def test_the_panel_carries_no_report_card(self):
        from app.main import templates

        rendered = templates.env.get_template("partials/recorder_panel.html").render(
            recording_state="idle", current_recording=None
        )

        self.assertNotIn("hx-swap-oob", rendered)
        self.assertNotIn("report-section-body", rendered)


class ReportCardEndpointTest(unittest.TestCase):
    """The browser reads the card from one place, whatever changed it."""

    def request(self):
        from starlette.requests import Request

        return Request({"type": "http", "method": "GET", "path": "/",
                        "headers": [], "query_string": b""})

    def test_the_card_endpoint_renders_the_current_state(self):
        from app import main

        main.tsduck_service._current_report = sample_report()
        self.addCleanup(setattr, main.tsduck_service, "_current_report", None)

        response = asyncio_run(main.get_report_card(self.request()))
        body = bytes(response.body).decode()

        self.assertIn("file capture_new.ts", body)
        self.assertIn("report-content", body)

    def test_the_card_endpoint_says_so_when_there_is_nothing(self):
        from app import main

        main.tsduck_service._current_report = None
        response = asyncio_run(main.get_report_card(self.request()))

        self.assertIn("report-empty", bytes(response.body).decode())


class LibraryReportButtonTest(unittest.TestCase):
    """L4: every recording in the library can be analysed on request."""

    def render(self):
        from app.main import templates

        return templates.env.get_template("partials/recordings_table.html").render(
            recordings=[
                {"filename": "capture_a.ts", "size_display": "10 MB",
                 "created": "2026-11-06T10:00:00"},
                {"filename": "capture_b.ts", "size_display": "20 MB",
                 "created": "2026-11-06T11:00:00"},
            ]
        )

    def buttons(self):
        from html.parser import HTMLParser

        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.items = []

            def handle_starttag(self, tag, attrs):
                if tag == "button":
                    self.items.append(dict(attrs))

        reader = Buttons()
        reader.feed(self.render())
        return reader.items

    def test_each_recording_offers_a_report_action(self):
        # Scoped to this action: the row also carries reveal (T6) and delete, and
        # widening the selector here would make the list unpinnable.
        posts = [
            b.get("hx-post")
            for b in self.buttons()
            if (b.get("hx-post") or "").startswith("/api/report/analyze/")
        ]

        self.assertEqual(
            ["/api/report/analyze/capture_a.ts", "/api/report/analyze/capture_b.ts"],
            posts,
        )

    def test_the_action_swaps_the_card(self):
        targets = {
            b.get("hx-target")
            for b in self.buttons()
            if (b.get("hx-post") or "").startswith("/api/report/analyze/")
        }

        self.assertEqual({"#report-section-body"}, targets)

    def test_the_delete_action_is_still_there(self):
        deletes = [b.get("hx-delete") for b in self.buttons() if b.get("hx-delete")]

        self.assertEqual(
            ["/api/recordings/capture_a.ts", "/api/recordings/capture_b.ts"], deletes
        )


if __name__ == "__main__":
    unittest.main()


class ReportFollowsItsFileTest(unittest.TestCase):
    """L6: a report about a file that no longer exists must never render.

    The report lives in the server process, so it survives a page reload on
    purpose (decided with the user). What it cannot do is outlive its own file:
    the library offers deletion, and a card still claiming an analysis of a
    deleted recording is a lie the operator cannot tell apart from a real one.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.announcements = []
        self.service.set_report_callback(self.announcements.append)
        self.capture = Path(self.directory.name) / "capture_new.ts"
        self.capture.write_bytes(b"\x47" * 4096)

    def build_report(self):
        with mock.patch.object(
            self.service, "_run_tsanalyze_report", return_value=sample_report()
        ):
            self.service._analyze_finished_capture(self.capture)
        self.assertIsNotNone(self.service.current_report)

    def test_deleting_the_analysed_recording_clears_the_card(self):
        self.build_report()

        asyncio_run(self.service.delete_recording("capture_new.ts"))

        self.assertIsNone(self.service.current_report)
        self.assertEqual(
            [{"source": None, "pending_bytes": None}], self.announcements[-1:]
        )

    def test_deleting_another_recording_keeps_the_report(self):
        self.build_report()
        other = Path(self.directory.name) / "capture_other.ts"
        other.write_bytes(b"\x47" * 1024)

        asyncio_run(self.service.delete_recording("capture_other.ts"))

        self.assertIsNotNone(self.service.current_report)

    def test_a_file_removed_behind_the_app_is_not_rendered(self):
        self.build_report()

        self.capture.unlink()

        self.assertIsNone(self.service.current_report)

    def test_a_deleted_capture_is_no_longer_offered_for_analysis(self):
        with mock.patch.object(
            tsduck, "should_analyze_automatically", return_value=False
        ):
            self.service._analyze_finished_capture(self.capture)
        self.assertEqual(4096, self.service.report_pending_bytes)

        asyncio_run(self.service.delete_recording("capture_new.ts"))

        self.assertIsNone(self.service.report_pending_bytes)
