"""The report card renders one state at a time and always names its source.

``openspec/changes/stream-report-ui`` S2, S5, S6 and S7. The card is rendered from
a template with three mutually exclusive states, so these tests parse the produced
HTML instead of matching strings: a state that leaks into another one is a defect
the operator would see.
"""

import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.main import templates
from app.services import report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
RAW = FIXTURE.read_text(encoding="utf-8")
NO_TABLES = (Path(__file__).resolve().parent / "fixtures" / "tsanalyze_no_tables.json")

CARD = "partials/report_card.html"


class Markup(HTMLParser):
    """Collects the ids, buttons and visible text of a rendered fragment."""

    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.buttons: list[str | None] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        identifier = attributes.get("id")
        if identifier:
            self.ids.append(identifier)
        if tag == "button":
            self.buttons.append(identifier)

    def handle_data(self, data):
        cleaned = data.strip()
        if cleaned:
            self.text.append(cleaned)

    def body(self) -> str:
        return " ".join(self.text)


def render(**context) -> Markup:
    markup = Markup()
    markup.feed(templates.env.get_template(CARD).render(**context))
    return markup


def sample_report(source: str) -> report.StreamReport:
    return report.parse_tsanalyze_json(RAW, source=source)


class ReadyStateTest(unittest.TestCase):
    """S6/S7: a report renders both layers and exactly one source label."""

    def setUp(self):
        self.markup = render(report=sample_report("file capture_674MHz.ts"))

    def test_it_names_the_source_exactly_once(self):
        self.assertEqual(1, self.markup.ids.count("report-source"))
        self.assertIn("file capture_674MHz.ts", self.markup.body())

    def test_it_renders_the_integrity_layer(self):
        body = self.markup.body()

        self.assertIn("291353", body, "the packet total is missing")
        self.assertIn("Transport errors", body)
        self.assertIn("Invalid sync", body)
        self.assertIn("61", body, "the PID total is missing")

    def test_it_renders_the_services_layer(self):
        body = self.markup.body()

        self.assertIn("R101 TV", body)
        self.assertIn("Mediaset", body)

    def test_it_shows_no_generate_control(self):
        # S5: a report that exists is never behind a generate button, no matter
        # whether it came from a file or from the live mux.
        self.assertEqual([], self.markup.buttons)

    def test_the_other_states_do_not_leak_in(self):
        self.assertNotIn("report-empty", self.markup.ids)
        self.assertNotIn("report-pending", self.markup.ids)


class PendingStateTest(unittest.TestCase):
    """S4: over the gate the card offers the action and shows nothing else."""

    def setUp(self):
        self.markup = render(report=None, report_pending_bytes=524_288_000)

    def test_it_offers_the_analyze_action(self):
        self.assertIn("report-analyze-button", self.markup.ids)
        self.assertEqual(["report-analyze-button"], self.markup.buttons)

    def test_it_never_shows_a_report_without_a_source(self):
        self.assertNotIn("report-source", self.markup.ids)
        self.assertNotIn("report-content", self.markup.ids)

    def test_the_empty_state_does_not_leak_in(self):
        self.assertNotIn("report-empty", self.markup.ids)


class EmptyStateTest(unittest.TestCase):
    def test_it_offers_nothing_to_press(self):
        markup = render(report=None, report_pending_bytes=None)

        self.assertIn("report-empty", markup.ids)
        self.assertEqual([], markup.buttons)
        self.assertNotIn("report-source", markup.ids)


class CaptureWithoutTablesTest(unittest.TestCase):
    """A capture with no tables is a finding, not a silence (S7)."""

    def test_it_says_no_service_was_decoded_and_still_names_the_source(self):
        empty = report.parse_tsanalyze_json(
            NO_TABLES.read_text(encoding="utf-8"), source="file synthetic.ts"
        )
        markup = render(report=empty)

        self.assertIn("report-no-services", markup.ids)
        self.assertEqual(1, markup.ids.count("report-source"))
        self.assertIn("file synthetic.ts", markup.body())


if __name__ == "__main__":
    unittest.main()
