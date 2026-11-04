"""Recordings directory anchoring — M5.

``TSDuckService(recordings_dir="recordings")`` resolved the directory against
the process working directory, so launching the app from anywhere but the
project root created a second ``recordings/`` folder there: captures landed in
one place and the library listed another, depending on how the app was started.

The rule: the default directory belongs to the project, not to whoever pressed
enter, while an explicit directory still decides for itself (the tests and any
future caller rely on that).
"""

import os
import tempfile
import unittest
from pathlib import Path

from app.services.tsduck_service import TSDuckService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DefaultRecordingsDirTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmpdir, True)
        self.addCleanup(os.chdir, os.getcwd())

    def test_default_directory_ignores_the_working_directory(self):
        os.chdir(self.tmpdir)

        service = TSDuckService()

        self.assertEqual(service.recordings_dir.resolve(), (PROJECT_ROOT / "recordings").resolve())
        self.assertFalse(
            (Path(self.tmpdir) / "recordings").exists(),
            "a recordings folder was created in the process working directory",
        )

    def test_default_directory_is_absolute(self):
        service = TSDuckService()

        self.assertTrue(service.recordings_dir.is_absolute())

    def test_default_directory_exists(self):
        service = TSDuckService()

        self.assertTrue(service.recordings_dir.is_dir())

    def test_explicit_directory_still_wins(self):
        service = TSDuckService(recordings_dir=self.tmpdir)

        self.assertEqual(service.recordings_dir.resolve(), Path(self.tmpdir).resolve())
        self.assertTrue(Path(self.tmpdir).is_dir())


if __name__ == "__main__":
    unittest.main()
