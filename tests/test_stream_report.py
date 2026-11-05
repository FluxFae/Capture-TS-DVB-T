"""Parser for ``tsanalyze --json``, the data source of the stream report.

``openspec/changes/stream-report-ui`` S6 and S7: the report carries the mux
integrity layer and the services layer only, and it can never exist without
naming its source. The fixture is a real capture of the 674 MHz Mediaset mux,
so these assertions describe what TSDuck actually emits, not what we imagined.
"""

import asyncio
import dataclasses
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from app.services import report, tsduck_service

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_674mhz.json"
RAW = FIXTURE.read_text(encoding="utf-8")
SOURCE = "file capture_674MHz_20261105_134617.ts"

NO_TABLES_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tsanalyze_no_tables.json"
NO_TABLES_RAW = NO_TABLES_FIXTURE.read_text(encoding="utf-8")


def parsed() -> report.StreamReport:
    return report.parse_tsanalyze_json(RAW, source=SOURCE)


class IntegrityLayerTest(unittest.TestCase):
    """S7 layer 1: the numbers that answer "is this capture intact?"."""

    def setUp(self):
        self.integrity = parsed().integrity

    def test_the_mux_identity_and_size_are_exposed(self):
        self.assertEqual(901, self.integrity.transport_stream_id)
        self.assertEqual(19, self.integrity.service_count)
        self.assertEqual(54_774_364, self.integrity.byte_count)
        self.assertEqual(17, self.integrity.duration_seconds)
        self.assertEqual("ITA", self.integrity.country)

    def test_the_packet_counters_expose_transport_errors(self):
        packets = self.integrity.packets

        self.assertEqual(291_353, packets.total)
        self.assertEqual(0, packets.invalid_syncs)
        self.assertEqual(0, packets.transport_errors)
        self.assertEqual(0, packets.suspect_ignored)

    def test_the_pid_totals(self):
        pids = self.integrity.pids

        self.assertEqual(61, pids.total)
        self.assertEqual(61, pids.clear)
        self.assertEqual(0, pids.scrambled)
        self.assertEqual(12, pids.with_pcr)
        self.assertEqual(0, pids.unreferenced)

    def test_the_bitrate_is_reported_for_both_packet_sizes(self):
        self.assertEqual(24_752_315, self.integrity.bitrate_188)
        self.assertEqual(26_858_895, self.integrity.bitrate_204)


class ServiceLayerTest(unittest.TestCase):
    """S7 layer 2: the global PID group and, per service, its PIDs."""

    def setUp(self):
        self.report = parsed()

    def test_every_service_in_the_mux_is_present_in_order(self):
        self.assertEqual(
            [101, 102, 103, 104, 105, 106, 107, 114, 115, 116, 120, 121, 122, 123,
             151, 152, 153, 154, 160],
            [service.id for service in self.report.services],
        )

    def test_a_service_carries_its_identity(self):
        service = self.report.services[0]

        self.assertEqual("R101 TV", service.name)
        self.assertEqual("Mediaset", service.provider)
        self.assertEqual(
            "Advanced codec HD digital television service", service.type_name
        )
        self.assertEqual(201, service.pmt_pid)
        self.assertEqual(1031, service.pcr_pid)

    def test_service_pids_are_resolved_to_their_descriptions(self):
        # services[].pids is a list of numbers and the descriptions live in the
        # flat top-level pids array, so the parser has to join the two.
        by_number = {pid.number: pid for pid in self.report.services[0].pids}

        self.assertEqual([201, 1031, 1131, 6054, 7811, 7891], sorted(by_number))
        self.assertEqual(
            "AVC video (1920x1080, high profile, level 4.0, 4:2:0)",
            by_number[1031].description,
        )
        self.assertEqual(2_206_064, by_number[1031].bitrate_188)

    def test_the_global_pid_group_is_kept_separately(self):
        global_group = self.report.global_pids

        self.assertEqual(6, global_group.total)
        self.assertEqual(89_544, global_group.bitrate_188)
        self.assertEqual([0, 16, 17, 18, 20, 21], [pid.number for pid in global_group.pids])
        self.assertEqual("PAT", global_group.pids[0].description)


