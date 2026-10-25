"""Exercise the public installer and the runtime from its installed location."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("install.py")
ROOT = SCRIPT.parent.parent
spec = importlib.util.spec_from_file_location("skill_installer", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cascade packaging ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.destination = self.root / "new parent" / "cascade"

    def cli(self, *arguments, success=True, script=SCRIPT):
        result = subprocess.run(
            [sys.executable, "-B", str(script), *map(str, arguments)], cwd=self.root,
            capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Traceback", result.stderr)
        return result

    def source_copy(self):
        source = self.root / "cascade source with spaces"
        source.mkdir()
        shutil.copy2(ROOT / "SKILL.md", source / "SKILL.md")
        for name in installer.RUNTIME_DIRECTORIES:
            shutil.copytree(ROOT / name, source / name)
        for name in installer.OPTIONAL_NOTICE_FILES:
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, source / name)
        for name in installer.OPTIONAL_NOTICE_DIRECTORIES:
            if (ROOT / name).exists():
                shutil.copytree(ROOT / name, source / name)
        (source / "tools").mkdir()
        shutil.copy2(SCRIPT, source / "tools/install.py")
        return source

    def test_installed_runtime_initializes_checks_and_reads_tracker_from_another_cwd(self):
        self.cli("--dest", self.destination)
        self.assertTrue((self.destination / "SKILL.md").is_file())
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {path.parts[0] for path in installer.payload_files(ROOT)})
        self.assertFalse((self.destination / "scripts/tests").exists())
        html = self.root / "separate workspace" / "tracker.html"
        tracker = self.destination / "scripts/tracker.py"
        self.cli("init", html, "--title", "Installed copy", script=tracker)
        self.cli("check", html, script=tracker)
        state = json.loads(self.cli("read", html, script=tracker).stdout)
        self.assertEqual(state["title"], "Installed copy")
        self.assertEqual(state["missions"], [])
        self.assertTrue((html.parent / "prompts").is_dir())
        self.assertTrue((html.parent / "evidence").is_dir())
        for relative in installer.payload_files(ROOT):
            self.assertEqual((self.destination / relative).read_bytes(), (ROOT / relative).read_bytes())

    def test_paths_with_spaces_work_for_source_installer_destination_and_cwd(self):
        source = self.source_copy()
        self.cli("--dest", self.destination, script=source / "tools/install.py")
        self.assertEqual((self.destination / "assets/tracker.html").read_bytes(),
                         (source / "assets/tracker.html").read_bytes())

    def test_existing_directory_is_refused_without_any_content_or_metadata_mutation(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination / "user skill.txt"
        sentinel.write_bytes(b"Keep my installed skill exactly.\n")
        before = (self.destination.stat().st_mtime_ns, sentinel.stat().st_mtime_ns, sentinel.read_bytes())
        result = self.cli("--dest", self.destination, success=False)
        self.assertIn("Destination already exists", result.stderr)
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(before,
                         (self.destination.stat().st_mtime_ns, sentinel.stat().st_mtime_ns, sentinel.read_bytes()))

    def test_existing_file_and_dangling_symlink_are_refused(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"User file")
        self.cli("--dest", self.destination, success=False)
        self.assertEqual(self.destination.read_bytes(), b"User file")
        self.destination.unlink()
        target = self.root / "nonexistent target"
        self.destination.symlink_to(target, target_is_directory=True)
        self.cli("--dest", self.destination, success=False)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(self.destination.readlink(), target)
        self.assertFalse(target.exists())

    def test_symlinked_payload_file_directory_and_source_are_refused_before_writing(self):
        source = self.source_copy()
        outside = self.root / "local private file"
        outside.write_text("Private bytes must stay outside the skill.", encoding="utf-8")
        file_link = source / "references/local.md"
        file_link.symlink_to(outside)
        result = self.cli("--source", source, "--dest", self.destination, success=False)
        self.assertIn("Symlinks", result.stderr)
        self.assertFalse(self.destination.parent.exists())
        file_link.unlink()
        directory_link = source / "scripts/local-directory"
        directory_link.symlink_to(self.root, target_is_directory=True)
        self.cli("--source", source, "--dest", self.destination, success=False)
        self.assertFalse(self.destination.exists())
        directory_link.unlink()
        source_link = self.root / "source link"
        source_link.symlink_to(source, target_is_directory=True)
        self.cli("--source", source_link, "--dest", self.destination, success=False)
        self.assertFalse(self.destination.exists())

    def test_dangling_payload_symlink_is_refused(self):
        source = self.source_copy()
        (source / "assets/missing").symlink_to(self.root / "missing")
        result = self.cli("--source", source, "--dest", self.destination, success=False)
        self.assertIn("Symlinks", result.stderr)
        self.assertFalse(self.destination.parent.exists())

    def test_tests_caches_bytecode_and_repository_files_are_not_copied(self):
        source = self.source_copy()
        for name in ("README.md", "private-notes.txt"):
            (source / name).write_text("Repository only", encoding="utf-8")
        for name in ("__pycache__", ".pytest_cache", ".git"):
            directory = source / "scripts" / name
            directory.mkdir(exist_ok=True)
            (directory / "local-data").write_bytes(b"Do not copy")
        (source / "scripts/helper.pyc").write_bytes(b"Bytecode")
        (source / "assets/.DS_Store").write_bytes(b"Finder metadata")
        self.cli("--source", source, "--dest", self.destination)
        for relative in ("README.md", "private-notes.txt", "tools", "scripts/tests", "scripts/__pycache__",
                         "scripts/.pytest_cache", "scripts/.git", "scripts/helper.pyc", "assets/.DS_Store"):
            self.assertFalse((self.destination / relative).exists(), relative)

    def test_optional_license_notices_are_preserved_exactly_and_may_be_absent(self):
        source = self.source_copy()
        (source / "LICENSE").write_bytes(b"Cascade license fixture\n")
        (source / "NOTICE.md").write_text("# Notices\n[Upstream](LICENSES/upstream.txt)\n", encoding="utf-8")
        (source / "LICENSES").mkdir(exist_ok=True)
        (source / "LICENSES/upstream.txt").write_bytes(b"Upstream license fixture\n")
        self.cli("--source", source, "--dest", self.destination)
        for relative in ("LICENSE", "NOTICE.md", "LICENSES/upstream.txt"):
            self.assertEqual((self.destination / relative).read_bytes(), (source / relative).read_bytes())
        for name in installer.OPTIONAL_NOTICE_FILES:
            (source / name).unlink()
        shutil.rmtree(source / "LICENSES")
        other_destination = self.root / "without optional notices" / "cascade"
        self.cli("--source", source, "--dest", other_destination)
        for name in (*installer.OPTIONAL_NOTICE_FILES, *installer.OPTIONAL_NOTICE_DIRECTORIES):
            self.assertFalse((other_destination / name).exists())

    def test_optional_license_symlinks_are_refused(self):
        source = self.source_copy()
        for name in installer.OPTIONAL_NOTICE_FILES:
            (source / name).unlink(missing_ok=True)
        shutil.rmtree(source / "LICENSES", ignore_errors=True)
        for name in (*installer.OPTIONAL_NOTICE_FILES, *installer.OPTIONAL_NOTICE_DIRECTORIES):
            with self.subTest(name=name):
                link = source / name
                link.symlink_to(self.root / "outside", target_is_directory=name == "LICENSES")
                result = self.cli("--source", source, "--dest", self.destination, success=False)
                self.assertIn("Symlinks", result.stderr)
                self.assertFalse(self.destination.parent.exists())
                link.unlink()

    def test_check_and_dry_run_list_payload_without_creating_parent_directories(self):
        for flag in ("--check", "--dry-run"):
            with self.subTest(flag=flag):
                result = self.cli("--dest", self.destination, flag)
                self.assertIn("would install", result.stdout)
                self.assertIn("assets/tracker.html", result.stdout)
                self.assertNotIn("scripts/tests/", result.stdout)
                self.assertFalse(self.destination.parent.exists())

    def test_missing_source_and_required_payload_file_have_clear_errors(self):
        source = self.root / "missing source"
        result = self.cli("--source", source, "--dest", self.destination, success=False)
        self.assertIn("Source directory is missing", result.stderr)
        source = self.source_copy()
        (source / "SKILL.md").unlink()
        result = self.cli("--source", source, "--dest", self.destination, success=False)
        self.assertIn("Required source path is missing", result.stderr)
        self.assertIn("SKILL.md", result.stderr)
        self.assertFalse(self.destination.parent.exists())

    def test_destination_is_required_and_self_install_is_refused(self):
        self.assertIn("--dest", self.cli(success=False).stderr)
        source = self.source_copy()
        nested = source / "scripts/installed skill"
        result = self.cli("--source", source, "--dest", nested, success=False)
        self.assertIn("outside the source", result.stderr)
        self.assertFalse(nested.exists())

    def test_concurrent_destination_creation_is_preserved(self):
        files = installer.payload_files(ROOT)

        def competing_install(source):
            self.destination.mkdir(parents=True)
            (self.destination / "other user.txt").write_bytes(b"Keep competing installation")
            return files

        with mock.patch.object(installer, "payload_files", side_effect=competing_install):
            with self.assertRaises(FileExistsError):
                installer.install(ROOT, self.destination)
        self.assertEqual((self.destination / "other user.txt").read_bytes(), b"Keep competing installation")
        self.assertFalse((self.destination / "SKILL.md").exists())

    def test_failed_copy_removes_only_its_partial_installation(self):
        sibling = self.root / "unrelated file"
        sibling.write_bytes(b"Keep")
        with mock.patch.object(installer.shutil, "copyfileobj", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                installer.install(ROOT, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(sibling.read_bytes(), b"Keep")

    def test_source_directory_replaced_by_symlink_after_validation_is_not_followed(self):
        source = self.source_copy()
        installer.payload_files(source)
        (source / "scripts").rename(source / "original scripts")
        (source / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        with self.assertRaises(OSError):
            with installer.open_payload_file(source, Path("scripts/tracker.py")):
                self.fail("Symlinked directory must not be opened")


if __name__ == "__main__":
    unittest.main()
