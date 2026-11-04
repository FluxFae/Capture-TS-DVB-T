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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import tsduck

# A normalized record is a list of ``key=value`` fields joined by ':'. Only a
# colon that is immediately followed by a lowercase key and '=' separates
# fields; any other colon belongs to the value (e.g. a NIT description).
_NORMALIZED_FIELD_SEPARATOR = re.compile(r":(?=[a-z][a-z0-9_]*=)")


class RecordingState(str, Enum):
    IDLE = "idle"
    TUNING = "tuning"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class DVBDevice:
    index: int
    name: str
    delivery_systems: list[str]


# DVB-T is the only delivery system verified end to end on real hardware, so it
# is the one the UI preselects whenever a receiver reports it.
VERIFIED_DELIVERY_SYSTEM = "DVB-T"


class DeviceSelectionError(ValueError):
    """Raised when the submitted receiver device or delivery system is unusable."""


class NoServicesSelectedError(ValueError):
    """Raised when a service filter is requested but names no service."""


def default_device(devices: list[DVBDevice]) -> DVBDevice | None:
    """Choose the device to preselect in the form.

    Exactly one device is chosen, so the rendered ``<option>`` list can never
    carry two ``selected`` attributes and leave the browser to pick the last.
    """
    for device in devices:
        if VERIFIED_DELIVERY_SYSTEM in device.delivery_systems:
            return device
    return devices[0] if devices else None


def default_delivery_system(device: DVBDevice | None) -> str:
    """Delivery system to preselect for a device."""
    if device is None or not device.delivery_systems:
        return ""
    if VERIFIED_DELIVERY_SYSTEM in device.delivery_systems:
        return VERIFIED_DELIVERY_SYSTEM
    return device.delivery_systems[0]


def available_delivery_systems(devices: list[DVBDevice]) -> list[str]:
    """Every delivery system the connected devices report, in device order.

    The form renders these once and the client filters them against the chosen
    device, so switching from the DVB-T to the DVB-C tuner exposes DVB-C
    without another round trip.
    """
    systems: list[str] = []
    for device in devices:
        for system in device.delivery_systems:
            if system not in systems:
                systems.append(system)
    return systems


def resolve_device(
    devices: list[DVBDevice],
    adapter: int,
    delivery_system: str,
) -> DVBDevice:
    """Validate the submitted device/system pair before any tuner is opened.

    ``tsp -a 1 --delivery-system DVB-T`` fails hard on the DVB-C tuner of a
    hybrid receiver, and ``tslsdvb`` indices shift when devices are plugged or
    unplugged. Both cases are rejected here with a readable message instead of
    a raw TSDuck error.
    """
    if not devices:
        raise DeviceSelectionError(
            "No receiver device found — connect a DVB receiver and reload the page"
        )

    device = next((d for d in devices if d.index == adapter), None)
    if device is None:
        available = ", ".join(str(d.index) for d in devices)
        raise DeviceSelectionError(
            f"Receiver device {adapter} not found (available: {available})"
        )

    if delivery_system not in device.delivery_systems:
        supported = ", ".join(device.delivery_systems)
        raise DeviceSelectionError(
            f"Device {device.index} ({device.name}) does not support "
            f"{delivery_system}; it reports {supported}"
        )

    return device


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


class TSDuckService:
    """Orchestrates all TSDuck operations for the application."""

    def __init__(
        self,
        recordings_dir: str = "recordings",
        poll_interval: float = 0.5,
    ):
        """Create the service.

        ``poll_interval`` is how often the recording monitor polls the tsp
        process and the output file. The soloHD class tuners need several
        seconds to lock, so this interval affects responsiveness only.
        """
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(exist_ok=True)
        self.poll_interval = poll_interval

        self._current_recording: RecordingInfo | None = None
        self._tsp_process: subprocess.Popen | None = None
        self._recording_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_callback: Callable[[str], None] | None = None
        self._state_callback: Callable[[dict], None] | None = None

    @property
    def state(self) -> RecordingState:
        if self._current_recording:
            return self._current_recording.state
        return RecordingState.IDLE

    @property
    def current_recording(self) -> RecordingInfo | None:
        return self._current_recording

    def set_log_callback(self, callback: Callable[[str], None]) -> None:
        self._log_callback = callback

    def set_state_callback(self, callback: Callable[[dict], None]) -> None:
        self._state_callback = callback

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

    async def delete_recording(self, filename: str) -> bool:
        """Delete a recording file, refusing any path outside the recordings dir."""
        if not filename or Path(filename).suffix != ".ts":
            return False

        root = self.recordings_dir.resolve()
        candidate = (root / filename).resolve()
        # Backslashes are path separators on Windows, so a "filename" such as
        # "..\\victim.ts" would otherwise escape the recordings directory.
        if candidate.parent != root or not candidate.is_file():
            return False

        candidate.unlink()
        self._emit_log(f"Deleted recording: {candidate.name}")
        return True

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _get_recording_status(self) -> dict:
        """Get current recording status as a dict for WebSocket broadcast."""
        if not self._current_recording:
            return {"state": RecordingState.IDLE, "file_size": 0, "duration": 0}

        rec = self._current_recording
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
