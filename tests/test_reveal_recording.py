"""Reveal a recording in the platform file manager — S9.

``openspec/changes/stream-report-ui`` S9. The request, verbatim: "en la ultima
seccion donde se ven los files registrados ts y se ven el boton de borrar, me
gustaria que hubiera tambien boton ir a carpeta (es mas facil para ir a
buscarlo)".

Two rules carry the risk here:

1. A name typed by the browser never leaves ``recordings_dir``. The reveal
   launches a process, so a traversal name (``..\\victim.ts``, ``../victim.ts``,
   an absolute path) has to be refused *before* anything is launched — the same
   defect that was fixed for delete.
2. The platform is decided by a pure function, tested without launching
   anything: Windows selects the file in Explorer, macOS reveals it in Finder,
   Linux opens the containing folder (``xdg-open`` has no "select this file"
   verb, and pretending otherwise would be a lie in the UI), and any other
   platform reports unavailability instead of failing silently.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import tsduck_service as tsduck

# Same directory for every comparison: whether a path is "the parent folder" is
# what the command decides, and the tests must hold on POSIX and Windows alike.
SAMPLE = Path("recordings") / "capture_a.ts"


class RevealCommandTest(unittest.TestCase):
    """One pure function decides what to launch, per platform."""

    def test_windows_selects_the_file_in_explorer(self):
        command = tsduck.reveal_command(SAMPLE, platform="win32")

        self.assertEqual(["explorer", f"/select,{SAMPLE}"], command)

    def test_macos_reveals_the_file_in_finder(self):
        command = tsduck.reveal_command(SAMPLE, platform="darwin")

        self.assertEqual(["open", "-R", str(SAMPLE)], command)

    def test_linux_opens_the_containing_folder(self):
        command = tsduck.reveal_command(SAMPLE, platform="linux")

        self.assertEqual(["xdg-open", str(SAMPLE.parent)], command)

    def test_an_unsupported_platform_has_no_command(self):
        self.assertIsNone(tsduck.reveal_command(SAMPLE, platform="aix"))

    def test_the_command_follows_the_running_platform_by_default(self):
        self.assertEqual(
            tsduck.reveal_command(SAMPLE, platform=sys.platform),
            tsduck.reveal_command(SAMPLE),
        )


class RevealRecordingTest(unittest.TestCase):
    """The service refuses bad names before the launcher, and reports outcomes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        self.capture = Path(self.directory.name) / "capture_a.ts"
        self.capture.write_bytes(b"\x47" * 188)
        launcher = mock.patch.object(self.service, "_launch_reveal")
        self.launch = launcher.start()
        self.addCleanup(launcher.stop)

    def reveal(self, filename):
        return asyncio.run(self.service.reveal_recording(filename))

    def test_a_known_recording_is_revealed_by_its_absolute_path(self):
        outcome = self.reveal("capture_a.ts")

        self.assertIs(tsduck.RevealOutcome.REVEALED, outcome)
        self.launch.assert_called_once()
        command = self.launch.call_args.args[0]
        self.assertIn(str(self.capture.resolve()), " ".join(str(part) for part in command))

    def test_a_name_outside_the_recordings_directory_never_launches_anything(self):
        victim = Path(self.directory.name).parent / "victim.ts"
        victim.write_bytes(b"\x47")
        self.addCleanup(lambda: victim.unlink(missing_ok=True))

        for name in ("..\\victim.ts", "../victim.ts", str(victim)):
            with self.subTest(name=name):
                self.assertIs(tsduck.RevealOutcome.UNKNOWN, self.reveal(name))
                self.launch.assert_not_called()
                self.launch.reset_mock()

    def test_an_unknown_name_is_refused(self):
        self.assertIs(tsduck.RevealOutcome.UNKNOWN, self.reveal("nope.ts"))
        self.launch.assert_not_called()

    def test_a_non_ts_name_is_refused(self):
        (Path(self.directory.name) / "notes.txt").write_text("x")

        self.assertIs(tsduck.RevealOutcome.UNKNOWN, self.reveal("notes.txt"))
        self.launch.assert_not_called()

    def test_a_platform_without_a_file_manager_reports_unavailability(self):
        with mock.patch.object(tsduck, "reveal_command", return_value=None):
            outcome = self.reveal("capture_a.ts")

        self.assertIs(tsduck.RevealOutcome.UNSUPPORTED, outcome)
        self.launch.assert_not_called()

    def test_a_missing_file_manager_binary_reports_unavailability(self):
        """``xdg-open`` is not always installed; that is unavailability, not a crash."""
        service = tsduck.TSDuckService(recordings_dir=self.directory.name)

        with mock.patch.object(
            tsduck.subprocess, "Popen", side_effect=FileNotFoundError("xdg-open")
        ) as popen:
            outcome = asyncio.run(service.reveal_recording("capture_a.ts"))

        self.assertIs(tsduck.RevealOutcome.UNSUPPORTED, outcome)
        popen.assert_called_once()


