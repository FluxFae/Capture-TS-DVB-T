"""Typed model and parser for the ``tsanalyze --json`` stream report.

``openspec/changes/stream-report-ui`` S6 and S7: the report carries the mux
integrity layer and the services layer only, and it cannot exist without naming
its source (``live 674 MHz`` or ``file capture_....ts``).

The document does not nest a service's PID descriptions: ``services[].pids`` is a
list of PID numbers and the descriptions live in the flat top-level ``pids``
array, so parsing joins the two. Layers 3 and 4 of the reference report (PID
analysis table, per-PID section repetition) are deliberately absent.
"""

import json
from dataclasses import dataclass


class ReportParseError(ValueError):
    """tsanalyze produced something this parser cannot turn into a report."""


@dataclass(frozen=True)
class PacketCounters:
    total: int
    invalid_syncs: int
    transport_errors: int
    suspect_ignored: int


@dataclass(frozen=True)
class PidTotals:
    total: int
    clear: int
    scrambled: int
    with_pcr: int
    unreferenced: int


@dataclass(frozen=True)
class Integrity:
    """Layer 1: the numbers that answer "is this capture intact?"."""

    transport_stream_id: int
    service_count: int
    byte_count: int
    duration_seconds: int
    country: str
    bitrate_188: int
    bitrate_204: int
    packets: PacketCounters
    pids: PidTotals


@dataclass(frozen=True)
class PidInfo:
    number: int
    description: str
    bitrate_188: int


@dataclass(frozen=True)
class GlobalPids:
    """Layer 2, first block: the PIDs that belong to the mux itself."""

    total: int
    bitrate_188: int
    pids: tuple[PidInfo, ...]


@dataclass(frozen=True)
class Service:
    """Layer 2, second block: one service and the PIDs it carries."""

    id: int
    name: str
    provider: str
    type_name: str
    pmt_pid: int
    pcr_pid: int
    bitrate_188: int
    pids: tuple[PidInfo, ...]


@dataclass(frozen=True)
class StreamReport:
    source: str
    integrity: Integrity
    global_pids: GlobalPids
    services: tuple[Service, ...]


def parse_tsanalyze_json(raw: str, *, source: str) -> StreamReport:
    """Turn ``tsanalyze --json`` output into a report.

    ``source`` is mandatory: a report that cannot say where it came from would
    mix "now" and "what I captured", which is the defect S6 exists to prevent.
    """
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReportParseError(f"tsanalyze did not return JSON: {error}") from error

    if not isinstance(document, dict):
        raise ReportParseError("tsanalyze returned JSON that is not an object")

    try:
        return _build_report(document, source)
    except (KeyError, TypeError, ValueError) as error:
        raise ReportParseError(f"unexpected tsanalyze document: {error}") from error


def _build_report(document: dict, source: str) -> StreamReport:
    summary = document["ts"]
    packet_counters = summary["packets"]
    pid_totals = summary["pids"]
    known_pids = {entry["id"]: _pid_info(entry) for entry in document.get("pids") or []}

    return StreamReport(
        source=source,
        integrity=Integrity(
            # A stream without a PAT has no transport stream id to report, and
            # that absence is itself the finding, so it reads as 0 instead of
            # refusing to build the report.
            transport_stream_id=summary.get("id", 0),
            service_count=summary["services"]["total"],
            byte_count=summary["bytes"],
            duration_seconds=summary["duration"],
            country=summary.get("country", ""),
            bitrate_188=summary["bitrate"],
            bitrate_204=summary["bitrate-204"],
            packets=PacketCounters(
                total=packet_counters["total"],
                invalid_syncs=packet_counters["invalid-syncs"],
                transport_errors=packet_counters["transport-errors"],
                suspect_ignored=packet_counters["suspect-ignored"],
            ),
            pids=PidTotals(
                total=pid_totals["total"],
                clear=pid_totals["clear"],
                scrambled=pid_totals["scrambled"],
                with_pcr=pid_totals["pcr"],
                unreferenced=pid_totals["unreferenced"],
            ),
        ),
        global_pids=GlobalPids(
            total=pid_totals["global"]["total"],
            bitrate_188=pid_totals["global"]["bitrate"],
            pids=tuple(
                known_pids[number]
                for number in pid_totals["global"].get("pids", [])
                if number in known_pids
            ),
        ),
        services=tuple(
            _service(entry, known_pids) for entry in document.get("services") or []
        ),
    )


def _service(entry: dict, known_pids: dict[int, PidInfo]) -> Service:
    return Service(
        id=entry["id"],
        name=entry.get("name", ""),
        provider=entry.get("provider", ""),
        type_name=entry.get("type-name", ""),
        pmt_pid=entry.get("pmt-pid", 0),
        pcr_pid=entry.get("pcr-pid", 0),
        bitrate_188=entry.get("bitrate", 0),
        pids=tuple(
            known_pids[number] for number in entry.get("pids", []) if number in known_pids
        ),
    )


def _pid_info(entry: dict) -> PidInfo:
    return PidInfo(
        number=entry["id"],
        description=entry.get("description", ""),
        bitrate_188=entry.get("bitrate", 0),
    )