class CaptureWithoutTablesTest(unittest.TestCase):
    """A stream with no PAT must still produce a report, not an error.

    This is the shape a failed or empty capture takes, and it is exactly what the
    report exists to reveal: turning "nothing decodable" into a parse error would
    hide the one fact the operator needs. The fixture is real tsanalyze output for
    a tableless stream: no ts.id, no country, and no services key at all.
    """

    def setUp(self):
        self.report = report.parse_tsanalyze_json(
            NO_TABLES_RAW, source="file synthetic.ts"
        )

    def test_the_report_is_built_instead_of_raising(self):
        self.assertEqual(0, self.report.integrity.transport_stream_id)
        self.assertEqual("", self.report.integrity.country)
        self.assertEqual((), self.report.services)

    def test_the_packet_and_pid_counts_are_still_reported(self):
        self.assertEqual(15_980_000, self.report.integrity.byte_count)
        self.assertEqual(85_000, self.report.integrity.packets.total)
        self.assertEqual(1, self.report.integrity.pids.total)

    def test_the_global_pid_group_holds_only_the_pat(self):
        self.assertEqual(1, self.report.global_pids.total)
        self.assertEqual([0], [pid.number for pid in self.report.global_pids.pids])
        self.assertEqual("PAT", self.report.global_pids.pids[0].description)


class SourceLabelTest(unittest.TestCase):
    """S6: a report always knows where it came from."""

    def test_the_parser_keeps_the_source_it_was_given(self):
        self.assertEqual(SOURCE, parsed().source)

    def test_the_source_has_no_default_value(self):
        field = report.StreamReport.__dataclass_fields__["source"]

        self.assertIs(dataclasses.MISSING, field.default)
        self.assertIs(dataclasses.MISSING, field.default_factory)


class LayerBoundaryTest(unittest.TestCase):
    """S7 renders layers 1 and 2; layers 3 and 4 must never reach the model."""

    EXACT_FIELDS = {
        "StreamReport": {"source", "integrity", "global_pids", "services"},
        "Integrity": {
            "transport_stream_id",
            "service_count",
            "byte_count",
            "duration_seconds",
            "country",
            "bitrate_188",
            "bitrate_204",
            "packets",
            "pids",
        },
        "PacketCounters": {"total", "invalid_syncs", "transport_errors", "suspect_ignored"},
        "PidTotals": {"total", "clear", "scrambled", "with_pcr", "unreferenced"},
        "GlobalPids": {"total", "bitrate_188", "pids"},
        "Service": {
            "id",
            "name",
            "provider",
            "type_name",
            "pmt_pid",
            "pcr_pid",
            "bitrate_188",
            "pids",
        },
        "PidInfo": {"number", "description", "bitrate_188"},
    }

    def test_the_model_carries_exactly_the_two_rendered_layers(self):
        for name, expected in self.EXACT_FIELDS.items():
            with self.subTest(model=name):
                fields = set(getattr(report, name).__dataclass_fields__)
                self.assertEqual(expected, fields)


class ParseFailureTest(unittest.TestCase):
    """A broken document becomes a typed error, never half a report."""

    def test_malformed_json_is_a_typed_error(self):
        with self.assertRaises(report.ReportParseError):
            report.parse_tsanalyze_json("{not json", source=SOURCE)

    def test_a_document_without_the_expected_shape_is_a_typed_error(self):
        with self.assertRaises(report.ReportParseError):
            report.parse_tsanalyze_json("{}", source=SOURCE)

    def test_a_json_document_that_is_not_an_object_is_a_typed_error(self):
        with self.assertRaises(report.ReportParseError):
            report.parse_tsanalyze_json("[1, 2, 3]", source=SOURCE)


class RunnerTest(unittest.TestCase):
    """The runner asks tsanalyze for JSON and labels the source as a file."""

    def test_it_asks_tsanalyze_for_json_and_names_the_file(self):
        service = tsduck_service.TSDuckService(recordings_dir=FIXTURE.parent)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=RAW, stderr=""
        )

        with mock.patch.object(
            tsduck_service.subprocess, "run", return_value=completed
        ) as run:
            result = asyncio.run(service.analyze_report_file(str(FIXTURE)))

        self.assertEqual(["tsanalyze", "--json", str(FIXTURE)], run.call_args[0][0])
        self.assertEqual(f"file {FIXTURE.name}", result.source)
        self.assertEqual(901, result.integrity.transport_stream_id)

    def test_a_failed_run_is_a_typed_error(self):
        service = tsduck_service.TSDuckService(recordings_dir=FIXTURE.parent)
        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="cannot open input file"
        )

        with mock.patch.object(
            tsduck_service.subprocess, "run", return_value=completed
        ), self.assertRaises(report.ReportParseError):
            asyncio.run(service.analyze_report_file("missing.ts"))


if __name__ == "__main__":
    unittest.main()
