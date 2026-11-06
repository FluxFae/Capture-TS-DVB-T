"""Where the recorder lives on the dashboard, and what it must not drag along.

``openspec/changes/stream-report-ui`` S1, S3 and S8. The recorder moved into the
Signal Telemetry card so the action and its feedback stay together, and it had to
be compacted to fit the narrower column without losing the four live counters.

These tests parse the rendered dashboard as a tree instead of matching strings,
because the risks involved are structural: a second ``#recorder-form`` left behind
in the bottom row, a recorder dropped outside the card, or a swap target that
wraps the counters and redraws them on every panel refresh.
"""

import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from app.main import templates
from app.services.tsduck_service import RecordingInfo

DASHBOARD = "dashboard.html"

METRICS = ("metric-state", "metric-filesize", "metric-duration", "metric-bitrate")


class Element:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Element] = []

    @property
    def element_id(self):
        return self.attrs.get("id")

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def is_inside(self, identifier):
        return any(node.element_id == identifier for node in self.ancestors())

    def contains(self, identifier):
        return any(node.element_id == identifier for node in self.walk())


class TreeReader(HTMLParser):
    """Builds an element tree, so ancestry questions get exact answers."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.root = Element("[document]", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return


def render_dashboard(recording_state="idle", current_recording=None, report=None,
                     report_pending_bytes=None):
    rendered = templates.env.get_template(DASHBOARD).render(
        devices=[],
        selected_adapter=-1,
        delivery_systems=[],
        selected_system="DVB-T",
        recordings=[],
        recording_state=recording_state,
        current_recording=current_recording,
        report=report,
        report_pending_bytes=report_pending_bytes,
    )
    reader = TreeReader()
    reader.feed(rendered)
    return rendered, reader.root


def find_by_id(root, identifier):
    return [node for node in root.walk() if node.element_id == identifier]


class RecorderPlacementTest(unittest.TestCase):
    """S1: one recorder, inside the telemetry card, and no leftover card."""

    def setUp(self):
        self.html, self.root = render_dashboard()

    def test_the_recorder_form_exists_exactly_once(self):
        self.assertEqual(1, len(find_by_id(self.root, "recorder-form")))

    def test_the_recorder_lives_inside_the_telemetry_card(self):
        (form,) = find_by_id(self.root, "recorder-form")

        self.assertTrue(
            form.is_inside("signal-telemetry"),
            "the recorder form is not a descendant of #signal-telemetry",
        )

    def test_the_old_recorder_card_is_gone(self):
        self.assertEqual([], find_by_id(self.root, "recorder-section"))
        self.assertNotIn("TS Recorder", self.html)

    def test_the_telemetry_card_keeps_its_slot_in_the_top_row(self):
        (telemetry,) = find_by_id(self.root, "signal-telemetry")

        self.assertFalse(telemetry.is_inside("rf-control"))
        row = telemetry.parent
        assert row is not None
        self.assertIn("flux-dashboard__row--top", row.attrs.get("class", ""))


class CompactControlsTest(unittest.TestCase):
    """S3: mode, duration and the button share one row container."""

    def setUp(self):
        self.html, self.root = render_dashboard()

    def test_the_three_controls_share_a_single_row_container(self):
        (row,) = find_by_id(self.root, "recorder-controls")
        children = [child for child in row.children if child.tag in {"div", "button"}]

        self.assertEqual(3, len(children), "the controls row must hold exactly three cells")
        self.assertTrue(children[0].walk())
        self.assertTrue(
            any(node.attrs.get("name") == "record_mode" for node in row.walk()),
            "the capture-mode radios are not inside the controls row",
        )
        self.assertTrue(
            any(node.attrs.get("name") == "duration" for node in row.walk()),
            "the duration input is not inside the controls row",
        )
        self.assertTrue(
            any(node.tag == "button" for node in row.walk()),
            "the record button is not inside the controls row",
        )

    def test_the_controls_row_sits_inside_the_recorder_form(self):
        (row,) = find_by_id(self.root, "recorder-controls")

        self.assertTrue(row.is_inside("recorder-form"))

    def test_the_record_button_no_longer_spans_the_full_width(self):
        (row,) = find_by_id(self.root, "recorder-controls")
        buttons = [node for node in row.walk() if node.tag == "button"]

        self.assertTrue(buttons, "no record button rendered")
        for button in buttons:
            self.assertNotIn("flux-btn--full", button.attrs.get("class", ""))


class LiveCountersSurviveTest(unittest.TestCase):
    """S8: the counters keep updating while the panel redraws them away."""

    def test_the_four_counters_are_inside_the_telemetry_card(self):
        _, root = render_dashboard()

        for metric in METRICS:
            (node,) = find_by_id(root, metric)
            self.assertTrue(
                node.is_inside("signal-telemetry"),
                f"#{metric} is not inside #signal-telemetry",
            )

    def test_the_panel_swap_target_does_not_wrap_the_counters(self):
        """A refresh of #recorder-panel must not redraw the metrics.

        The panel is re-rendered over the WebSocket when a capture ends, so any
        counter living inside that target would be rebuilt from scratch (and lose
        the values accumulated in the DOM between frames).
        """

        _, root = render_dashboard()
        (panel,) = find_by_id(root, "recorder-panel")

        for metric in METRICS:
            self.assertFalse(
                panel.contains(metric),
                f"#{metric} would be destroyed by a #recorder-panel swap",
            )

    def test_the_form_stays_inside_the_swap_target(self):
        _, root = render_dashboard()
        (panel,) = find_by_id(root, "recorder-panel")

        self.assertTrue(panel.contains("recorder-form"))

    def test_a_running_capture_does_not_repeat_its_counters(self):
        """L8b: the panel used to print the megabytes and seconds a second time.

        The counters above the panel are the live truth; a summary line repeating
        them read as the same information said twice, which is what the operator
        objected to. The panel's job is the controls, not the numbers.
        """
        recording = RecordingInfo(
            filename="capture.ts",
            filepath="recordings/capture.ts",
            frequency=674_000_000,
            bandwidth=8_000_000,
            delivery_system="DVB-T",
            started_at=datetime(2026, 11, 5, 12, 0, 0),
            duration_limit=120,
        )
        _, root = render_dashboard("recording", recording)
        (panel,) = find_by_id(root, "recorder-panel")

        self.assertFalse(any(node.attrs.get("id") == "live-title" for node in panel.walk()))


class BottomRowTest(unittest.TestCase):
    """S2: the recorder's slot now holds the report, with the log beside it."""

    def setUp(self):
        _, self.root = render_dashboard()

    def bottom_row(self):
        rows = [
            node
            for node in self.root.walk()
            if "flux-dashboard__row--bottom" in node.attrs.get("class", "")
        ]
        self.assertEqual(1, len(rows), "the bottom row must exist exactly once")
        return rows[0]

    def test_the_bottom_row_is_report_then_log(self):
        cards = [
            child.element_id
            for child in self.bottom_row().children
            if child.tag == "section"
        ]

        self.assertEqual(["report-section", "log-section"], cards)

    def test_the_mux_table_and_the_library_keep_their_places(self):
        order = [node.element_id for node in self.root.walk() if node.element_id]

        self.assertLess(order.index("mux-section"), order.index("report-section"))
        self.assertLess(order.index("log-section"), order.index("library-section"))

    def test_the_report_card_renders_inside_its_swap_target(self):
        (body,) = find_by_id(self.root, "report-section-body")

        self.assertTrue(body.is_inside("report-section"))
        self.assertTrue(body.contains("report-empty"))

    def test_the_log_card_is_unchanged(self):
        (log,) = find_by_id(self.root, "log-section")

        self.assertTrue(log.contains("log-console"))


