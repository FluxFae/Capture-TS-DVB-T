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


class Cell:
    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.text = []

    @property
    def classes(self):
        return set((self.attrs.get("class") or "").split())

    @property
    def alignment(self):
        """The one alignment class that matters to the eye, if any."""
        for name in ("flux-table__th--right", "flux-table__td--right"):
            if name in self.classes:
                return "right"
        return "left"

    def content(self):
        return " ".join(self.text)


class TableReader(HTMLParser):
    """Collects table sections as cells, with their classes and text."""

    def __init__(self):
        super().__init__()
        self.headers = []
        self.rows = []
        self.cell = None
        self.row = None
        self.in_thead = False

    def handle_starttag(self, tag, attrs):
        if tag == "thead":
            self.in_thead = True
        elif tag == "tr":
            self.row = []
            if not self.in_thead:
                self.rows.append(self.row)
        elif tag in {"th", "td"}:
            self.cell = Cell(tag, dict(attrs))
            if self.in_thead:
                self.headers.append(self.cell)
            elif self.row is not None:
                self.row.append(self.cell)

    def handle_endtag(self, tag):
        if tag == "thead":
            self.in_thead = False
        elif tag in {"th", "td"}:
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            cleaned = data.strip()
            if cleaned:
                self.cell.text.append(cleaned)


def render_table(report_value) -> TableReader:
    reader = TableReader()
    reader.feed(templates.env.get_template(CARD).render(report=report_value))
    return reader


class ServicesTableTest(unittest.TestCase):
    """L3b/L3c: the table is read by eye, so alignment and content both matter."""

    def setUp(self):
        self.table = render_table(sample_report("file capture_674MHz.ts"))

    def test_the_table_is_present(self):
        self.assertTrue(self.table.rows, "no services table rendered")

    def test_each_header_is_aligned_with_its_own_column(self):
        for index, header in enumerate(self.table.headers):
            with self.subTest(column=header.content()):
                data = self.table.rows[0][index]
                self.assertEqual(header.alignment, data.alignment)

    def test_the_provider_is_not_repeated_on_every_row(self):
        # The provider string also lives inside a service name ("Mediaset Italia2
        # HD"), so what must be gone is the dedicated cell, not every occurrence.
        cells = [cell.content() for row in self.table.rows for cell in row]

        self.assertNotIn("Mediaset", cells)
        self.assertNotIn("PROVIDER", [h.content().upper() for h in self.table.headers])

    def test_a_service_shows_the_video_and_audio_it_carries(self):
        first = " ".join(cell.content() for cell in self.table.rows[0])

        self.assertIn("AVC 1920x1080", first)
        self.assertIn("MPEG-4 AAC ita", first)

    def test_the_pid_column_explains_what_it_counts(self):
        headers = {header.content().upper(): header for header in self.table.headers}
        pid_header = next(h for text, h in headers.items() if text.startswith("PIDs".upper()))

        self.assertIn("shared", (pid_header.attrs.get("title") or "").lower())

    def test_the_unique_pid_total_is_labelled_as_unique(self):
        rendered = templates.env.get_template(CARD).render(
            report=sample_report("file capture_674MHz.ts")
        )

        self.assertIn("PIDs (unique)", rendered)
