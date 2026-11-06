"""
TSDuck service layer — wraps CLI commands and Python API for DVB-T operations.

Uses subprocess for analysis/scan commands (tsanalyze, tslsdvb) and the
TSDuck Python API (TSProcessor) for recording, since it gives us event
callbacks and programmatic control.
"""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.services import report
from app.services.devices import DVBDevice

# A normalized record is a list of ``key=value`` fields joined by ':'. Only a
# colon that is immediately followed by a lowercase key and '=' separates
# fields; any other colon belongs to the value (e.g. a NIT description).
_NORMALIZED_FIELD_SEPARATOR = re.compile(r":(?=[a-z][a-z0-9_]*=)")

# Captures belong to the project, not to the directory the process happens to
# be started from: resolve them from this file, never from the working dir.
DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "recordings"

# Below this size a file holds container tables only, never a service payload:
# tsp writes 188 bytes and exits 0 when a zap filter matches nothing.
EMPTY_CAPTURE_BYTES = 4096

# A report is a full scan of the file. Measured at roughly 7 ms per MB, so a
# capture at the 512 MiB automatic-analysis gate costs a few seconds and this
# leaves a wide margin for a slow disk.
REPORT_TIMEOUT_SECONDS = 120

# A finished capture builds its report on its own only while it stays small.
# Measured at roughly 7 ms per MB, so 512 MiB is about 4 s, while a 30 minute
# unlimited capture at 24.9 Mbit/s is ~5.6 GB and ~40 s: a silent wait that
# long reads as a hang, so above this the operator asks for it explicitly.
AUTO_ANALYZE_MAX_BYTES = 512 * 1024 * 1024


def should_analyze_automatically(file_size: int) -> bool:
    """Whether a finished capture is analysed without being asked (S4)."""
    return 0 < file_size <= AUTO_ANALYZE_MAX_BYTES


def reveal_command(path: Path, platform: str | None = None) -> list[str] | None:
    """The file-manager command that reveals ``path``, or ``None`` (S9).

    Pure and platform-parameterised on purpose: what gets launched is the risky
    part of the action, so it is decided here and tested without spawning
    anything. ``platform`` defaults to ``sys.platform`` and exists only so the
    other platforms can be pinned from a test.

    Windows and macOS can select the file itself. ``xdg-open`` has no "select
    this file" verb, so on Linux the containing folder is opened: an honest,
    smaller promise instead of a silent lie in the button's label.
    """
    target = sys.platform if platform is None else platform
    if target == "win32":
        # Explorer's own switches use a comma right after /select with no space,
        # and it exits non-zero even when it succeeds, so its status is never read.
        return ["explorer", f"/select,{path}"]
    if target == "darwin":
        return ["open", "-R", str(path)]
    if target.startswith(("linux", "freebsd")):
        return ["xdg-open", str(path.parent)]
    return None


class NoServicesSelectedError(ValueError):
    """Raised when a service filter is requested but names no service."""


class RevealOutcome(str, Enum):
    """What happened when the operator asked to see a recording (S9)."""

    REVEALED = "revealed"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class RecordingState(str, Enum):
    IDLE = "idle"
    TUNING = "tuning"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"




@dataclass
class ServiceInfo:
    service_id: int
    service_name: str
    provider: str
    service_type: str
    service_type_desc: str
    pmt_pid: int
    pcr_pid: int
    bitrate: int
    pids: list[dict]


@dataclass
class MuxInfo:
    ts_id: int
    original_network_id: int
    network_name: str
    services: list[ServiceInfo]
    total_bitrate: int
    country_code: str


@dataclass
class RecordingInfo:
    """One capture: its target and its live counters.

    ``duration_limit`` is the number of seconds the user asked for (``None``
    means unlimited); the panel redraws from it so the typed value is not lost.
    """


    filename: str
    filepath: str
    frequency: int
    bandwidth: int
    delivery_system: str
    started_at: datetime
    duration_seconds: float = 0
    file_size_bytes: int = 0
    state: RecordingState = RecordingState.IDLE
    services_filter: list[int] = field(default_factory=list)
    mux_info: MuxInfo | None = None
    last_error: str = ""
    duration_limit: int | None = None


