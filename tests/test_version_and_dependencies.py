"""Single-source guards for version and declared dependencies — debt items 1-3.

``openspec/changes/public-release-prep`` asks for the version to live in the
code and for the README to stay evergreen. It did not happen: the OpenAPI
schema announced ``1.0.0`` while the footer rendered a hardcoded ``v0.1.5``,
so two documents disagreed about what was running.

These tests pin the invariant: one package version, rendered everywhere, and a
requirements file that lists only what the code actually imports.
"""

import re
import unittest
from pathlib import Path

from app.main import app, templates
from app.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_TEMPLATE = PROJECT_ROOT / "app" / "templates" / "base.html"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def declared_requirements():
    """The requirement lines only: a comment is prose, not a declaration."""
    entries = []

    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)

    return entries


def requirement_name(entry):
    """The distribution name of a requirement line, without extras or bounds."""
    matched = re.match(r"[A-Za-z0-9._-]+", entry)

    return matched.group().lower() if matched else ""


class VersionTest(unittest.TestCase):
    def test_the_package_declares_a_semver(self):
        parts = APP_VERSION.split(".")
        self.assertEqual(len(parts), 3, f"{APP_VERSION!r} is not MAJOR.MINOR.PATCH")
        self.assertTrue(all(part.isdigit() for part in parts), APP_VERSION)

    def test_the_openapi_schema_announces_the_package_version(self):
        self.assertEqual(app.version, APP_VERSION)

    def test_the_footer_renders_the_package_version(self):
        html = templates.env.get_template("base.html").render()

        self.assertIn(f"v{APP_VERSION}", html)

    def test_the_footer_does_not_hardcode_a_version(self):
        source = BASE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("app_version", source, "the footer stopped reading the package version")
        self.assertNotRegex(
            source,
            r"footer-version\">v\d+\.\d+\.\d+<",
            "the footer went back to a hardcoded version",
        )

    def test_the_readme_stays_evergreen(self):
        # openspec/changes/public-release-prep: the version lives in the code,
        # so the README must not pin a release number that will rot.
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotRegex(readme, r"\bv\d+\.\d+\.\d+\b", "the README pins a version")


class DeclaredDependenciesTest(unittest.TestCase):
    def test_requirements_does_not_declare_packages_the_app_never_imports(self):
        declared = {requirement_name(entry) for entry in declared_requirements()}

        for unused in ("aiofiles", "python-dotenv"):
            self.assertNotIn(
                unused,
                declared,
                f"{unused} is declared but never imported; drop it or use it",
            )

    def test_requirements_still_declares_what_the_app_needs(self):
        declared = {requirement_name(entry) for entry in declared_requirements()}

        for needed in ("fastapi", "uvicorn", "jinja2", "python-multipart", "websockets"):
            self.assertIn(needed, declared, f"{needed} is imported but not declared")


class RequirementBoundsTest(unittest.TestCase):
    """A clean install must reproduce the set that was actually tested.

    ``openspec/changes/upgrade-deps`` records why: open lower bounds let a fresh
    ``pip install -r requirements.txt`` resolve a combination nobody ran.
    """

    REQUIREMENT_LINE = re.compile(r"[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?~=\d+\.\d+\.\d+")

    def test_every_requirement_uses_a_compatible_release_bound(self):
        unpinned = [
            entry
            for entry in declared_requirements()
            if not self.REQUIREMENT_LINE.fullmatch(entry)
        ]

        self.assertEqual(
            [],
            unpinned,
            "these requirements are not pinned to a tested release:",
        )


if __name__ == "__main__":
    unittest.main()
