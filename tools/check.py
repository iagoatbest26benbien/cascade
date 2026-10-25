#!/usr/bin/env python3
"""Check Cascade's portable payload and discover runtime and packaging unittests."""

import argparse
import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
from install import payload_files  # The sibling installer; no installed package needed.


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)\n]*)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", re.M)
PRIVATE_PATH = re.compile(r"/(?:home|Users)/[^/\s<>\"']+/|[A-Za-z]:\\Users\\[^\\\s]+\\")
TEXT_SUFFIXES = {".md", ".py", ".html", ".yaml", ".yml", ".json", ".txt", ".svg"}


def markdown_prose(text):
    """Remove fenced and inline code, where example links are not navigable links."""
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is None and marker:
            fence = marker[1]
        elif fence is not None:
            if re.fullmatch(r"\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
        else:
            lines.append(line)
    return re.sub(r"(`+).*?\1", "", "\n".join(lines))


def check_markdown(source, relative, files):
    prose = markdown_prose((source / relative).read_text(encoding="utf-8"))
    targets = [match[1].strip("<>") for pattern in (LINK, REFERENCE) for match in pattern.finditer(prose)]
    for target in targets:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            if parsed.scheme == "file":
                raise ValueError(f"{relative}: machine-local file link: {target}")
            continue
        link = Path(unquote(parsed.path))
        if link.is_absolute():
            raise ValueError(f"{relative}: absolute local Markdown link: {target}")
        resolved = (source / relative.parent / link).resolve()
        try:
            local = resolved.relative_to(source.resolve())
        except ValueError:
            raise ValueError(f"{relative}: Markdown link escapes the skill: {target}") from None
        if local not in files and not any(local in item.parents for item in files):
            raise ValueError(f"{relative}: Markdown target is missing from the runtime payload: {target}")


def check_payload(source):
    """Validate Python 3.11 syntax, local Markdown file targets, and personal paths."""
    files = set(payload_files(source))
    for relative in sorted(files):
        path = source / relative
        if path.suffix in TEXT_SUFFIXES or relative.name == "LICENSE":
            text = path.read_text(encoding="utf-8")
            if PRIVATE_PATH.search(text):
                raise ValueError(f"{relative}: personal absolute path in the runtime payload")
        if path.suffix == ".md":
            check_markdown(source, relative, files)
        if path.suffix == ".py":
            ast.parse(path.read_bytes(), filename=str(relative), feature_version=(3, 11))
    for path in (source / "tools").glob("*.py"):
        ast.parse(path.read_bytes(), filename=str(path), feature_version=(3, 11))
    return len(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Requires Python 3.11+, Git, and Linux, macOS, or WSL. No pip dependencies.")
    parser.add_argument("--verbose", action="store_true", help="Print each discovered test name")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.exit(1, "Checks require Python 3.11 or newer.\n")
    try:
        count = check_payload(ROOT)
    except (OSError, ValueError, SyntaxError) as error:
        parser.exit(1, f"Payload check failed: {error}\n")
    print(f"Portable payload: {count} files; Markdown file targets and Python 3.11 syntax OK", flush=True)
    failed = False
    # macOS commonly aliases /var and /tmp. Keep fixture paths and Git's
    # canonical worktree paths consistent without changing the runtime tests.
    test_environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                            TMPDIR=str(Path(tempfile.gettempdir()).resolve()))
    for directory in ("scripts/tests", "tools"):
        print(f"Discovering unittests in {directory}", flush=True)
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", directory, "-p", "test_*.py"]
        if args.verbose:
            command.append("-v")
        failed |= subprocess.run(command, cwd=ROOT, env=test_environment).returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
