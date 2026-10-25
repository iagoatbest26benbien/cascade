#!/usr/bin/env python3
"""Install the Cascade runtime payload with Python 3.11+ on Linux, macOS, or WSL."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys


SOURCE_ROOT = Path(__file__).absolute().parents[1]
RUNTIME_DIRECTORIES = ("scripts", "assets", "references", "agents")
OPTIONAL_NOTICE_FILES = ("LICENSE", "NOTICE.md")
OPTIONAL_NOTICE_DIRECTORIES = ("LICENSES",)
EXCLUDED_DIRECTORIES = {
    "tests", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".venv", "node_modules",
}


def payload_files(source):
    """Return relative regular-file paths; never traverse a payload symlink."""
    source = Path(source)
    if source.is_symlink():
        raise ValueError(f"Source must not be a symlink: {source}")
    if not source.is_dir():
        raise ValueError(f"Source directory is missing or not a directory: {source}")

    files = []

    def visit(path, directory=False):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            raise ValueError(f"Required source path is missing: {path}") from None
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlinks are not allowed in the runtime payload: {path}")
        if directory:
            if not stat.S_ISDIR(mode):
                raise ValueError(f"Expected a runtime directory: {path}")
            for child in sorted(path.iterdir()):
                # Exclusions are by name, so excluded symlinks are never followed.
                if child.name in EXCLUDED_DIRECTORIES or child.name == ".DS_Store":
                    continue
                if child.suffix in {".pyc", ".pyo"}:
                    continue
                child_mode = child.lstat().st_mode
                visit(child, directory=stat.S_ISDIR(child_mode))
        else:
            if not stat.S_ISREG(mode):
                raise ValueError(f"Expected a regular runtime file: {path}")
            files.append(path.relative_to(source))

    visit(source / "SKILL.md")
    for name in RUNTIME_DIRECTORIES:
        visit(source / name, directory=True)
    for name in OPTIONAL_NOTICE_FILES:
        if os.path.lexists(source / name):
            visit(source / name)
    for name in OPTIONAL_NOTICE_DIRECTORIES:
        if os.path.lexists(source / name):
            visit(source / name, directory=True)
    return tuple(files)


@contextmanager
def open_payload_file(source, relative):
    """Open through directory descriptors so a replaced symlink cannot be copied."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(source, directory_flags)
    try:
        for name in relative.parts[:-1]:
            child = os.open(name, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"Expected a regular runtime file: {source / relative}")
            yield stream
    finally:
        os.close(descriptor)


def install(source, destination, check=False):
    """Reserve a new directory exclusively; remove our partial copy on failure."""
    source = Path(source).absolute()
    destination = Path(destination).expanduser().absolute()
    if os.path.lexists(destination):
        raise ValueError(f"Destination already exists; nothing was changed: {destination}")
    files = payload_files(source)
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("Destination must be outside the source directory.")
    if check:
        return files

    destination.parent.mkdir(parents=True, exist_ok=True)
    # mkdir is exclusive even if another installer passed the preflight check.
    destination.mkdir()
    try:
        for relative in files:
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with open_payload_file(source, relative) as incoming, output.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                mode = os.fstat(incoming.fileno()).st_mode
                # Preserve executable bits without carrying setuid/setgid bits.
                output.chmod(stat.S_IMODE(mode) & 0o777)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Examples:\n"
                "  python3 tools/install.py --dest '/path with spaces/cascade'\n"
                "  python3 tools/install.py --dest /new/skill/path --check\n\n"
                "Installs SKILL.md, scripts/, assets/, references/, and agents/, plus\n"
                "LICENSE, NOTICE.md, and LICENSES/ when present. Tests, caches, and\n"
                "repository tooling are excluded. Symlinks are refused. Existing\n"
                "destinations are never overwritten."),
    )
    parser.add_argument("--dest", type=Path, required=True, metavar="PATH",
                        help="Full new skill directory, not its parent; missing parents are created")
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT, metavar="PATH",
                        help="Skill source root (default: repository containing this installer)")
    parser.add_argument("--check", "--dry-run", dest="check", action="store_true",
                        help="Validate the source and absent destination, list files, and write nothing")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.exit(1, "Installation requires Python 3.11 or newer.\n")
    try:
        files = install(args.source, args.dest, check=args.check)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Installation refused: {error}\n")
    destination = args.dest.expanduser().absolute()
    if args.check:
        print(f"Check OK: would install {len(files)} files into {destination}")
        for relative in files:
            print(f"  {relative.as_posix()}")
    else:
        print(f"Installed {len(files)} runtime files into {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