class RevealEndpointTest(unittest.TestCase):
    """The Library action: 404 for a refused name, 501 for a platform that cannot."""

    def setUp(self):
        from app import main

        self.main = main
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.capture = Path(self.directory.name) / "capture_a.ts"
        self.capture.write_bytes(b"\x47" * 188)
        self.service = tsduck.TSDuckService(recordings_dir=self.directory.name)
        patcher = mock.patch.object(main, "tsduck_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        launcher = mock.patch.object(self.service, "_launch_reveal")
        self.launch = launcher.start()
        self.addCleanup(launcher.stop)

    def test_a_known_recording_answers_no_content_and_launches_once(self):
        response = asyncio.run(self.main.reveal_recording("capture_a.ts"))

        self.assertEqual(204, response.status_code)
        self.launch.assert_called_once()

    def test_an_unknown_name_is_a_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(self.main.reveal_recording("nope.ts"))

        self.assertEqual(404, raised.exception.status_code)
        self.launch.assert_not_called()

    def test_a_traversal_name_is_a_404_and_launches_nothing(self):
        from fastapi import HTTPException

        victim = Path(self.directory.name).parent / "victim.ts"
        victim.write_bytes(b"\x47")
        self.addCleanup(lambda: victim.unlink(missing_ok=True))

        for name in ("..\\victim.ts", "../victim.ts", str(victim)):
            with self.subTest(name=name), self.assertRaises(HTTPException) as raised:
                asyncio.run(self.main.reveal_recording(name))

            self.assertEqual(404, raised.exception.status_code)
            self.launch.assert_not_called()

    def test_an_unsupported_platform_is_a_501(self):
        from fastapi import HTTPException

        with mock.patch.object(tsduck, "reveal_command", return_value=None), self.assertRaises(
            HTTPException
        ) as raised:
            asyncio.run(self.main.reveal_recording("capture_a.ts"))

        self.assertEqual(501, raised.exception.status_code)
        self.launch.assert_not_called()


class LibraryRevealButtonTest(unittest.TestCase):
    """Every row offers the action next to the ones it already had."""

    def render(self):
        from app.main import templates

        return templates.env.get_template("partials/recordings_table.html").render(
            recordings=[
                {"filename": "capture_a.ts", "size_display": "10 MB",
                 "created": "2026-11-06T10:00:00"},
            ]
        )

    def test_a_row_offers_the_reveal_action(self):
        self.assertIn('hx-post="/api/recordings/capture_a.ts/reveal"', self.render())

    def test_the_reveal_action_announces_what_it_does(self):
        rendered = self.render()

        self.assertIn('title="Show capture_a.ts in the file manager"', rendered)

    def test_the_reveal_action_swaps_nothing(self):
        rendered = self.render()

        start = rendered.index('hx-post="/api/recordings/capture_a.ts/reveal"')
        button = rendered[start:rendered.index("</button>", start)]
        self.assertIn('hx-swap="none"', button)


if __name__ == "__main__":
    unittest.main()
