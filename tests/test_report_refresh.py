"""The report card must follow the capture that just ended.

``openspec/changes/stream-report-ui`` S4, plus finding L3a from real use: a second
recording finished below the gate, the server analysed it, and the screen kept
showing the previous report — the operator had no way to know the analysis had run.

The panel is redrawn over the WebSocket when a capture ends, so the report card
rides along in that same response as an HTMX out-of-band swap. That keeps the whole
contract server-side and testable: no extra request, no JavaScript bookkeeping.
"""

import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.main import templates
from app.services import report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
PANEL = "partials/recorder_panel.html"
REPORT_BODY = "report-section-body"


class Element:
    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children = []

    @property
    def element_id(self):
        return self.attrs.get("id")

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Tags(HTMLParser):
    """A tree, because "inside the out-of-band block" is the question that matters."""

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                      "link", "meta", "source", "track", "wbr"})

    def __init__(self):
        super().__init__()
        self.root = Element("[document]", {})
        self.stack = [self.root]
        self.text = []

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

    def handle_data(self, data):
        cleaned = data.strip()
        if cleaned:
            self.text.append(cleaned)

    def by_id(self, identifier):
        return [node for node in self.root.walk() if node.element_id == identifier]

    def body(self):
        return " ".join(self.text)


def render_panel(report_value=None, pending=None, report_oob=False):
    html = templates.env.get_template(PANEL).render(
        recording_state="idle",
        current_recording=None,
        report=report_value,
        report_pending_bytes=pending,
        report_oob=report_oob,
    )
    markup = Tags()
    markup.feed(html)
    return markup


def delivered_body(markup):
    (body,) = markup.by_id(REPORT_BODY)
    return body


def sample_report():
    return report.parse_tsanalyze_json(
        FIXTURE.read_text(encoding="utf-8"), source="file capture_674MHz_new.ts"
    )


class PanelCarriesTheReportTest(unittest.TestCase):
    """A redraw of the recorder panel also delivers the current report."""

    def test_a_finished_analysis_reaches_the_card(self):
        markup = render_panel(report_value=sample_report(), report_oob=True)
        body = delivered_body(markup)

        self.assertEqual("innerHTML", body.attrs.get("hx-swap-oob"))
        ids = [node.element_id for node in body.walk() if node.element_id]
        self.assertIn("report-content", ids)
        self.assertIn("file capture_674MHz_new.ts", markup.body())

    def test_a_capture_over_the_gate_delivers_the_analyze_button(self):
        """L3a: the operator must see the button appear, not discover it by luck."""

        markup = render_panel(pending=900 * 1024 * 1024, report_oob=True)
        body = delivered_body(markup)

        self.assertEqual("innerHTML", body.attrs.get("hx-swap-oob"))
        self.assertIn("report-analyze-button", [n.element_id for n in body.walk()])

    def test_the_empty_state_is_delivered_too(self):
        """A failed capture clears the previous report; the card must say so."""

        body = delivered_body(render_panel(report_oob=True))
        inner = [
            node.element_id
            for child in body.children
            for node in child.walk()
            if node.element_id
        ]

        self.assertEqual(["report-empty"], inner)


class ThePageNeverDuplicatesItTest(unittest.TestCase):
    """The same partial is included in the dashboard, where no swap exists.

    If the out-of-band block were unconditional, the page would carry a second
    ``#report-section-body`` and a nested copy of the card.
    """

    def test_the_dashboard_include_has_no_out_of_band_block(self):
        markup = render_panel(report_value=sample_report(), report_oob=False)

        self.assertEqual([], markup.by_id(REPORT_BODY))
        self.assertNotIn("hx-swap-oob", str([n.attrs for n in markup.root.walk()]))

    def test_the_dashboard_renders_one_report_body(self):
        from tests.test_dashboard_layout import render_dashboard

        html, root = render_dashboard()
        bodies = [node for node in root.walk() if node.element_id == REPORT_BODY]

        self.assertEqual(1, len(bodies))
        self.assertNotIn("hx-swap-oob", html)


if __name__ == "__main__":
    unittest.main()
