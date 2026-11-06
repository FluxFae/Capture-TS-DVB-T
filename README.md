# Capture-TS

![Capture-TS Demo](.github/assets/Capture-TS.gif)

Capture-TS is a lightweight web-based DVB-T transport stream capture and recording tool for Windows. It provides a browser dashboard to discover DVB tuner devices, analyze multiplexes using TSDuck, start and stop recordings, and manage your captured `.ts` files — all from a clean, dark-themed UI.

<p align="center">
  <img src=".github/assets/Capture-TS-dark.png" width="48%" alt="Dark Theme Dashboard" />
  <img src=".github/assets/Capture-TS-MuxAnalyzer.png" width="48%" alt="Mux Analyzer" />
</p>
<p align="center">
  <img src=".github/assets/Capture-TS-Library.png" width="48%" alt="Recordings Library" />
  <img src=".github/assets/Capture-TS-record.png" width="48%" alt="Recording Panel" />
</p>

## ✨ Key Features

- **DVB Tuner Discovery** — Automatic detection of connected DVB-T receivers via `tslsdvb`
- **Mux Analysis** — MPEG-2 transport stream multiplex analysis using TSDuck
- **Flexible Recording** — Record a full DVB-T multiplex or only selected services
- **Live Updates** — Real-time recording state and log updates through WebSockets
- **Stream Report** — Per-capture integrity and services analysis: transport errors, PIDs, codecs and the `TSID`/`ONID`/`SID` identity triplet
- **Recording Library** — Analyze, reveal and delete captured `.ts` files
- **Dark-Themed UI** — Clean, responsive interface built for quick operation

## 🚀 Getting Started

### Prerequisites

