#!/usr/bin/env python3
"""Observe source versions without granting dispatch or acceptance authority.

Only capture() creates a new expected version. evaluate() never refreshes one.
GitHub reads use GET requests through gh; no response bodies or command errors
are returned. Equality describes these observations, not semantic test success.
"""

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import quote


ROLES = {"ticket", "code", "ci"}
KINDS = {"file", "git", "github_issue", "github_checks"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_SOURCES = 256
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PAGES = 100
MAX_CALLS = 128
TIMEOUT = 30
PAGE_SIZE = 100
# Git's repository-local environment (git rev-parse --local-env-vars). Transport
# credentials such as GIT_SSH_COMMAND/GIT_ASKPASS and normal config files remain.
GIT_CONTEXT_ENV = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def text(value, limit=1024):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def descriptor(spec):
    require(isinstance(spec, dict), "Source descriptor must be an object.")
    try:
        raw = json.dumps(spec, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("Source descriptor must be valid JSON data.") from None
    require(len(raw) <= MAX_DESCRIPTOR_BYTES, "Source descriptor exceeds observation limit.")
    require(text(spec.get("id"), 256), "Source id must be a nonempty bounded string.")
    require(isinstance(spec.get("role"), str) and spec["role"] in ROLES, "Unsupported source role.")
    require(isinstance(spec.get("kind"), str) and spec["kind"] in KINDS, "Unsupported source kind.")
    if spec["kind"] in {"file", "git"}:
        require(text(spec.get("path"), 4096), "Source path is missing or malformed.")
    if spec["kind"] in {"git", "github_checks"}:
        require(text(spec.get("ref")), "Source ref is missing or malformed.")
    if spec["kind"].startswith("github_"):
        repo = spec.get("repository")
        require(isinstance(repo, str) and len(repo) <= 256 and
                re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo) is not None and
                repo.split("/")[1] not in {".", ".."}, "Repository must be OWNER/REPO.")
    if spec["kind"] == "github_issue":
        require(type(spec.get("number")) is int and 0 < spec["number"] < 2**63,
                "Issue number must be a positive integer.")


class Reader:
    """A single source observation has a shared deadline and call budget."""

    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT
        self.calls = 0

    def run(self, argv):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        require(remaining > 0 and self.calls <= MAX_CALLS, "Source observation budget exceeded.")
        env = os.environ.copy()
        # Prevent inherited debugging from printing authenticated response headers.
        for name in list(env):
            if (name == "GH_DEBUG" or name.startswith("GIT_TRACE") or
                    (argv[0] == "git" and (name in GIT_CONTEXT_ENV or name.startswith("GIT_CONFIG")))):
                del env[name]
        env.update(GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=min(10, remaining), env=env, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError("Source command unavailable or timed out.") from None
        require(result.returncode == 0 and not result.stderr,
                "Source command failed or produced diagnostics.")
        require(len(result.stdout) <= MAX_BYTES, "Source response exceeds observation limit.")
        try:
            output = result.stdout.decode("utf-8")
        except UnicodeError:
            raise ValueError("Source response is not valid UTF-8.") from None
        require(re.search(r"\bredacted\b", output, re.IGNORECASE) is None,
                "Source response is redacted.")
        return output

    def api(self, endpoint):
        output = self.run(["gh", "api", "--hostname", "github.com", "--method", "GET",
                           "-H", "Accept: application/vnd.github+json", endpoint])
        try:
            return json.loads(output)
        except (ValueError, RecursionError):
            raise ValueError("Source response is not valid JSON.") from None

    def pages(self, endpoint, key=None):
        """Require exact totals for object pages; exhaust array pages explicitly."""
        rows, seen, total = [], set(), None
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in endpoint else "?"
            response = self.api(f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}")
            batch = response
            if key:
                require(isinstance(response, dict) and type(response.get("total_count")) is int
                        and response["total_count"] >= 0 and isinstance(response.get(key), list),
                        "Incomplete paginated source response.")
                count = response["total_count"]
                require(count <= PAGE_SIZE * MAX_PAGES and (total is None or count == total),
                        "Source pagination changed or exceeded limit.")
                total, batch = count, response[key]
            require(isinstance(batch, list) and len(batch) <= PAGE_SIZE,
                    "Malformed paginated source response.")
            for row in batch:
                require(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0,
                        "Source record identity is missing.")
                require(row["id"] not in seen, "Source pagination contains duplicate records.")
                seen.add(row["id"])
                rows.append(row)
            if key:
                require(len(rows) <= total, "Source pagination exceeds its total.")
                if len(rows) == total:
                    return rows
                require(len(batch) == PAGE_SIZE, "Source pagination is incomplete.")
            elif len(batch) < PAGE_SIZE:
                return rows
        raise ValueError("Source pagination exceeds observation limit.")


def local_path(spec, root):
    path = Path(spec["path"])
    return path if path.is_absolute() else root / path


def file_version(path):
    # Nonblocking open prevents a FIFO from hanging before the regular-file test.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), "File source is not a regular file.")
            require(before.st_size <= MAX_FILE_BYTES, "File source exceeds observation limit.")
            remaining, hashed = before.st_size, hashlib.sha256()
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                require(bool(block), "File source changed during observation.")
                remaining -= len(block)
                hashed.update(block)
            after = os.fstat(stream.fileno())
            current = path.stat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            require(identity(before) == identity(after) == identity(current),
                    "File source changed during observation.")
            return hashed.hexdigest()
    except OSError:
        raise ValueError("File source cannot be read.") from None


def index_gitlinks(entries):
    """Read cached modes/OIDs and flags from NUL-delimited ls-files output."""
    require(not entries or entries.endswith("\0"), "Git index response is malformed.")
    links = []
    for entry in entries.split("\0")[:-1]:
        header, separator, name = entry.partition("\t")
        values = header.split(" ")
        require(separator and name and len(values) == 4, "Git index response is malformed.")
        tag, mode, commit, stage = values
        # -v lowercases assume-unchanged entries; S/s means skip-worktree.
        require(tag == "H", "Git HEAD index is unmerged or suppresses dirty checks.")
        require(mode in {"100644", "100755", "120000", "160000"} and stage == "0"
                and COMMIT.fullmatch(commit) is not None, "Git index response is malformed.")
        if mode == "160000":
            require(all(part and part not in {".", "..", ".git"} for part in name.split("/")),
                    "Git submodule index path is unsafe.")
            links.append((name, commit))
    return links


def clean_git_worktrees(worktree, head, reader):
    """Iteratively check index-discovered modules using one observation budget."""
    pending, observed, seen = [(worktree.resolve(strict=True), head)], [], set()
    while pending:
        worktree, expected = pending.pop()
        require(worktree not in seen, "Git submodule worktree is repeated or cyclic.")
        seen.add(worktree)
        root_argv = ["git", "--no-optional-locks", "-C", worktree]
        top = reader.run(root_argv + ["rev-parse", "--show-toplevel"]).rstrip("\n")
        require(text(top, 4096) and Path(top).is_absolute()
                and Path(top).resolve(strict=True) == worktree, "Git submodule worktree is unavailable or mismatched.")
        current = reader.run(root_argv + ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
        require(current == expected, "Git HEAD differs from the observed or indexed commit.")
        # Process-local overrides and disabled optional locks preserve every
        # caller index/config, including the gitdir files of nested modules.
        checks = root_argv + ["-c", "core.fsmonitor=false", "-c", "core.ignoreStat=false",
                              "-c", "core.trustctime=true", "-c", "core.checkStat=default",
                              "-c", "core.untrackedCache=false", "-c", "core.fileMode=true"]
        links = index_gitlinks(reader.run(checks + ["ls-files", "--cached", "--stage", "-v", "-z"]))
        # A cache established under reduced stat checks is not trusted merely
        # because this process restores defaults. Pinned refs need no such check.
        for key, default, boolean in (("core.ignoreStat", "false", True),
                                      ("core.trustctime", "true", True),
                                      ("core.checkStat", "default", False)):
            command = ["config", "--get", "--default=" + default]
            if boolean:
                command.append("--type=bool")
            value = reader.run(root_argv + command + [key]).strip()
            require(value == default, "Git HEAD configuration suppresses dirty checks.")
        # Each module is checked explicitly. Avoid Git's own nested worktree
        # traversal so flags/config in children cannot be hidden or bypass the
        # Reader call budget. An explicit cached diff also covers staged gitlink
        # changes, which --ignore-submodules=all hides from porcelain status.
        reader.run(checks + ["diff-index", "--cached", "--quiet", "--no-ext-diff",
                             "--ignore-submodules=none", current, "--"])
        require(not reader.run(checks + ["status", "--porcelain=v1", "--untracked-files=all",
                                        "--ignore-submodules=all"]), "Git HEAD worktree is dirty.")
        observed.append((root_argv, current))
        for name, commit in links:
            child = worktree
            for part in name.split("/"):
                child = child / part
                require(not child.is_symlink(), "Git submodule path contains a symbolic link.")
            marker = child / ".git"
            require(child.is_dir() and not marker.is_symlink() and (marker.is_file() or marker.is_dir()),
                    "Git submodule worktree is unavailable or uninitialized.")
            child = child.resolve(strict=True)
            require(child != worktree and child.is_relative_to(worktree), "Git submodule path escapes its worktree.")
            pending.append((child, commit))
    # A module already checked must still name that same commit at the end.
    for argv, commit in observed:
        require(reader.run(argv + ["rev-parse", "--verify", "HEAD^{commit}"]).strip() == commit,
                "Git HEAD changed during observation.")


def git_version(spec, root, reader):
    argv = ["git", "--no-optional-locks", "-C", str(local_path(spec, root))]
    revision = lambda: reader.run(argv + ["rev-parse", "--verify", "--end-of-options",
                                          spec["ref"] + "^{commit}"]).strip()
    head = revision()
    require(COMMIT.fullmatch(head) is not None, "Git commit response is malformed.")
    if spec["ref"] == "HEAD":
        require(reader.run(argv + ["rev-parse", "--is-inside-work-tree"]).strip() == "true",
                "HEAD source requires a worktree.")
        worktree = reader.run(argv + ["rev-parse", "--show-toplevel"]).rstrip("\n")
        require(text(worktree, 4096) and Path(worktree).is_absolute(), "Git worktree response is malformed.")
        clean_git_worktrees(Path(worktree), head, reader)
        require(revision() == head, "Git HEAD changed during observation.")
    return digest({"kind": "git", "commit": head})


def fields(row, names):
    require(isinstance(row, dict) and all(name in row for name in names),
            "Source response is missing required fields.")
    return {name: row[name] for name in names}


def nullable_string(value):
    return value is None or isinstance(value, str)


def timestamp(value, nullable=False):
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def issue_version(spec, reader):
    repo = spec["repository"].lower()
    row = reader.api(f"repos/{repo}/issues/{spec['number']}")
    data = fields(row, ("id", "number", "title", "body", "state", "labels", "milestone", "updated_at"))
    require(type(data["id"]) is int and data["id"] > 0 and type(data["number"]) is int
            and data["number"] == spec["number"] and isinstance(data["title"], str)
            and nullable_string(data["body"]) and data["state"] in ("open", "closed")
            and timestamp(data["updated_at"]) and isinstance(data["labels"], list),
            "Issue source fields are malformed.")
    labels = []
    for label in data["labels"]:
        value = fields(label, ("id", "name"))
        require(type(value["id"]) is int and value["id"] > 0 and isinstance(value["name"], str),
                "Issue label is malformed.")
        labels.append(value)
    require(len({label["id"] for label in labels}) == len(labels), "Duplicate issue labels.")
    data["labels"] = sorted(labels, key=lambda value: value["id"])
    if data["milestone"] is not None:
        milestone = fields(data["milestone"], ("id", "number", "title", "state", "description", "updated_at", "due_on"))
        require(type(milestone["id"]) is int and milestone["id"] > 0
                and type(milestone["number"]) is int and milestone["number"] > 0
                and isinstance(milestone["title"], str) and milestone["state"] in ("open", "closed")
                and nullable_string(milestone["description"]) and timestamp(milestone["updated_at"])
                and timestamp(milestone["due_on"], nullable=True), "Issue milestone is malformed.")
        data["milestone"] = milestone
    data["state_reason"] = row.get("state_reason")
    require(nullable_string(data["state_reason"]), "Issue state reason is malformed.")
    return digest({"kind": "github_issue", "repository": repo, "issue": data})


def ci_record(row, head, run=False):
    names = ["id", "head_sha", "status", "conclusion"]
    names += ["name", "started_at", "completed_at", "external_id", "check_suite"] if run else ["created_at", "updated_at"]
    data = fields(row, names)
    require(data["head_sha"] == head and data["status"] in
            ("queued", "in_progress", "completed", "waiting", "requested", "pending")
            and data["conclusion"] in (None, "success", "failure", "neutral", "cancelled", "skipped",
                                       "timed_out", "action_required", "stale", "startup_failure"),
            "Check observation is malformed or belongs to another commit.")
    for name in ("started_at", "completed_at") if run else ("created_at", "updated_at"):
        require(timestamp(data[name], nullable=run), "Check timestamps are malformed.")
    if run:
        require(text(data["name"]) and nullable_string(data["external_id"])
                and isinstance(data["check_suite"], dict)
                and type(data["check_suite"].get("id")) is int
                and data["check_suite"]["id"] > 0, "Check run identity is malformed.")
        data["check_suite"] = {"id": data["check_suite"]["id"]}
    # Preserve attempt-related fields when the provider includes them. No response
    # prose or headers are exposed; these values only enter the digest.
    for name in ("run_attempt", "run_id", "updated_at", "created_at", "details_url", "html_url"):
        if name in row:
            if name in ("run_attempt", "run_id"):
                require(type(row[name]) is int and row[name] > 0, "Check attempt identity is malformed.")
            elif name in ("created_at", "updated_at"):
                require(timestamp(row[name]), "Check timestamps are malformed.")
            else:
                require(nullable_string(row[name]), "Check metadata is malformed.")
            data[name] = row[name]
    return data


def checks_version(spec, reader):
    repo = spec["repository"].lower()
    base = f"repos/{repo}"
    endpoint = f"{base}/commits/{quote(spec['ref'], safe='')}"
    commit = reader.api(endpoint)
    require(isinstance(commit, dict) and isinstance(commit.get("sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", commit["sha"]) is not None,
            "GitHub commit response is malformed.")
    head = commit["sha"]
    suites, runs = [], []
    # Traversing suites avoids the direct check-runs endpoint's 1000-suite cap.
    for row in reader.pages(f"{base}/commits/{head}/check-suites", "check_suites"):
        suites.append(ci_record(row, head))
        for run in reader.pages(f"{base}/check-suites/{row['id']}/check-runs?filter=all", "check_runs"):
            value = ci_record(run, head, run=True)
            require(value["check_suite"]["id"] == row["id"], "Check run belongs to another suite.")
            runs.append(value)
    require(len({run["id"] for run in runs}) == len(runs), "Duplicate check runs across suites.")
    statuses = []
    for row in reader.pages(f"{base}/commits/{head}/statuses"):
        value = fields(row, ("id", "state", "context", "description", "target_url", "created_at", "updated_at"))
        require(value["state"] in ("error", "failure", "pending", "success")
                and text(value["context"]) and timestamp(value["created_at"]) and timestamp(value["updated_at"])
                and nullable_string(value["description"]) and nullable_string(value["target_url"]),
                "Commit status is malformed.")
        statuses.append(value)
    # A moving branch must still resolve to the observed SHA at the end.
    final = reader.api(endpoint)
    require(isinstance(final, dict) and final.get("sha") == head,
            "GitHub ref changed during observation.")
    return digest({"kind": "github_checks", "repository": repo, "head": head,
                   "suites": sorted(suites, key=lambda row: row["id"]),
                   "runs": sorted(runs, key=lambda row: row["id"]),
                   "statuses": sorted(statuses, key=lambda row: row["id"])})


def capture(spec, root: Path) -> dict:
    """Copy a descriptor, replacing its fingerprint and UTC observation time.

    Raises ValueError with a fixed, non-sensitive reason for any failed read.
    """
    descriptor(spec)
    require(isinstance(root, Path), "Source root must be a pathlib.Path.")
    reader = Reader()
    try:
        kind = spec["kind"]
        if kind == "file":
            fingerprint = file_version(local_path(spec, root))
        elif kind == "git":
            fingerprint = git_version(spec, root, reader)
        elif kind == "github_issue":
            fingerprint = issue_version(spec, reader)
        else:
            fingerprint = checks_version(spec, reader)
        result = copy.deepcopy(spec)
    except (OSError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ValueError("Source descriptor or response is malformed.") from None
    result.update(fingerprint=fingerprint, observed_at=datetime.now(timezone.utc).isoformat())
    return result


def evaluate(specs, root: Path) -> list[dict]:
    """Reobserve every expected descriptor, leaving the supplied versions intact."""
    if not isinstance(specs, list) or not specs or len(specs) > MAX_SOURCES:
        return [{"id": None, "role": None, "status": "unknown", "expected": None,
                 "observed": None, "reason": "Sources must be a nonempty bounded list."}]
    ids = Counter(spec.get("id") for spec in specs if isinstance(spec, dict) and text(spec.get("id"), 256))
    results = []
    for spec in specs:
        row = {"id": None, "role": None, "status": "unknown", "expected": None, "observed": None,
               "reason": "Source descriptor is malformed."}
        if isinstance(spec, dict):
            if text(spec.get("id"), 256):
                row["id"] = spec["id"]
            if isinstance(spec.get("role"), str) and spec["role"] in ROLES:
                row["role"] = spec["role"]
        try:
            descriptor(spec)
            require(ids[spec["id"]] == 1, "Source ids must be unique.")
            expected = spec.get("fingerprint")
            require(isinstance(expected, str) and HEX.fullmatch(expected) is not None,
                    "Expected source fingerprint is missing or malformed.")
            row["expected"] = expected
            row["observed"] = capture(spec, root)["fingerprint"]
            row["status"] = "unchanged" if expected == row["observed"] else "changed"
            row["reason"] = "Source version matches." if row["status"] == "unchanged" else "Source version changed."
        except ValueError as error:
            row["reason"] = str(error)
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "check"))
    parser.add_argument("--spec", required=True, help="JSON descriptor, or nonempty descriptor list for check")
    parser.add_argument("--root", required=True, type=Path, help="Tracker directory for relative paths")
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec)
        result = capture(spec, args.root) if args.command == "capture" else evaluate(
            spec if isinstance(spec, list) else [spec], args.root)
    except (ValueError, RecursionError):
        print("Source capture failed: invalid descriptor or unavailable observation.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(args.command == "check" and any(row["status"] != "unchanged" for row in result))


if __name__ == "__main__":
    sys.exit(main())