class TSDuckService:
    """Orchestrates all TSDuck operations for the application."""

    def __init__(
        self,
        recordings_dir: str | Path | None = None,
        poll_interval: float = 0.5,
    ):
        """Create the service.

        ``recordings_dir`` defaults to ``recordings/`` next to the project, not
        next to whatever directory the process was started from: a relative
        default silently wrote captures somewhere else and left the library
        listing a different folder. An explicit directory is still honoured
        as given and resolved to an absolute path.

        ``poll_interval`` is how often the recording monitor polls the tsp
        process and the output file. The soloHD class tuners need several
        seconds to lock, so this interval affects responsiveness only.
        """
        self.recordings_dir = Path(recordings_dir or DEFAULT_RECORDINGS_DIR).resolve()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval

        self._current_recording: RecordingInfo | None = None
        self._tsp_process: subprocess.Popen | None = None
        self._recording_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_callback: Callable[[str], None] | None = None
        self._state_callback: Callable[[dict], None] | None = None
        self._report_callback: Callable[[dict], None] | None = None
        self._current_report: report.StreamReport | None = None
        self._report_pending_bytes: int | None = None
        # The file the card is talking about, analysed or still waiting for the
        # operator. A report outlives a page reload on purpose, but never its own
        # file (L6).
        self._report_target: Path | None = None

    @property
    def current_report(self) -> report.StreamReport | None:
        """The report of the last capture that finished, if one was built.

        The report survives a reload because it is process state, so it also has to
        die with its file: a card still claiming an analysis of a deleted recording
        is indistinguishable from a real one (L6).
        """
        self._drop_report_if_file_is_gone()
        return self._current_report

    @property
    def report_pending_bytes(self) -> int | None:
        """Size of a finished capture waiting for the operator to ask for it."""
        self._drop_report_if_file_is_gone()
        return self._report_pending_bytes

    def _forget_report(self) -> None:
        self._current_report = None
        self._report_pending_bytes = None
        self._report_target = None

    def _drop_report_if_file_is_gone(self) -> None:
        if self._report_target is None:
            return
        if not self._report_target.exists():
            self._forget_report()

    @property
    def state(self) -> RecordingState:
        if self._current_recording:
            return self._current_recording.state
        return RecordingState.IDLE

    @property
    def current_recording(self) -> RecordingInfo | None:
        """The capture the telemetry describes, or ``None`` when it lost its file.

        The counters describe a recorded file, so a finished capture whose file is
        gone must not keep claiming megabytes and seconds after a reload (L8a).
        Guarded at read time like the report (L6), so no code path can leave the
        card lying about a file that no longer exists.

        Two states are deliberately untouched: a capture that is still running
        (``tsp`` writes the file a moment after tuning, so a missing file is the
        normal gap before the first byte) and a failure that never produced a file
        (that recording carries the error the panel has to show).
        """
        recording = self._current_recording
        if recording is not None and self._finished_capture_is_gone(recording):
            self._current_recording = None
            return None
        return recording

    @staticmethod
    def _finished_capture_is_gone(recording: RecordingInfo) -> bool:
        """Whether this recording is a finished capture whose file disappeared."""
        if recording.state is not RecordingState.IDLE or recording.file_size_bytes == 0:
            return False
        return not Path(recording.filepath).is_file()

    def set_log_callback(self, callback: Callable[[str], None]) -> None:
        self._log_callback = callback

    def set_state_callback(self, callback: Callable[[dict], None]) -> None:
        self._state_callback = callback

    def set_report_callback(self, callback: Callable[[dict], None]) -> None:
        """Register the announcement that "the report changed".

        The card cannot be told by the recording state: a capture emits its
        finished ``IDLE`` state before the file is analysed, so a redraw driven by
        that state always shows the previous report. The analysis itself announces,
        after ``_current_report`` is the truth (S4, finding L3a).
        """
        self._report_callback = callback

    def _emit_report(self) -> None:
        if self._report_callback is None:
            return
        try:
            self._report_callback(self._get_report_status())
        except Exception as error:  # pragma: no cover - defensive
            self._emit_log(f"Report announcement failed: {error}", "warning")

    def _emit_log(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [{level.upper()}] {message}"
        if self._log_callback:
            self._log_callback(entry)

    def _emit_state(self, data: dict) -> None:
        if self._state_callback:
            self._state_callback(data)

    # ──────────────────────────────────────────────
    # Device discovery
    # ──────────────────────────────────────────────

    async def list_devices(self) -> list[DVBDevice]:
        """List available DVB tuner devices using tslsdvb CLI."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["tslsdvb"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            devices = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Pattern: 0: "Hauppauge WinTV-soloHD DVBC Tuner" (DVB-C, DVB-C/C)
                match = re.match(
                    r'(\d+):\s+"(.+?)"\s+\((.+?)\)', line
                )
                if match:
                    idx = int(match.group(1))
                    name = match.group(2)
                    systems = [s.strip() for s in match.group(3).split(",")]
                    devices.append(DVBDevice(index=idx, name=name, delivery_systems=systems))
            return devices
        except Exception as e:
            self._emit_log(f"Error listing devices: {e}", "error")
            return []

    # ──────────────────────────────────────────────
    # Mux analysis
    # ──────────────────────────────────────────────

    async def analyze_mux(
        self,
        frequency: int,
        bandwidth: int = 8,
        adapter: int = 1,
        delivery_system: str = "DVB-T",
        duration_seconds: int = 10,
    ) -> MuxInfo | None:
        """
        Tune to a frequency and analyze the transport stream.
        Returns structured MuxInfo with services, network info, etc.
        Uses tsanalyze via tsp CLI for reliable structured output.
        """
        self._emit_log(f"Analyzing mux at {frequency/1_000_000:.1f} MHz...")
        self._emit_state({"action": "analyzing", "frequency": frequency})

        freq_hz = frequency if frequency > 1_000_000 else frequency * 1_000_000

        cmd = [
            "tsp",
            "-I", "dvb",
            "-a", str(adapter),
            "-f", str(freq_hz),
            "--bandwidth", str(bandwidth),
            "--delivery-system", delivery_system,
            "--signal-timeout", "8",
            "-P", "until", "--seconds", str(duration_seconds),
            "-P", "analyze", "--normalized",
            "-O", "drop",
        ]

        try:
            self._emit_log(f"Running: {' '.join(cmd)}")
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=duration_seconds + 30,
            )

            if result.returncode != 0 and not result.stdout:
                self._emit_log(f"Analysis failed: {result.stderr}", "error")
                return None

            output = result.stdout + result.stderr
            return self._parse_normalized_analysis(output)

        except subprocess.TimeoutExpired:
            self._emit_log("Analysis timeout — no signal?", "error")
            return None
        except Exception as e:
            self._emit_log(f"Analysis error: {e}", "error")
            return None

    async def analyze_file(self, filepath: str) -> MuxInfo | None:
        """Analyze an existing .ts file and return structured MuxInfo."""
        self._emit_log(f"Analyzing file: {filepath}")

        cmd = ["tsanalyze", "--normalized", filepath]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout + result.stderr
            return self._parse_normalized_analysis(output)
        except Exception as e:
            self._emit_log(f"File analysis error: {e}", "error")
            return None

    async def analyze_report_file(self, filepath: str) -> report.StreamReport:
        """Analyze an existing .ts file into the stream report model.

        Uses ``tsanalyze --json``: the colon-delimited ``--normalized`` output
        truncates any value that contains a colon, and the report needs nested
        data anyway. A failed run raises instead of returning a partial report,
        so the caller can show an error state rather than invented numbers.
        """
        self._emit_log(f"Analyzing report: {filepath}")
        return await asyncio.to_thread(self._run_tsanalyze_report, filepath)

    def _run_tsanalyze_report(self, filepath: str | Path) -> report.StreamReport:
        """Run ``tsanalyze --json`` and parse it. Blocking on purpose.

        The asynchronous entry point wraps this in a thread, and the recording
        monitor calls it directly once its capture has already finished.
        """
        result = subprocess.run(
            ["tsanalyze", "--json", str(filepath)],
            capture_output=True,
            text=True,
            timeout=REPORT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip() or "no output"
            raise report.ReportParseError(f"tsanalyze failed for {filepath}: {detail}")

        return report.parse_tsanalyze_json(
            result.stdout, source=f"file {Path(filepath).name}"
        )

    def _analyze_finished_capture(self, filepath: Path) -> None:
        """Build the report for a capture that just ended well (S4).

        Called from the recording monitor after the capture has finished, so a
        report never races the file being written. A report that cannot be built
        must not disturb the recorder: the failure is logged and the capture is
        still reported as successful.

        The announcement is in a ``finally`` on purpose: the card has to be told
        about every outcome — a report, a capture parked above the gate, or a
        failure — because the alternative is a screen still showing a report that
        is no longer the current one (finding L3a).
        """
        self._current_report = None
        self._report_pending_bytes = None

        try:
            self._build_report_for(filepath)
        finally:
            self._emit_report()
    def _build_report_for(self, filepath: Path) -> None:
        try:
            file_size = filepath.stat().st_size
        except OSError as error:
            self._emit_log(f"Report skipped: {error}", "warning")
            return

        if not should_analyze_automatically(file_size):
            self._report_pending_bytes = file_size
            self._report_target = Path(filepath).resolve()
            self._emit_log(
                f"Capture is {self._format_size(file_size)}; "
                "analyze it on request to build the report",
                "info",
            )
            return

        self._emit_log(f"Analyzing capture: {filepath.name}")
        try:
            self._current_report = self._run_tsanalyze_report(filepath)
            self._report_target = Path(filepath).resolve()
        except Exception as error:
            # Best-effort background step: a broken report must never turn a
            # successful capture into a failure or kill the monitor thread.
            self._emit_log(f"Report failed: {error}", "error")
            return

        integrity = self._current_report.integrity
        level = "success" if integrity.packets.transport_errors == 0 else "warning"
        self._emit_log(
            f"Report ready: {integrity.service_count} services, "
            f"{integrity.packets.transport_errors} transport errors",
            level,
        )

    async def analyze_recording(self, filename: str) -> report.StreamReport | None:
        """Build the report for one recording the operator picked (S10).

        Any recording in the library can be analysed, not only the one that just
        finished: the auto path is a convenience, not the only way in. The name is
        resolved inside ``recordings_dir`` before anything runs, and a report that
        cannot be built clears the previous one instead of leaving the operator
        reading a report about a different file.
        """
        candidate = self._resolve_recording(filename)
        if candidate is None:
            self._emit_log(f"Refused to analyze {filename!r}: not a recording", "warning")
            return None

        self._forget_report()
        try:
            # Blocking on purpose inside a thread: a 5 GB capture takes ~40 s and
            # the event loop has a recording to poll.
            return await asyncio.to_thread(self._build_named_report, candidate)
        finally:
            # Same rule as a finished capture: whenever the report changes, the
            # card is told, including when the change is "there is no report".
            self._emit_report()

    def _build_named_report(self, candidate: Path) -> report.StreamReport | None:
        self._emit_log(f"Analyzing recording: {candidate.name}")
        try:
            self._current_report = self._run_tsanalyze_report(candidate)
            self._report_target = candidate
        except Exception as error:
            self._emit_log(f"Report failed for {candidate.name}: {error}", "error")
            return None

        integrity = self._current_report.integrity
        level = "success" if integrity.packets.transport_errors == 0 else "warning"
        self._emit_log(
            f"Report ready: {integrity.service_count} services, "
            f"{integrity.packets.transport_errors} transport errors",
            level,
        )
        return self._current_report

    async def analyze_pending_capture(self) -> report.StreamReport | None:
        """Build the report the size gate left for the operator (S4).

        Returns the report it built, the one already built, or ``None`` when
        there is nothing to analyze. A failure is logged and reported as no
        report, never as a fabricated one.
        """
        if self._current_report is not None:
            return self._current_report

        rec = self._current_recording
        if rec is None or not rec.filepath:
            return None

        filepath = Path(rec.filepath)
        if not filepath.exists():
            return None

        self._emit_log(f"Analyzing capture on request: {filepath.name}")
        try:
            self._current_report = await asyncio.to_thread(
                self._run_tsanalyze_report, filepath
            )
        except Exception as error:
            self._emit_log(f"Report failed: {error}", "error")
            return None

        self._report_pending_bytes = None
        return self._current_report

    def _parse_normalized_analysis(self, output: str) -> MuxInfo | None:
        """Parse tsanalyze --normalized output into MuxInfo.

        Normalized format uses ':' as field separator:
            ts:id=901:pkts=123456:bitrate=22299673
            service:id=101:tsid=901:orignetwid=272:name=Canale5 HD FACTORY
            pid:pid=16:description=NIT (Network: 0x3001 (12289) mediaset1)
        """
        ts_id = 0
        onid = 0
        network_name = ""
        country_code = ""
        total_bitrate = 0
        services: list[ServiceInfo] = []

        # Join continuation lines (lines that don't start with a known prefix
        # are continuations of the previous line, e.g. multi-word service names)
        joined_lines = []
        known_prefixes = (
            "ts:",
            "service:",
            "pid:",
            "time:",
            "table:",
            "global:",
            "unreferenced:",
            "title:",
        )
        for raw_line in output.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if any(stripped.startswith(p) for p in known_prefixes):
                joined_lines.append(stripped)
            elif joined_lines:
                # Continuation of previous line
                joined_lines[-1] += " " + stripped

        for line in joined_lines:
            parts = self._parse_normalized_line(line)

            # Transport stream info: ts:id=901:...
            if line.startswith("ts:"):
                ts_id = self._parse_int(parts.get("id", "0"))
                total_bitrate = self._parse_int(parts.get("bitrate", "0"))
                country_code = parts.get("country", "")

            # Service entries: service:id=101:tsid=901:orignetwid=272:...
            elif line.startswith("service:"):
                sid = self._parse_int(parts.get("id", "0"))
                sname = parts.get("name", f"Service {sid}")
                provider = parts.get("provider", "")
                stype_val = self._parse_int(parts.get("servtype", parts.get("type", "0")))
                stype_desc = self._service_type_name(stype_val)
                bitrate = self._parse_int(parts.get("bitrate", "0"))
                pmt = self._parse_int(parts.get("pmtpid", parts.get("pmt_pid", "0")))
                pcr = self._parse_int(parts.get("pcrpid", parts.get("pcr_pid", "0")))
                srv_onid = self._parse_int(parts.get("orignetwid", "0"))

                if srv_onid and not onid:
                    onid = srv_onid

                services.append(ServiceInfo(
                    service_id=sid,
                    service_name=sname,
                    provider=provider,
                    service_type=hex(stype_val),
                    service_type_desc=stype_desc,
                    pmt_pid=pmt,
                    pcr_pid=pcr,
                    bitrate=bitrate,
                    pids=[],
                ))

            # PID lines — extract network name from NIT description
            elif line.startswith("pid:"):
                desc = parts.get("description", "")
                if "NIT" in desc or "Network" in desc:
                    match = re.search(
                        r'Network:\s*0x[0-9A-Fa-f]+\s*\(\d+\)\s*(.+?)(?:\)|$)',
                        desc,
                    )
                    if match:
                        network_name = match.group(1).strip().rstrip(")")

            # Country code: the ts line carries it, the TOT lines repeat it.
            elif line.startswith("time:"):
                if not country_code and parts.get("country"):
                    country_code = parts["country"]

        # If we didn't parse normalized output well, try the table format
        if not services:
            services, ts_id, onid, network_name, country_code, total_bitrate = (
                self._parse_table_analysis(output)
            )

        if not services:
            self._emit_log("No services found in analysis output", "warning")
            return None

        return MuxInfo(
            ts_id=ts_id,
            original_network_id=onid,
            network_name=network_name,
            services=services,
            total_bitrate=total_bitrate,
            country_code=country_code,
        )

    def _parse_normalized_line(self, line: str) -> dict[str, str]:
        """Parse a tsanalyze normalized line.

        Real format uses ':' as field separator:
            service:id=101:tsid=901:orignetwid=272:name=Canale5 HD FACTORY

        The first segment is the record type (ts, service, pid, etc.).
        Remaining segments are key=value pairs separated by ':'.

        A value may legitimately contain a colon, because the NIT description
        reads ``NIT (Network: 0x3001 (12289) Mediaset1)``. Only a colon that
        introduces a new ``key=`` field is a separator, so the split has to
        look ahead at what follows it instead of taking every colon.
        """
        parts: dict[str, str] = {}
        _, separator, rest = line.partition(":")
        if not separator:
            return parts

        # Records may end with an extra ':' (the ts line does, the pid lines do
        # not) and that trailing colon is punctuation, not part of the value.
        if rest.endswith(":"):
            rest = rest[:-1]

        for segment in _NORMALIZED_FIELD_SEPARATOR.split(rest):
            key, equals, value = segment.partition("=")
            if not equals:
                continue
            key = key.strip()
            value = value.strip()
            # If we already have this key, append (handles continuation)
            if key in parts:
                parts[key] = parts[key] + ":" + value
            else:
                parts[key] = value

        return parts

    @staticmethod
    def _parse_int(value: str) -> int:
        """Parse an integer that may be decimal or hex (0x...) format."""
        value = value.strip()
        if not value:
            return 0
        try:
            if value.startswith("0x") or value.startswith("0X"):
                return int(value, 16)
            return int(value)
        except ValueError:
            return 0

    def _parse_table_analysis(self, output: str) -> tuple:
        """Fallback: parse tsanalyze table-format output."""
        services = []
        ts_id = 0
        onid = 0
        network_name = ""
        country_code = ""
        total_bitrate = 0

        # Transport Stream Id
        match = re.search(r'Transport Stream Id:\s*\.+\s*0x([0-9A-Fa-f]+)\s*\((\d+)\)', output)
        if match:
            ts_id = int(match.group(1), 16)

        # Total bitrate
        match = re.search(r'Selected reference bitrate:\s*\.+\s*([\d,]+)\s*b/s', output)
        if match:
            total_bitrate = int(match.group(1).replace(",", ""))

        # Country code
        match = re.search(r'TOT country code:\s*\.+\s*([A-Z]{3})', output)
        if match:
            country_code = match.group(1)

        # Network info from NIT PID line
        match = re.search(
            r'NIT \(Network:\s*0x([0-9A-Fa-f]+)\s*\(\d+\)\s*(.+?)\)', output
        )
        if match:
            network_name = match.group(2).strip()

        # Service blocks
        service_blocks = re.finditer(
            r'Service:\s*0x([0-9A-Fa-f]+)\s*\((\d+)\),\s*TS:\s*0x[0-9A-Fa-f]+\s*\(\d+\),\s*'
            r'Original Netw:\s*0x([0-9A-Fa-f]+)\s*\((\d+)\)\s*\|'
            r'\s*\|\s*Service name:\s*(.+?),\s*provider:\s*(.+?)\s*\|'
            r'\s*\|\s*Service type:\s*0x([0-9A-Fa-f]+)\s*\((.+?)\)',
            output,
        )
        for m in service_blocks:
            sid = int(m.group(1), 16)
            sname = m.group(5).strip()
            provider = m.group(6).strip()
            stype_val = int(m.group(7), 16)
            stype_desc = m.group(8).strip()

            if not onid:
                onid = int(m.group(3), 16)

            # Find bitrate for this service in Srv table
            srv_match = re.search(
                rf'0x{m.group(1)}\s+{re.escape(sname)}.*?([\d,]+)\s*b/s',
                output,
            )
            bitrate = int(srv_match.group(1).replace(",", "")) if srv_match else 0

            # PMT and PCR PIDs
            pmt_match = re.search(
                rf'Service:\s*0x{m.group(1)}.*?PMT PID:\s*0x([0-9A-Fa-f]+).*?'
                rf'PCR PID:\s*0x([0-9A-Fa-f]+)',
                output,
                re.DOTALL,
            )
            pmt_pid = int(pmt_match.group(1), 16) if pmt_match else 0
            pcr_pid = int(pmt_match.group(2), 16) if pmt_match else 0

            services.append(ServiceInfo(
                service_id=sid,
                service_name=sname,
                provider=provider,
                service_type=hex(stype_val),
                service_type_desc=stype_desc,
                pmt_pid=pmt_pid,
                pcr_pid=pcr_pid,
                bitrate=bitrate,
                pids=[],
            ))

        return services, ts_id, onid, network_name, country_code, total_bitrate

    @staticmethod
    def _service_type_name(stype: int) -> str:
        """Map DVB service type codes to human-readable descriptions."""
        names = {
            0x01: "Digital TV",
            0x02: "Digital Radio",
            0x06: "Teletext",
            0x0C: "Data broadcast",
            0x11: "MPEG-2 HD TV",
            0x16: "H.264/AVC SD TV",
            0x19: "H.264/AVC HD TV",
            0x1F: "HEVC TV",
            0x20: "HEVC UHD TV",
        }
        return names.get(stype, f"Type 0x{stype:02X}")

    # ──────────────────────────────────────────────
    # Recording
    # ──────────────────────────────────────────────

    async def start_recording(
        self,
        frequency: int,
        bandwidth: int = 8,
        adapter: int = 1,
        delivery_system: str = "DVB-T",
        service_ids: list[int] | None = None,
        duration_seconds: int | None = None,
        filename: str | None = None,
    ) -> RecordingInfo:
        """Start recording a transport stream.

        ``service_ids`` distinguishes the two recording modes: ``None`` records
        the full mux, a non-empty list records only those services, and an
        empty list is rejected because "selected services" with nothing
        selected must never quietly turn into a full-mux capture.
        """
        if service_ids is not None and not service_ids:
            raise NoServicesSelectedError(
                "No services selected — tick at least one service, "
                "or switch the capture mode to full mux"
            )

        if self.state in (
            RecordingState.TUNING,
            RecordingState.RECORDING,
            RecordingState.STOPPING,
        ):
            raise RuntimeError("A recording is already in progress")

        freq_hz = frequency if frequency > 1_000_000 else frequency * 1_000_000

        if not filename:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            freq_mhz = freq_hz // 1_000_000
            filename = f"capture_{freq_mhz}MHz_{ts}.ts"

        filepath = self.recordings_dir / filename

        self._current_recording = RecordingInfo(
            filename=filename,
            filepath=str(filepath),
            frequency=freq_hz,
            bandwidth=bandwidth,
            delivery_system=delivery_system,
            started_at=datetime.now(),
            state=RecordingState.TUNING,
            services_filter=service_ids or [],
            duration_limit=duration_seconds,
        )

        self._stop_event.clear()
        self._emit_state(self._get_recording_status())
        self._emit_log(f"Starting recording to {filename}")

        # Build tsp command
        cmd = [
            "tsp",
            "-I", "dvb",
            "-a", str(adapter),
            "-f", str(freq_hz),
            "--bandwidth", str(bandwidth),
            "--delivery-system", delivery_system,
            "--signal-timeout", "10",
        ]

        # Service filtering with zap plugin
        if service_ids:
            # The 'zap' plugin supports multiple services natively and correctly
            # rewrites PAT/SDT to create a compliant output TS.
            cmd.extend(["-P", "zap"])
            for sid in service_ids:
                cmd.append(str(sid))
            cmd.extend(["--no-ecm"])

        # Duration limit
        if duration_seconds:
            cmd.extend(["-P", "until", "--seconds", str(duration_seconds)])

        cmd.extend(["-O", "file", str(filepath)])

        # Start in background thread
        self._recording_thread = threading.Thread(
            target=self._run_recording,
            args=(cmd, filepath, duration_seconds),
            daemon=True,
        )
        self._recording_thread.start()

        return self._current_recording

    def _run_recording(self, cmd: list[str], filepath: Path, duration_limit: int | None = None) -> None:
        """Run tsp recording in background thread.

        The tuner needs several seconds to lock, so the recording clock starts
        at the first byte written to the output file, not when tsp is launched.
        A process that dies before writing anything is reported as an error
        instead of a successful empty capture.
        """
        rec = self._current_recording
        if rec is None:
            return

        stderr_lines: list[str] = []
        process: subprocess.Popen | None = None
        first_data_at: datetime | None = None

        try:
            self._emit_log(f"Executing: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._tsp_process = process
            drain = threading.Thread(
                target=self._drain_stderr,
                args=(process, stderr_lines),
                daemon=True,
            )
            drain.start()

            while not self._stop_event.is_set():
                returncode = process.poll()

                if filepath.exists():
                    rec.file_size_bytes = filepath.stat().st_size
                    if rec.file_size_bytes > 0 and first_data_at is None:
                        first_data_at = datetime.now()
                        rec.state = RecordingState.RECORDING
                        self._emit_log("Signal locked — recording started", "success")
                        self._emit_state(self._get_recording_status())

                if first_data_at is not None:
                    rec.duration_seconds = (
                        datetime.now() - first_data_at
                    ).total_seconds()

                if (
                    duration_limit
                    and first_data_at is not None
                    and rec.duration_seconds >= duration_limit
                ):
                    self._emit_log(
                        f"Duration limit reached ({duration_limit}s), stopping recording...",
                        "info",
                    )
                    break

                if returncode is not None:
                    break

                self._emit_state(self._get_recording_status())
                time.sleep(self.poll_interval)

            stopped_by_user = self._stop_event.is_set()
            self._terminate_process(process)

            if filepath.exists():
                rec.file_size_bytes = filepath.stat().st_size

            tsp_error = self._tsp_error(stderr_lines)
            if tsp_error and rec.file_size_bytes < EMPTY_CAPTURE_BYTES:
                # ``tsp -P zap`` exits 0 after writing only the tables when the
                # requested services are missing from the PAT, so a non-empty
                # file does not prove a capture: 188 bytes of tables is not data.
                self._discard_artifact(filepath)
                rec.last_error = tsp_error
                rec.state = RecordingState.ERROR
                self._emit_state(self._get_recording_status())
                self._emit_log(f"Recording failed: {tsp_error}", "error")
                return

            if first_data_at is not None:
                rec.duration_seconds = (
                    datetime.now() - first_data_at
                ).total_seconds()
                rec.state = RecordingState.IDLE
                self._emit_state(self._get_recording_status())
                self._emit_log(
                    f"Recording stopped. File: {filepath.name}, "
                    f"Size: {self._format_size(rec.file_size_bytes)}, "
                    f"Duration: {rec.duration_seconds:.1f}s",
                    "success",
                )
                if tsp_error:
                    self._emit_log(
                        f"Capture ended with a tsp error (file kept): {tsp_error}",
                        "warning",
                    )
                self._analyze_finished_capture(filepath)
            elif stopped_by_user:
                self._discard_empty_artifact(filepath)
                rec.state = RecordingState.IDLE
                self._emit_state(self._get_recording_status())
                self._emit_log(
                    "Recording cancelled before signal lock — no data was written",
                    "warning",
                )
            else:
                # Give the drain thread a moment: tsp reports its failure on
                # stderr and exits, and that message is the useful diagnosis.
                drain.join(timeout=2)
                rec.last_error = self._failure_reason(stderr_lines, process, duration_limit)
                self._discard_empty_artifact(filepath)
                rec.state = RecordingState.ERROR
                self._emit_state(self._get_recording_status())
                self._emit_log(f"Recording failed: {rec.last_error}", "error")

        except Exception as e:
            self._terminate_process(process)
            rec.last_error = f"Recording error: {e}"
            rec.state = RecordingState.ERROR
            self._emit_log(rec.last_error, "error")
            self._emit_state(self._get_recording_status())

    def _drain_stderr(self, process: subprocess.Popen, sink: list[str]) -> None:
        """Forward tsp stderr to the log and keep it for failure reporting."""
        if not process.stderr:
            return
        for line in process.stderr:
            cleaned = line.strip()
            if cleaned:
                sink.append(cleaned)
                self._emit_log(cleaned, "tsduck")

    @staticmethod
    def _terminate_process(process: subprocess.Popen | None) -> None:
        """Stop a tsp process, escalating to kill if it ignores terminate."""
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(Exception):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    @staticmethod
    def _discard_empty_artifact(filepath: Path) -> None:
        """Remove the empty file tsp leaves behind when a capture fails."""
        with contextlib.suppress(OSError):
            if filepath.exists() and filepath.stat().st_size == 0:
                filepath.unlink()

    @staticmethod
    def _discard_artifact(filepath: Path) -> None:
        """Remove a capture artifact that carries no usable payload."""
        with contextlib.suppress(OSError):
            if filepath.exists() and filepath.stat().st_size < EMPTY_CAPTURE_BYTES:
                filepath.unlink()

    @staticmethod
    def _tsp_error(stderr_lines: list[str]) -> str:
        """Return tsp's own error line, if it reported one.

        ``tsp`` prefixes its diagnostics with ``* Error:`` and still exits 0 for
        recoverable failures such as a service filter that matched nothing, so
        the message is the only trustworthy signal.
        """
        for line in stderr_lines:
            if line.startswith("* Error:"):
                return line
        return ""

    @staticmethod
    def _failure_reason(
        stderr_lines: list[str],
        process: subprocess.Popen | None,
        duration_limit: int | None,
    ) -> str:
        """Build a human-readable reason for a capture that produced no data."""
        for line in reversed(stderr_lines):
            if "error" in line.lower():
                return line
        for line in reversed(stderr_lines):
            if line:
                return line
        returncode = process.returncode if process is not None else None
        if returncode:
            return f"tsp exited with code {returncode} before receiving any data"
        if duration_limit:
            return "No data was received before the duration limit expired"
        return "No data was received — check antenna, device and frequency"

    async def stop_recording(self) -> RecordingInfo | None:
        """Stop the current recording, including while it is still tuning."""
        rec = self._current_recording
        if rec is None or rec.state not in (
            RecordingState.TUNING,
            RecordingState.RECORDING,
            RecordingState.STOPPING,
        ):
            return None

        if rec.state == RecordingState.STOPPING:
            return rec

        self._emit_log("Stopping recording...")
        rec.state = RecordingState.STOPPING
        self._emit_state(self._get_recording_status())

        self._stop_event.set()

        thread = self._recording_thread
        if thread:
            await asyncio.to_thread(thread.join, timeout=10)
            if thread.is_alive():
                # The monitor thread is stuck; make sure tsp is not orphaned.
                self._terminate_process(self._tsp_process)
                await asyncio.to_thread(thread.join, timeout=5)

        return rec

    # ──────────────────────────────────────────────
    # Recordings library
    # ──────────────────────────────────────────────

    async def list_recordings(self) -> list[dict]:
        """List all recorded .ts files with metadata."""
        recordings = []
        for f in sorted(self.recordings_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            recordings.append({
                "filename": f.name,
                "filepath": str(f),
                "size_bytes": stat.st_size,
                "size_display": self._format_size(stat.st_size),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return recordings

    async def reveal_recording(self, filename: str) -> RevealOutcome:
        """Show one recording in the platform file manager (S9).

        The name is resolved inside ``recordings_dir`` first: the reveal launches
        a process, and a name that escapes the directory has to be refused before
        anything runs. The same rule already guards delete and analyze.
        """
        candidate = self._resolve_recording(filename)
        if candidate is None:
            self._emit_log(f"Refused to reveal {filename!r}: not a recording", "warning")
            return RevealOutcome.UNKNOWN

        command = reveal_command(candidate)
        if command is None:
            self._emit_log(
                "Cannot reveal the recording: no supported file manager here",
                "warning",
            )
            return RevealOutcome.UNSUPPORTED

        try:
            self._launch_reveal(command)
        except OSError as error:
            # The command exists for this platform but is not installed (xdg-open
            # is the usual absence). That is unavailability, not a crash.
            self._emit_log(f"Cannot reveal the recording: {error}", "warning")
            return RevealOutcome.UNSUPPORTED

        self._emit_log(f"Revealed in the file manager: {candidate.name}")
        return RevealOutcome.REVEALED

    def _launch_reveal(self, command: list[str]) -> None:
        """Hand the file to the platform's file manager and stop caring (S9).

        Detached on purpose: the file manager is a GUI program the operator drives
        and the app must not wait for it, nor inherit its output.
        """
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def delete_recording(self, filename: str) -> bool:
        """Delete a recording file, refusing any path outside the recordings dir."""
        candidate = self._resolve_recording(filename)
        if candidate is None:
            return False

        candidate.unlink()
        self._emit_log(f"Deleted recording: {candidate.name}")
        current = self._current_recording
        if current is not None and Path(current.filepath) == candidate:
            # The counters describe this file: they go with it, and the browser is
            # told so the card does not keep printing a deleted capture (L8a).
            self._current_recording = None
            self._emit_state(self._get_recording_status())
        if self._report_target == candidate:
            # The card is describing this file: it goes with it, and the browser is
            # told so the operator does not keep reading a report of nothing (L6).
            self._forget_report()
            self._emit_report()
        return True

    def _resolve_recording(self, filename: str) -> Path | None:
        """Resolve a library name inside ``recordings_dir``, or ``None``.

        One rule for every action that takes a name from the browser (delete,
        reveal, analyze): a name that escapes the recordings directory must be
        refused before a file is touched or a process is launched. Backslashes are
        path separators on Windows, so a "filename" such as ``..\\victim.ts``
        would otherwise walk out of the directory.
        """
        if not filename or Path(filename).suffix != ".ts":
            return None

        root = self.recordings_dir.resolve()
        candidate = (root / filename).resolve()
        if candidate.parent != root or not candidate.is_file():
            return None
        return candidate

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _get_report_status(self) -> dict:
        """What changed about the report, for the announcement (S4, L3a)."""
        # The guarded properties, not the raw fields: an announcement must never
        # advertise a report whose file is already gone (L6).
        current = self.current_report
        return {
            "source": current.source if current else None,
            "pending_bytes": self.report_pending_bytes,
        }

    def _get_recording_status(self) -> dict:
        """Get current recording status as a dict for WebSocket broadcast."""
        # The guarded property, not the raw field: the browser must never be told
        # about counters whose file is gone (L8a).
        rec = self.current_recording
        if not rec:
            # The display fields are part of the empty shape on purpose: the page
            # only overwrites a counter when it is given the text to print, so
            # without them the last capture's numbers would stay on screen.
            return {
                "state": RecordingState.IDLE,
                "file_size": 0,
                "file_size_display": self._format_size(0),
                "duration": 0,
                "duration_display": self._format_duration(0),
            }

        return {
            "state": rec.state.value,
            "last_error": rec.last_error,
            "filename": rec.filename,
            "frequency": rec.frequency,
            "file_size": rec.file_size_bytes,
            "file_size_display": self._format_size(rec.file_size_bytes),
            "duration": rec.duration_seconds,
            "duration_display": self._format_duration(rec.duration_seconds),
            "services_filter": rec.services_filter,
        }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format bytes into human-readable size."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 ** 2:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 ** 3:
            return f"{size_bytes / (1024 ** 2):.1f} MB"
        else:
            return f"{size_bytes / (1024 ** 3):.2f} GB"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds into HH:MM:SS."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
