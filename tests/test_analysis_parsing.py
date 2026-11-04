"""Mux analysis parsing tests — M1.

``tsanalyze --normalized`` separates fields with ':', but field *values* may
contain a colon too (the NIT description literally reads
``NIT (Network: 0x3001 (12289) Mediaset1)``). Splitting the line on every colon
truncates the value, which is why the network name and the country never
reached the UI.

The fixtures below are copied verbatim from a real capture of the Mediaset mux
at 674 MHz (adapter 0, DVB-T, 8 MHz) so the parser is pinned to the format the
tool actually emits, not to a guess about it.
"""

import unittest

from app.services.tsduck_service import TSDuckService

TS_LINE = (
    "ts:id=901:services=19:clearservices=19:scrambledservices=0:pids=61:"
    "clearpids=61:scrambledpids=0:pcrpids=12:unreferencedpids=0:packets=208798:"
    "invalidsyncs=0:transporterrors=0:suspectignored=0:bytes=39254024:"
    "bitrate=24698902:bitrate204=26800936:userbitrate=24714580:"
    "userbitrate204=26817948:pcrbitrate=24698902:pcrbitrate204=26800936:"
    "duration=12:country=ITA:"
)

TOT_LINE = (
    "time:local:tot:first:date=04/11/2026:time=13h51m12s:"
    "secondsince2000=847115472:country=ITA:"
)

NIT_LINE = (
    "pid:pid=16:access=clear:servcount=0:global:bitrate=946:bitrate204=1027:"
    "packets=8:clear=8:scrambled=0:invalidscrambling=0:af=0:pcr=0:pts=0:dts=0:"
    "pcrleap=0:ptsleap=0:dtsleap=0:discontinuities=0:duplicated=0:unitstart=8:"
    "description=NIT (Network: 0x3001 (12289) Mediaset1)"
)

SERVICE_LINE = (
    "service:id=101:tsid=901:orignetwid=272:access=clear:pids=6:clearpids=6:"
    "scrambledpids=0:packets=15428:bitrate=1824992:bitrate204=1980310:"
    "servtype=25:pmtpid=201:pcrpid=1031:pidlist=201,1031,1131,6054,7811,7891:"
    "provider=Mediaset:name=R101 TV"
)


def analyze(*lines):
    return TSDuckService()._parse_normalized_analysis("\n".join(lines))


class NetworkNameTest(unittest.TestCase):
    def test_network_name_survives_the_colon_inside_the_nit_description(self):
        mux = analyze(TS_LINE, SERVICE_LINE, NIT_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.network_name, "Mediaset1")

    def test_network_name_is_empty_when_there_is_no_nit_line(self):
        mux = analyze(TS_LINE, SERVICE_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.network_name, "")


class CountryCodeTest(unittest.TestCase):
    def test_country_code_comes_from_the_ts_line(self):
        mux = analyze(TS_LINE, SERVICE_LINE, NIT_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.country_code, "ITA")

    def test_country_code_falls_back_to_the_tot_line(self):
        ts_without_country = TS_LINE.replace("country=ITA:", "")
        mux = analyze(ts_without_country, TOT_LINE, SERVICE_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.country_code, "ITA")

    def test_country_code_is_empty_when_no_source_reports_it(self):
        mux = analyze(TS_LINE.replace("country=ITA:", ""), SERVICE_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.country_code, "")


class ServiceParsingTest(unittest.TestCase):
    def test_real_service_line_parses_every_field(self):
        mux = analyze(TS_LINE, SERVICE_LINE)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.ts_id, 901)
        self.assertEqual(mux.original_network_id, 272)
        self.assertEqual(mux.total_bitrate, 24698902)
        self.assertEqual(len(mux.services), 1)

        service = mux.services[0]
        self.assertEqual(service.service_id, 101)
        self.assertEqual(service.service_name, "R101 TV")
        self.assertEqual(service.provider, "Mediaset")
        self.assertEqual(service.pmt_pid, 201)
        self.assertEqual(service.pcr_pid, 1031)
        self.assertEqual(service.bitrate, 1824992)

    def test_colon_inside_a_service_name_is_preserved(self):
        line = SERVICE_LINE.replace("name=R101 TV", "name=Studio:1 HD")
        mux = analyze(TS_LINE, line)

        self.assertIsNotNone(mux)
        self.assertEqual(mux.services[0].service_name, "Studio:1 HD")

    def test_trailing_record_separator_is_not_part_of_the_value(self):
        mux = analyze(TS_LINE, SERVICE_LINE + ":")

        self.assertIsNotNone(mux)
        self.assertEqual(mux.services[0].service_name, "R101 TV")


if __name__ == "__main__":
    unittest.main()