class ReportActionsTargetExistingElementsTest(unittest.TestCase):
    """A report action whose swap target is missing does nothing at all.

    The analysis button lived in the partial before the card had a home on the
    dashboard, so ``hx-target="#report-section-body"`` pointed at an element that
    did not exist. Every action rendered by the card is checked here against the
    page it is actually delivered in.
    """

    def page_and_card(self, report=None, report_pending_bytes=None):
        _, root = render_dashboard(report=report, report_pending_bytes=report_pending_bytes)
        card = templates.env.get_template("partials/report_card.html").render(
            report=report, report_pending_bytes=report_pending_bytes
        )
        reader = TreeReader()
        reader.feed(card)
        return root, reader.root

    def test_a_pending_capture_can_actually_be_analyzed(self):
        root, card = self.page_and_card(report_pending_bytes=524_288_000)
        (button,) = [
            node for node in card.walk() if node.element_id == "report-analyze-button"
        ]
        target = button.attrs["hx-target"].lstrip("#")

        self.assertEqual(1, len(find_by_id(root, target)), f"#{target} is not on the page")

    def test_a_ready_report_is_labelled_inside_the_bottom_row(self):
        from app.services import report as report_module

        raw = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
        parsed = report_module.parse_tsanalyze_json(
            raw.read_text(encoding="utf-8"), source="file capture.ts"
        )
        _, root = render_dashboard(report=parsed)
        (source,) = find_by_id(root, "report-source")

        self.assertTrue(source.is_inside("report-section-body"))
        self.assertTrue(source.is_inside("report-section"))
        self.assertIn("file capture.ts", render_dashboard(report=parsed)[0])


if __name__ == "__main__":
    unittest.main()
