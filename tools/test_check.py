"""Portable payload checks catch broken links without treating examples as links."""

from pathlib import Path
import tempfile
import unittest

from check import check_payload


class PayloadCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="payload check ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for directory in ("scripts", "assets", "references", "agents"):
            (self.root / directory).mkdir()
        (self.root / "SKILL.md").write_text("[Guide](references/guide.md#heading)\n", encoding="utf-8")
        (self.root / "references/guide.md").write_text("# Heading\n", encoding="utf-8")

    def test_relative_links_and_fenced_or_inline_example_paths_are_valid(self):
        (self.root / "references/guide.md").write_text(
            "[Skill](../SKILL.md) [remote](https://example.test/guide)\n"
            "```text\n[$implement](/absolute/path/SKILL.md)\n```\n"
            "`[example](missing.md)`\n"
            "~~~markdown\n[example](also-missing.md)\n~~~\n"
            "[spaced]: <file with spaces.md>\n", encoding="utf-8")
        (self.root / "references/file with spaces.md").write_text("# Notes\n", encoding="utf-8")
        self.assertEqual(check_payload(self.root), 3)

    def test_local_markdown_target_must_be_inside_installed_payload(self):
        (self.root / "README.md").write_text("Repository only", encoding="utf-8")
        for target in ("missing.md", "README.md", "../outside.md", "/absolute/file.md", "file:///tmp/local.md"):
            with self.subTest(target=target):
                (self.root / "SKILL.md").write_text(f"[Missing]({target})\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    check_payload(self.root)

    def test_personal_absolute_paths_are_reported_without_printing_the_path(self):
        (self.root / "references/guide.md").write_text("Run /home/example/private/tool.py", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "personal absolute path") as error:
            check_payload(self.root)
        self.assertNotIn("/home/example", str(error.exception))

    def test_python_newer_than_the_documented_minimum_is_rejected(self):
        (self.root / "scripts/helper.py").write_text("type Alias = str\n", encoding="utf-8")
        with self.assertRaises(SyntaxError):
            check_payload(self.root)


if __name__ == "__main__":
    unittest.main()