- Windows 10 / 11
- Python 3.10+
- [TSDuck](https://tsduck.io/) — with `tsp`, `tsanalyze`, and `tslsdvb` available in `PATH`
- [Visual Studio Community 2022](https://visualstudio.microsoft.com/) — C++20 support (required by TSDuck)
- USB DVB-T receiver with installed drivers (e.g. Hauppauge WinTV-soloHD)

### Local Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/FluxFae/Capture-TS-DVB-T.git
   cd Capture-TS-DVB-T
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv venv
   venv\Scripts\Activate.ps1   # PowerShell
   # or
   venv\Scripts\activate.bat   # cmd.exe
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
   The pins use `~=` (compatible release) at the versions this release was verified
   with, so patch and minor fixes are accepted automatically but a new minor is not.

4. **Run the application**:
   ```bash
   python run.py
   ```

5. Open your browser at `http://127.0.0.1:8000`

## ✅ Verified Configuration

Capture-TS is developed and verified against one concrete setup:

- Hauppauge WinTV-soloHD, **DVB-T** and **DVB-T2** tuner, 8 MHz muxes
- Windows 11, Python 3.12, TSDuck `3.43-4549`

Both terrestrial paths are verified on air: DVB-T on the Mediaset multiplex at 674 MHz,
and DVB-T2 on the Rai multiplex at 626 MHz, where the report decodes HEVC video plus
MPEG-1 and MPEG-2 AAC audio.

The DVB-C tuner that this hybrid receiver also exposes is **not** verified, and cannot be
here: DVB-C is not in use in Italy, so there is no cable signal to receive. It is
confirmed to reach `tsp` and tune, nothing more.

The stick carries two tuners, one DVB-T/DVB-T2 and one DVB-C, and `tslsdvb` numbers them
in whatever order Windows enumerates them: the same stick has been seen with the DVB-T
tuner first and, after a reconnect, second. **Do not rely on an index.** The form
preselects the tuner that reports DVB-T and only offers the delivery systems the
selected device reports, so picking the DVB-C tuner exposes `DVB-C` / `DVB-C/C` instead
of sending an incompatible system to `tsp`; an incompatible pair is refused with a
message naming the device and the systems it supports.

Signal lock takes several seconds on this hardware. Recording duration is therefore
counted from the first byte written to the file, not from the moment the recorder
starts, and a capture that never receives data ends in an error instead of an empty
file reported as a success.

The capture mode decides what lands in the file: *full mux* records the whole
multiplex and ignores the service checkboxes, while *selected services* records only
the ticked ones. Selecting that mode with nothing ticked is refused rather than
silently falling back to the full mux.

## 📊 Stream Report

Every capture can be read back as a transport stream analysis report. The question it
answers is *is this recording intact?*, not *what is on air right now*, so it is built
with `tsanalyze --json` from a recorded `.ts` file and never from a live tuner snapshot.

The header identifies what was captured:

| Field | Meaning |
| --- | --- |
| `SOURCE` | The file the report describes. A report is never rendered without it. |
| `TSID` | Transport stream id of the multiplex. |
| `ONID` | Original network id — the distinct values in service order, `—` when the capture carries no tables. |
| `SERVICES` | Number of services in the capture. |
| `COUNTRY` | Country code taken from the time tables. |

`TSID` + `ONID` + `SID` is the triplet broadcast uses to identify a service on air, so
the services table starts with the `SID` in decimal.

Below the header come two layers:

- **Integrity** — packets, transport errors, invalid syncs, unique PIDs, bitrate at
  188 bytes/packet and duration. Transport errors are the headline number.
- **Services** — per service: SID, name, video codec with resolution, audio codecs with
  their language, PIDs used and bitrate. There is no provider column: the frequency scan
  already names the network for the whole multiplex, while the codecs are what no other
  panel shows.

Two arithmetic facts that look like defects and are not:

- `PIDs (unique)` counts every PID once, while the per-service column counts the PIDs
  assigned to that service. A PID shared by three services is counted once above and
  three times in the table, so the column adds up to more than the total. PID 0 is not
  counted in the total by TSDuck's own accounting.
- `Transport errors` counts damaged sync bytes, which is what a dropped signal, a full
  disk or a lost lock produces. Zero on a short capture does not prove a long one was
  clean, which is why the scan always covers the whole file.

### When the report runs by itself

A capture up to `AUTO_ANALYZE_MAX_BYTES` (512 MiB) is analysed the moment it stops, and
the card updates on its own — no reload and no button. Above that size the card shows the
file size and an **Analyze capture** button, because the scan is not free:
`tsanalyze --json` costs roughly 7 ms per MB, so 235 MB takes about 1.7 s and 512 MiB
about 4 s. The threshold is a pure function of the file size.

The scan **always covers the whole file**, never a sample of the first minutes. A sample
would let a report claim "0 transport errors" about 3 % of a 30 minute capture, while the
corruption that matters happens at the end.

The report is process state: a page reload keeps it, because a refresh must not throw
away an analysis that cost seconds to produce. What it cannot do is outlive its file —
deleting the analysed recording empties the card, and a report whose file disappeared is
never rendered.

## 📚 Recording Library

Every recorded `.ts` file gets three icon buttons in the library table:

| Button | What it does |
| --- | --- |
| Bar chart | Analyses that file and shows its report in the report card. This is how a capture recorded before this feature existed, or one recorded before a restart, is read back on demand. |
| Folder | Reveals the file in the platform file manager. |
| Trash | Removes the file, and with it any report or telemetry that described it. |

**The folder button** is per platform: Windows selects the file in Explorer, macOS
reveals it in Finder, and Linux opens the containing folder with `xdg-open` — there is no
"select this file" verb there. On any other platform, or when the file manager binary is
missing, the action reports that it is unavailable instead of failing silently.

Every action resolves the submitted name inside the `recordings/` directory before doing
anything: a name that escapes it (including a backslash separator) or does not exist is
refused before any process is launched.

## 🧪 Tests

The suite uses the Python standard library only (nothing extra to install) and does
not need a tuner: `subprocess.Popen` is replaced by a fake `tsp` process, and the
WebSocket tests run against a real in-process server.

```bash
venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

## ⚙️ Tech Stack

- **Backend**: Python 3.12, FastAPI, Jinja2
- **Frontend**: HTMX, Lit Web Components, CSS custom properties (`flux-tokens.css` and
  `flux.css`)
- **DVB Tooling**: TSDuck (`tsp`, `tsanalyze`, `tslsdvb`)

## 🛠️ Built with Gentle Shell

This project was written by [Gentle Shell](https://github.com/Gentleman-Programming/gentle-shell),
a coding agent for controlled development built for Pi — not by free-form prompting. The
workflow it enforces left its evidence in the repository:

- **Requirements before code.** A change starts as numbered requirements that quote the
  operator's own words, plus a task list and a log of every correction. They are kept as
  local change documents while the work is in flight, so a decision can be traced back to
  the request that produced it.
- **One task, one reviewable commit.** Each task closed with its code, its tests and its
  documentation in a single Conventional Commit.
- **Test-first with observed evidence.** The suite grew from zero to 202 tests as the
  features landed, with the failing test written before the fix.
- **Verification against the real device.** Fixes were confirmed on air with a Hauppauge
  WinTV-soloHD — 674 MHz (DVB-T) and 626 MHz (DVB-T2) — not only against mocks.
- **Context that survives a session.** Decisions, dead ends and corrections were
  recorded, so a later session resumed the work instead of re-litigating it.

The operator stays in charge: what to build, when to commit, when to release and the
final call on every change are human decisions, and the agent is the tool that carries
them out.

## 🚧 Project Status (As-Is) & Contributions

This is a personal tool open-sourced for the community. It currently does not have active maintenance or a planned development roadmap.

If you find this tool useful, discover a bug, or want to add an improvement, Pull Requests are welcome. I will review and integrate community contributions based on availability.

## 📜 License

Distributed under the MIT License.
