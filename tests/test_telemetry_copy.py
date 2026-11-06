"""Each fact is printed once — L8b.

``openspec/changes/stream-report-ui`` L8, verbatim: "Tambien tenemos mucho texto
redundante, cuando comienza a registrar en la parte inferior a los recuadros de
telemetria se ve un texto pequeno que nuestra recording escrito dos veces y muestra
los Mb y sec que ya se muestran arriba, es informacion duplicada."

FILE SIZE and DURATION sit right above the recorder panel and already carry the
megabytes and the seconds, so no line under them may repeat those numbers, and the
word "Recording" appears once, in the card's own label. What survives is only what
the counters cannot say: that a capture is still waiting for its first byte, and the
name of the file a finished capture produced.
"""

import unittest
from datetime import datetime
from pathlib import Path

from app.services.tsduck_service import RecordingInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def finished_capture(filename="recordings/capture_a.ts", size_bytes=120 * 1024 * 1024):
    """Only the attributes the dashboard reads; the page never talks to the service."""
    return RecordingInfo(
        filename=Path(filename).name,
        filepath=filename,
        frequency=674_000_000,
        bandwidth=8,
        delivery_system="DVB-T",
        started_at=datetime(2026, 11, 6, 13, 0, 0),
        duration_seconds=12.0,
        file_size_bytes=size_bytes,
        duration_limit=None,
    )


class NoRepeatedCountersTest(unittest.TestCase):
    """The small live line repeated the counters that sit right above it."""

    def render(self, state, capture=None):
        from app.main import templates

        return templates.env.get_template("dashboard.html").render(
            devices=[], recordings=[], report=None, report_pending_bytes=None,
            recording_state=state, current_recording=capture,
        )

    def test_a_running_capture_does_not_repeat_the_counters(self):
        rendered = self.render("recording", finished_capture())

        # The exact old phrases, not the bare words: "target" also lives in every
        # hx-target attribute, and a test matching those would pin nothing.
        self.assertNotIn("s captured", rendered, "the live line repeated the seconds")
        self.assertNotIn("s target,", rendered, "the live line repeated the target")
        self.assertNotIn("no limit", rendered, "the live line repeated the duration limit")

    def test_the_word_recording_appears_once(self):
        rendered = self.render("recording", finished_capture())

        self.assertEqual(1, rendered.count(">Recording<"))

    def test_tuning_still_explains_the_wait(self):
        rendered = self.render("tuning", finished_capture(size_bytes=0))

        self.assertIn("Waiting for the first data", rendered)

    def test_the_progress_label_is_gone(self):
        rendered = self.render("recording", finished_capture())

        self.assertNotIn("progress-label", rendered)

    def test_the_script_does_not_invent_a_progress_label(self):
        script = (PROJECT_ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("progress-label", script)


if __name__ == "__main__":
    unittest.main()
