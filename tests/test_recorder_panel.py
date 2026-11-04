"""Recorder panel state tests.

The panel is re-rendered from the template every time a recording starts or
stops, so anything the template hardcodes is silently thrown away: choosing
"Selected Services" and 120 seconds and pressing record used to redraw the form
with "Full Mux" and 0, as if the user had never touched it.

The rules pinned here: the panel redraws with the mode and duration that are
actually running (or last ran), and it says out loud that a capture is in
progress instead of only reporting the outcome at the end.
"""

import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.main import templates
from app.services.tsduck_service import RecordingInfo, RecordingState

PANEL = "partials/recorder_panel.html"


class FormReader(HTMLParser):
    """Read what the browser would read: inputs, their values and checked state."""

    def __init__(self):
        super().__init__()
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))

    def radio_state(self):
        return {
            item["value"]: "checked" in item
            for item in self.inputs
            if item.get("name") == "record_mode"
        }

    def field_value(self, name):
        for item in self.inputs:
            if item.get("name") == name:
                return item.get("value")
        return None


def render_panel(state, recording=None):
    html = templates.env.get_template(PANEL).render(
        recording_state=state.value if isinstance(state, RecordingState) else state,
        current_recording=recording,
    )
    reader = FormReader()
    reader.feed(html)
    return html, reader


def make_recording(services_filter=(), duration_limit=None, **overrides):
    """Build the state the panel renders. The template reads attributes only."""
    recording = RecordingInfo(
        filename="capture_610MHz.ts",
        filepath=str(Path("recordings") / "capture_610MHz.ts"),
        frequency=610_000_000,
        bandwidth=8,
        delivery_system="DVB-T",
        started_at=__import__("datetime").datetime.now(),
        **overrides,
    )
    recording.services_filter = list(services_filter)
    recording.duration_limit = duration_limit
    return recording


class ModeAndDurationSurviveTheRedrawTest(unittest.TestCase):
    def test_selected_mode_stays_selected(self):
        recording = make_recording(services_filter=[101, 102, 103], duration_limit=120)

        _, reader = render_panel(RecordingState.RECORDING, recording)

        self.assertEqual(
            reader.radio_state(),
            {"full_mux": False, "selected": True},
            "the panel redrew with Full Mux after a selected-services capture",
        )

    def test_full_mux_stays_full_mux(self):
        recording = make_recording(duration_limit=60)

        _, reader = render_panel(RecordingState.RECORDING, recording)

        self.assertEqual(reader.radio_state(), {"full_mux": True, "selected": False})

    def test_written_duration_is_not_reset_to_zero(self):
        recording = make_recording(services_filter=[101], duration_limit=120)

        _, reader = render_panel(RecordingState.RECORDING, recording)

        self.assertEqual(reader.field_value("duration"), "120")

    def test_unlimited_recording_keeps_zero(self):
        recording = make_recording()

        _, reader = render_panel(RecordingState.RECORDING, recording)

        self.assertEqual(reader.field_value("duration"), "0")

    def test_panel_without_any_recording_uses_the_neutral_defaults(self):
        _, reader = render_panel(RecordingState.IDLE, None)

        self.assertEqual(reader.radio_state(), {"full_mux": True, "selected": False})
        self.assertEqual(reader.field_value("duration"), "0")


class ProgressMessageTest(unittest.TestCase):
    def test_an_active_capture_is_announced_below_the_button(self):
        recording = make_recording(services_filter=[101, 102, 103], duration_limit=120)

        html, _ = render_panel(RecordingState.RECORDING, recording)

        self.assertIn("Recording", html)
        self.assertIn("120", html)

    def test_tuning_is_announced_too(self):
        recording = make_recording(duration_limit=30)

        html, _ = render_panel(RecordingState.TUNING, recording)

        self.assertIn("Recording", html)

    def test_an_idle_panel_does_not_claim_to_be_recording(self):
        recording = make_recording(services_filter=[101], duration_limit=30, file_size_bytes=1024, duration_seconds=30)

        html, _ = render_panel(RecordingState.IDLE, recording)

        self.assertNotIn("flux-record-summary--live", html)
        self.assertIn("capture_610MHz.ts", html, "the finished summary disappeared")


if __name__ == "__main__":
    unittest.main()
