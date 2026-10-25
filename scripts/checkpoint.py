#!/usr/bin/env python3
"""Publish/verify exact Git checkpoints and restore a separate relocated workstream.

Standard library + git only. Remote operations use disposable repositories and
the configured credential helper or SSH agent. Caller worktrees, filters and
hooks are not used. No remote URL or credential is persisted in the snapshot.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from urllib.parse import quote, unquote, urlsplit


SCHEMA = "orchestrate-tickets/checkpoint/v1"
CATALOG_SCHEMA = "orchestrate-tickets/catalog/v1"
TRACKER_SCHEMA = "orchestrate-tickets/tracker/v1"
# Known v1 writers, retained as byte-exact regression fixtures. Validation uses
# the installed trusted module; archived code is never imported/executed.
LEGACY_TRACKERS = {
    "1629bbf4472608e024ec1a80c894146bc03b5c89a34602a2bcb1e2d1c096a2a5",  # Original writer.
    "2bfac353909a2565ccf3eb77c99c964fe7e23b238373ae3a3e34ef472f0e8c4e",  # R1: concurrency, before atomic export.
    "a2a219b4b57749b45b43ef3ce6807593859ef2e7208f4fbad5233c058590ab66",  # Before iterative dependency validation.
}
MANIFEST = "manifest.json"
TEXT_SUFFIXES = {".md", ".txt", ".rst"}
PROTECTED = {"owner", "owners", "attempt", "attempts", "dispatch", "delivery", "prompt_pointer", "request_id_pointer"}
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`\[\]()]+")
# Paths with spaces must be enclosed in Markdown links, quotes or backticks.
ABSOLUTE = re.compile(r"(?<![\w/.:~])/(?!/)[^\s\"'`<>\[\](){},;!?]*")
QUOTED = re.compile(r"[`\"'](/[^\n`\"']+)[`\"']")
MARKDOWN = re.compile(r"\]\((<[^>\n]+>|[^)\n]+?)(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?[ \t]*\)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def relative(value):
    require(isinstance(value, str) and value and "\\" not in value
            and not any(ord(c) < 32 for c in value), "Invalid relative path.")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value
            and all(p not in {"", ".", "..", ".git", ".checkpoint"} for p in path.parts),
            f"Unsafe relative path: {value!r}")
    return path


def absolute(value):
    require(isinstance(value, str) and value.startswith("/") and "\\" not in value
            and not any(ord(c) < 32 for c in value), "Expected a POSIX absolute path.")
    require(PurePosixPath(value).as_posix() == value
            and ".." not in PurePosixPath(value).parts,
            f"Noncanonical absolute path: {value!r}")
    return Path(value)


def no_symlinks(path):
    for part in (*reversed(path.parents), path):
        require(not part.is_symlink(), f"Symlink is not allowed: {part}")


def files_under(path):
    """Read regular files only; never dereference a symlink or enter .git."""
    no_symlinks(path)
    mode = path.lstat().st_mode
    if stat.S_ISREG(mode):
        return {"": path.read_bytes()}
    require(stat.S_ISDIR(mode), f"Not a regular file or directory: {path}")
    result = {}
    for folder, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            entry = Path(folder) / name
            require(name not in {".git", ".checkpoint"}, f"Reserved entry: {entry}")
            require(not entry.is_symlink(), f"Symlink is not allowed: {entry}")
            require(stat.S_ISREG(entry.lstat().st_mode) or stat.S_ISDIR(entry.lstat().st_mode),
                    f"Not a regular file or directory: {entry}")
        for name in files:
            entry = Path(folder) / name
            key = entry.relative_to(path).as_posix()
            relative(key)
            result[key] = entry.read_bytes()
    return result


def tracker_module():
    path = Path(__file__).resolve().with_name("tracker.py")
    content = path.read_bytes()
    module = types.ModuleType("checkpoint_tracker")
    module.__file__ = str(path)
    exec(compile(content, str(path), "exec"), module.__dict__)
    return module, content


def state_from(content, tracker):
    html = content.decode("utf-8")
    matches = list(tracker.DATA.finditer(html))
    require(len(matches) == 1, "Expected exactly one tracker-data block.")
    return html, json.loads(matches[0][2])


def validate_tracker(source, tracker_path, root, tracker, check_sources=False):
    html, data = state_from(source[tracker_path], tracker)
    tracker_root = root / PurePosixPath(tracker_path).parent
    exports = tracker.validate(data, tracker_root)
    for mission in data["missions"]:
        if mission.get("prompt") is not None:
            relative(mission["prompt"]["file"])
        history = mission.get("dispatch_history", [])
        require(isinstance(history, list), "Invalid dispatch history.")
        for dispatch in [mission.get("dispatch")] + history:
            if not isinstance(dispatch, dict) or not any(k in dispatch for k in ("tool_payload_file", "tool_payload_sha256")):
                continue
            sha = dispatch.get("tool_payload_sha256")
            require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha), "Invalid exact tool envelope digest.")
            path = dispatch.get("tool_payload_file")
            relative(path)
            require(path == "evidence/dispatch/" + sha + ".json", "Invalid exact tool envelope archive path.")
            member = (PurePosixPath(tracker_path).parent / path).as_posix()
            require(member in source and digest(source[member]) == sha, "Missing or changed exact tool envelope.")
    refs = [data["spec"]] + [m.get("report", "") for m in data["missions"]]
    for ref in refs:
        require(not ref or ref.startswith(("/", "#")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref),
                f"Use an absolute file path or URL for spec/report before publication: {ref!r}")
    for path, content in exports.items():
        key = path.relative_to(root).as_posix()
        require(source.get(key) == content.encode("utf-8"),
                f"Missing or divergent prompt export: {key}")
    if check_sources:
        # Same local-source check as tracker.py check, performed only after relocation.
        for ref in refs:
            if ref.startswith("/"):
                require(Path(ref).exists(), f"Missing local source: {ref}")
    return html, data


def walk_strings(value, label="state"):
    if isinstance(value, str):
        yield label, value
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk_strings(child, f"{label}[{i}]")
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from walk_strings(child, f"{label}.{key}")


def text_files(source, tracker_path):
    tracker_parent = PurePosixPath(tracker_path).parent
    prompts = (tracker_parent / "prompts").as_posix() + "/"
    contexts = {"context.md", (tracker_parent / "context.md").as_posix()}
    for path, content in source.items():
        if (path.startswith(prompts) and Path(path).suffix in TEXT_SUFFIXES
                or path in contexts
                or path.startswith("dependencies/") and Path(path).suffix in TEXT_SUFFIXES):
            yield path, content.decode("utf-8")


def immutable_files(source, manifest, tracker):
    """Reports/evidence are scanned, but never rewritten as working prose."""
    _, data = state_from(source[manifest["tracker"]], tracker)
    paths = {p for p in source if "evidence" in PurePosixPath(p).parts}
    parent = PurePosixPath(manifest["tracker"]).parent
    roots = [manifest["original_root"]] + [m["source"] for m in manifest["includes"]]
    for label, text in walk_strings(data):
        if not re.search(r"\.criteria\[\d+\]\.proof$|\.evidence\[\d+\]$|\.report$", label):
            continue
        targets = {mapping_for(ref, manifest) for ref in references(text, roots)}
        targets.update(relative_target(ref, parent, label) for ref in local_links(text, True, roots))
        for target in targets - {None, ""}:
            paths.update(p for p in source if p == target or p.startswith(target + "/"))
    return paths


def dependency_texts(source, manifest):
    opaque = []
    texts = []
    for path, content in source.items():
        if path == manifest["tracker"]:
            continue  # Tracker state is validated/scanned separately.
        if Path(path).suffix not in TEXT_SUFFIXES:
            # Structured receipts may contain historical host provenance. Their
            # UTF-8 encoding does not make every path a required attachment.
            opaque.append(path)
            continue
        try:
            text = content.decode("utf-8")
            if "\0" in text:
                raise UnicodeError("Opaque content")
        except UnicodeError:
            opaque.append(path)
            continue
        texts.append((path, text))
    return texts, sorted(opaque)


def references(text, roots=()):
    """Conservative explicit POSIX references, not shell/code interpretation."""
    found = set()
    # Known workstream/include roots may themselves contain spaces even when the
    # original unquoted suffix does not. Keep these roots whole during tokenizing.
    for root in sorted(roots, key=len, reverse=True):
        text = text.replace(root, root.replace(" ", "\ue000"))
    def url_replace(match):
        value = match[0]
        if value.lower().startswith("file://"):
            parsed = urlsplit(value)
            require(not parsed.netloc, "Use file:///absolute/path, without a host.")
            found.add(unquote(parsed.path))
        return " " * len(value)
    clean = URL.sub(url_replace, text)
    # Consume links before quotes: an optional Markdown title is not a path.
    def add_link(match):
        target = match[1].strip().strip("<>")
        if target.startswith("/"):
            found.add(target.split("#", 1)[0].rstrip("/"))
        return " " * len(match[0])
    clean = MARKDOWN.sub(add_link, clean)
    def add_quoted(match):
        found.add(match[1].rstrip("/"))
        return " " * len(match[0])
    clean = QUOTED.sub(add_quoted, clean)
    for match in ABSOLUTE.finditer(clean):
        value = match[0].rstrip(".:/")
        if value:
            found.add(value.split("#", 1)[0])
    return {p.replace("\ue000", " ") for p in found}


def mapping_for(path, manifest):
    # More specific explicit includes take precedence over the root mapping.
    mappings = manifest["includes"] + [{"source": manifest["original_root"],
                                        "destination": "", "kind": "directory"}]
    for item in sorted(mappings, key=lambda x: len(x["source"]), reverse=True):
        source = item["source"]
        if path == source or item["kind"] == "directory" and path.startswith(source + "/"):
            suffix = path[len(source):].lstrip("/")
            return "/".join(p for p in (item["destination"], suffix) if p)
    return None


def local_links(text, factual, roots=()):
    targets = [m[1].strip().strip("<>") for m in MARKDOWN.finditer(text)]
    if factual:
        plain = MARKDOWN.sub(lambda m: " " * len(m[0]), text)
        plain = URL.sub(lambda m: " " * len(m[0]), plain)
        # An absolute path whose root contains spaces and a word like evidence/
        # is one reference, not an extra relative proof inside that root.
        for ref in references(text, roots):
            plain = plain.replace(ref, " " * len(ref))
        targets.extend(m[0].rstrip(".,;") for m in re.finditer(
            r"(?<![\w/:])(?:evidence/|prompts/|dependencies/|\./|\.\./)[^\s<>\"'`\[\]()]+", plain))
        if re.fullmatch(r"[\w.-]+\.[A-Za-z0-9]{1,10}", text.strip()):
            targets.append(text.strip())
    return targets


def relative_target(target, parent, label):
    target = target.split("#", 1)[0]
    if not target or target.startswith(("/", "#")) or re.match(r"[\w+.-]+:", target):
        return None
    parts = list(parent.parts)
    for part in unquote(target).split("/"):
        if part == "..":
            require(parts, f"Relative link escapes checkpoint: {label}")
            parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def git_source_recoveries(data):
    """Explicit external code locators, bound to the expected source version."""
    records = []
    ignored = set()
    for i, mission in enumerate(data["missions"]):
        guard = mission.get("guard")
        if not isinstance(guard, dict) or not isinstance(guard.get("sources"), list):
            continue
        for j, spec in enumerate(guard["sources"]):
            if not isinstance(spec, dict) or spec.get("kind") != "git" or "recovery" not in spec:
                continue
            recovery = spec["recovery"]
            require(isinstance(recovery, dict) and set(recovery) == {"remote", "commit"}, "Invalid Git source recovery locator.")
            safe_remote(recovery["remote"])
            commit = recovery["commit"]
            require(isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit), "Git recovery needs a full commit.")
            raw = json.dumps({"kind":"git", "commit":commit}, sort_keys=True, separators=(",", ":")).encode()
            require(spec.get("fingerprint") == digest(raw), "Git recovery commit differs from the expected source fingerprint.")
            require(isinstance(spec.get("id"), str) and spec["id"] and isinstance(spec.get("path"), str) and spec["path"], "Git recovery needs a source ID and checkout path.")
            records.append({"pointer":f"/missions/{i}/guard/sources/{j}", "id":spec["id"],
                            "remote":recovery["remote"], "commit":commit, "fingerprint":spec["fingerprint"]})
            label = f"state.missions[{i}].guard.sources[{j}]"
            ignored.update((label + ".path", label + ".recovery.remote"))
    return records, ignored


def retrieve_git_sources(records):
    for record in records:
        with tempfile.TemporaryDirectory(prefix="checkpoint-code-read-") as directory:
            repo = Path(directory) / "repo"
            init_repo(repo)
            git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none",
                record["remote"], record["commit"], timeout=30)
            observed = git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}", timeout=10).decode().strip()
            require(observed == record["commit"], "Retrieved code differs from the expected source commit.")


def dependency_check(source, manifest, tracker):
    _, data = state_from(source[manifest["tracker"]], tracker)
    recoveries, source_locations = git_source_recoveries(data)
    if recoveries:
        proof = manifest.get("source_recovery")
        require(isinstance(proof, dict) and proof.get("sources") == recoveries
                and isinstance(proof.get("observed_at"), str) and proof["observed_at"],
                "Missing verified Git source recovery inventory.")
    else:
        require("source_recovery" not in manifest, "Unexpected Git source recovery inventory.")
    prose, opaque = dependency_texts(source, manifest)
    state_texts = list(walk_strings(data))
    runtime_checkouts = set()
    tracker_root = Path(manifest["original_root"]) / PurePosixPath(manifest["tracker"]).parent
    for label, value in state_texts:
        if label in source_locations and label.endswith(".path"):
            runtime_checkouts.add(str(absolute(value if value.startswith("/") else str(tracker_root / value))))
    texts = state_texts + prose
    immutable = immutable_files(source, manifest, tracker)
    tracker_parent = PurePosixPath(manifest["tracker"]).parent
    output_dirs = [(tracker_parent / name).as_posix() for name in ("prompts", "evidence")]
    for label, text in texts:
        if label in source_locations:
            continue  # External runtime checkout, verified through its pinned remote.
        if re.fullmatch(r"state\.missions\[\d+\]\.guard\.(?:prompt_pointer|request_id_pointer)", label):
            require((not text or text.startswith("/")) and not re.search(r"~(?![01])", text), "Invalid guard JSON pointer.")
            continue
        factual = label in immutable or re.search(r"\.criteria\[\d+\]\.proof$|\.evidence\[\d+\]$|\.report$", label)
        origins = [manifest] + (manifest.get("evidence_origins", []) if label in immutable else [])
        roots = [o["original_root"] for o in origins] + [m["source"] for o in origins for m in o["includes"]]
        for ref in references(text, roots):
            absolute(ref)
            if ref in runtime_checkouts:
                continue  # Declared external checkout root, not bundled source bytes.
            targets = {mapping_for(ref, origin) for origin in origins} - {None}
            require(len(targets) <= 1, f"{label}: ambiguous historical local reference {ref!r}.")
            target = next(iter(targets), None)
            require(target is not None,
                    f"{label}: external local reference {ref!r}; include the file/directory "
                    "with --include /absolute/path=dependencies/relative or remove the dependency.")
            # Future prompt/report destinations are allowed inside these managed output dirs.
            managed = (not target or target in source or any(p.startswith(target + "/") for p in source)
                       or not factual and any(target == d or target.startswith(d + "/") for d in output_dirs))
            require(managed, f"{label}: reference {ref!r} is not captured; explicitly --include it.")
        # Relative Markdown file links in included prose must also be present.
        if label in source or factual:
            for target in local_links(text, factual, roots):
                parent = PurePosixPath(label).parent if label in source else tracker_parent
                resolved = relative_target(target, parent, label)
                if resolved is None:
                    continue
                require(resolved in source or any(p.startswith(resolved + "/") for p in source),
                        f"{label}: missing local Markdown dependency {target!r}; include its directory.")
    return {"schema": "explicit-local-prose/v1", "opaque": opaque}


def relocate_text(text, manifest, destination):
    mappings = manifest["includes"] + [{"source": manifest["original_root"], "destination": ""}]
    # A single substitution prevents a destination containing an old source path
    # from being substituted a second time. Boundaries avoid /root matching /root2.
    table = {m["source"]: str(destination / m["destination"]) for m in mappings}
    pattern = re.compile(r"(?<![\w/.:~])(?:file://)?(" + "|".join(
        re.escape(p) for p in sorted(table, key=len, reverse=True)) + r")(?=/|[\s\"'`<>\[\](){},;.!?#]|$)")
    def substitute(value):
        return pattern.sub(lambda m: ("file://" if m[0].startswith("file://") else "") + table[m[1]], value)
    urls = []
    def file_url(match):
        if not match[0].startswith("file://"):
            value = match[0]
        else:
            parsed = urlsplit(match[0])
            path = substitute(unquote(parsed.path))
            value = "file://" + quote(path, safe="/") + ("#" + parsed.fragment if parsed.fragment else "")
        urls.append(value)
        return f"\ue001{len(urls) - 1}\ue002"
    masked = substitute(URL.sub(file_url, text))
    return re.sub("\ue001([0-9]+)\ue002", lambda m: urls[int(m[1])], masked)


def relocate_value(value, manifest, destination):
    if isinstance(value, str):
        return relocate_text(value, manifest, destination)
    if isinstance(value, list):
        return [relocate_value(v, manifest, destination) for v in value]
    if isinstance(value, dict):
        return {k: v if k in PROTECTED else relocate_value(v, manifest, destination)
                for k, v in value.items()}
    return value


def active_view(source, manifest, destination, tracker):
    active = dict(source)
    html, data = state_from(source[manifest["tracker"]], tracker)
    data = relocate_value(data, manifest, destination)
    if "workstream" in manifest:
        identity = workstream_identity(manifest["workstream"])
        require("workstream" not in data or data["workstream"] == identity,
                "Active tracker workstream differs from the verified checkpoint identity.")
        data["workstream"] = dict(identity)
    active[manifest["tracker"]] = tracker.render(relocate_text(html, manifest, destination), data).encode("utf-8")
    immutable = immutable_files(source, manifest, tracker)
    # Filter before decoding: binary reports are valid immutable evidence.
    for path, text in text_files({p: v for p, v in source.items() if p not in immutable}, manifest["tracker"]):
        active[path] = relocate_text(text, manifest, destination).encode("utf-8")
    # Verify references in active views without trusting that substitutions caught
    # every encoding. Historical evidence and .checkpoint/source stay exact.
    view_manifest = dict(manifest, original_root=str(destination), includes=[],
                         evidence_origins=[origin_mapping(manifest)] + manifest.get("evidence_origins", []))
    dependency_check(active, view_manifest, tracker)
    validate_tracker(active, manifest["tracker"], destination, tracker)
    return active


def collect(root, tracker_path, includes):
    root = absolute(str(root))
    require(str(root) != "/", "Workstream root cannot be /.")
    no_symlinks(root)
    require(root.is_dir(), f"Workstream directory does not exist: {root}")
    relative(tracker_path)
    tracker_parent = PurePosixPath(tracker_path).parent
    source = {}
    executable = {}
    selected = {tracker_path, "context.md", "dependencies"}
    selected.update((tracker_parent / p).as_posix() for p in ("prompts", "evidence", "context.md"))
    for key in sorted(selected):
        path = root / key
        if not path.exists() and not path.is_symlink():
            require(key != tracker_path, f"Missing tracker: {path}")
            continue
        for sub, content in files_under(path).items():
            name = key + ("/" + sub if sub else "")
            relative(name)
            source[name] = content
            executable[name] = bool((path / sub if sub else path).stat().st_mode & 0o111)
    mappings = []
    for value in includes:
        require("=" in value, "--include requires /absolute/file-or-directory=dependencies/relative")
        origin, target = value.split("=", 1)
        origin = absolute(origin)
        relative(target)
        internal = origin.is_relative_to(root) and origin != root
        require(target.startswith("dependencies/") or internal and target == origin.relative_to(root).as_posix(),
                "--include destinations must be under dependencies/ or the SAME source-relative location.")
        require(str(origin) != str(root), "Include specific dependencies, not the whole workstream root.")
        for previous in mappings:
            require(not (target == previous["destination"] or target.startswith(previous["destination"] + "/")
                         or previous["destination"].startswith(target + "/")), "Overlapping --include destinations.")
            require(not (str(origin) == previous["source"] or str(origin).startswith(previous["source"] + "/")
                         or previous["source"].startswith(str(origin) + "/")), "Overlapping --include sources.")
        content = files_under(origin)
        require(content, f"Empty include cannot be preserved by Git; include a directory containing files: {origin}")
        kind = "directory" if origin.is_dir() else "file"
        require(not any(p == target or p.startswith(target + "/") or target.startswith(p + "/") for p in source),
                f"--include destination collides with workstream files: {target}")
        for sub, payload in content.items():
            key = target + ("/" + sub if sub else "")
            relative(key)
            source[key] = payload
            executable[key] = bool((origin / sub if sub else origin).stat().st_mode & 0o111)
        mappings.append({"source": str(origin), "destination": target, "kind": kind})
    return source, mappings, executable


def write_files(root, entries, executable=None):
    for key, content in entries.items():
        path = root / relative(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        if executable is not None and key in executable:
            path.chmod(0o755 if executable[key] else 0o644)


def origin_mapping(manifest):
    return {"original_root": manifest["original_root"], "includes": manifest["includes"]}


def workstream_identity(value):
    require(isinstance(value, dict) and set(value) == {"id", "source"}, "Invalid workstream identity.")
    require(isinstance(value["id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)+", value["id"]),
            "Workstream ID must be project/stable-workstream.")
    canonical_source(value["source"])
    return value


def canonical_source(value):
    require(isinstance(value, str) and URL.fullmatch(value), "Canonical workstream source must be a clean URL.")
    parsed = urlsplit(value)
    require(parsed.scheme.lower() != "file" and parsed.netloc and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment,
            "Canonical workstream source must be a clean URL without credentials, query or fragment.")
    return value


def check_binding(manifest, source, tracker, selected, adoption=None, label="checkpoint"):
    inherited = manifest.get("workstream")
    if inherited is not None:
        require(inherited == selected, f"{label}: workstream identity differs from selected workstream.")
    _, state = state_from(source[manifest["tracker"]], tracker)
    spec = state["spec"]
    if spec.startswith("/"):
        require(inherited is not None or adoption == selected["source"],
                f"{label}: local-spec legacy/first binding requires explicit --adopt-workstream-source "
                "matching the registered canonical source; a local path does not establish that match.")
    else:
        require(canonical_source(spec) == selected["source"],
                f"{label}: selected spec source differs from registered canonical source.")


def expected_identity(manifest, expected):
    if expected is not None:
        require(manifest.get("workstream", {}).get("id") == expected,
                "Expected workstream ID differs or checkpoint is unbound; legacy source alone is not a binding.")


def limitations(manifest, files=None):
    result = []
    if "workstream" not in manifest:
        result.append("Unbound legacy/standalone checkpoint: workstream identity is not established.")
    if "dependency_scan" not in manifest:
        result.append("Legacy completeness: no recorded prose dependency scan; current explicit links were checked, "
                      "but historical attachment inventory is unknown.")
    if "executable" not in manifest:
        result.append("Legacy executable flags are unknown; absent metadata does not establish executability.")
    if manifest.get("source_recovery"):
        result.append("External Git sources were retrieved at " + manifest["source_recovery"]["observed_at"]
                      + "; rehydrate their pinned checkouts before action. Offline verification is not a fresh retention check.")
    opaque = manifest.get("dependency_scan", {}).get("opaque", [])
    if files is not None and "dependency_scan" not in manifest:
        source = {p[7:]: v for p, v in files.items() if p.startswith("source/")}
        opaque = dependency_texts(source, manifest)[1]
    if opaque:
        result.append("Opaque files preserved without dependency inspection: " + ", ".join(opaque))
    return result


def make_snapshot(root, tracker_path, includes, workstream=None, adoption=None):
    tracker, tracker_code = tracker_module()
    source, mappings, executable = collect(root, tracker_path, includes)
    validate_tracker(source, tracker_path, root, tracker)
    manifest = {"schema": SCHEMA, "tracker_schema": TRACKER_SCHEMA,
                "created_at": datetime.now(timezone.utc).isoformat(), "original_root": str(root),
                "tracker": tracker_path, "includes": mappings, "executable": executable}
    _, state = state_from(source[tracker_path], tracker)
    recoveries, _ = git_source_recoveries(state)
    if recoveries:
        retrieve_git_sources(recoveries)
        manifest["source_recovery"] = {"sources":recoveries, "observed_at":datetime.now(timezone.utc).isoformat()}
    previous = root / ".checkpoint"
    old_files, old_manifest = {}, {}
    if previous.exists():
        old_files = local_snapshot(previous)
        old_manifest = verify_files(old_files)
        manifest["evidence_origins"] = old_manifest.get("evidence_origins", [])[:]
        origin = origin_mapping(old_manifest)
        if origin not in manifest["evidence_origins"]:
            manifest["evidence_origins"].append(origin)
        if "workstream" in old_manifest:
            manifest["workstream"] = old_manifest["workstream"]
    if workstream is not None:
        workstream_identity(workstream)
        check_binding(manifest, source, tracker, workstream, adoption, "active workstream")
        if old_manifest:
            old_source = {k[len("source/"):]: v for k, v in old_files.items() if k.startswith("source/")}
            check_binding(old_manifest, old_source, tracker, workstream, adoption, "inherited .checkpoint")
        manifest["workstream"] = workstream
    manifest["dependency_scan"] = dependency_check(source, manifest, tracker)
    # Exercise schema/export and local source checks in an isolated relocated view.
    with tempfile.TemporaryDirectory(prefix="checkpoint-check-") as directory:
        target = Path(directory) / "active"
        active = active_view(source, manifest, target, tracker)
        write_files(target, active, executable)
        validate_tracker(active, tracker_path, target, tracker, check_sources=True)
    files = {"source/" + k: v for k, v in source.items()}
    # Carry content-addressed original prompts across later publications. Active
    # copies may have been relocated, but an uncertain request keeps its old bytes.
    payload_records = []
    if old_manifest:
        payload_records.extend(old_manifest.get("payloads", []))
        files.update({k: v for k, v in old_files.items() if k.startswith("originals/")})
        if not old_manifest.get("payloads"):
            # Earlier v1 snapshots stored originals only under source/. Import
            # them before deriving a new working view so upgrading is lossless.
            old_source = {k[len("source/"):]: v for k, v in old_files.items() if k.startswith("source/")}
            _, old_state = state_from(old_source[old_manifest["tracker"]], tracker)
            old_parent = PurePosixPath(old_manifest["tracker"]).parent
            old_missions = {str(old_parent / m["prompt"]["file"]): m
                            for m in old_state["missions"] if m.get("prompt")}
            for name, content in old_source.items():
                if name.startswith((old_parent / "prompts").as_posix() + "/"):
                    sha = digest(content)
                    files["originals/" + sha + ".prompt"] = content
                    mission = old_missions.get(name, {})
                    payload_records.append({"sha256": sha, "source_path": name,
                                            "source_root": old_manifest["original_root"],
                                            "mission": mission.get("id"),
                                            "delivery": mission.get("prompt", {}).get("delivery"),
                                            "dispatch": mission.get("dispatch", {})})
    _, current_state = state_from(source[tracker_path], tracker)
    prompt_prefix = (PurePosixPath(tracker_path).parent / "prompts").as_posix() + "/"
    current_missions = {str(PurePosixPath(tracker_path).parent / m["prompt"]["file"]): m
                        for m in current_state["missions"] if m.get("prompt")}
    for name, content in source.items():
        if not name.startswith(prompt_prefix):
            continue
        sha = digest(content)
        files["originals/" + sha + ".prompt"] = content
        mission = current_missions.get(name, {})
        dispatch = mission.get("dispatch", {})
        original_sha = dispatch.get("payload_sha256") if isinstance(dispatch, dict) else None
        if isinstance(dispatch, dict) and not original_sha:
            identity = next((dispatch[k] for k in ("request_id", "request", "idempotency_key") if dispatch.get(k)), None)
            if identity is not None:
                for carried in payload_records:
                    context = carried.get("dispatch", {})
                    prior_id = next((context[k] for k in ("request_id", "request", "idempotency_key") if context.get(k)), None)
                    if carried.get("mission") == mission.get("id") and prior_id == identity:
                        original_sha = carried["sha256"]
                        break
        derived_from = None
        if original_sha and original_sha != sha:
            require("originals/" + original_sha + ".prompt" in files,
                    "Declared original request payload is missing from the carried archive.")
            derived_from, dispatch = original_sha, {}
        elif dispatch:
            dispatch = dict(dispatch, payload_sha256=sha)
        record = {"sha256": sha, "source_path": name, "source_root": str(root),
                  "mission": mission.get("id"),
                  "delivery": mission.get("prompt", {}).get("delivery"),
                  "dispatch": dispatch}
        if derived_from:
            record["view_of_payload"] = derived_from
        if record not in payload_records:
            payload_records.append(record)
    manifest["payloads"] = payload_records
    files["tools/tracker.py"] = tracker_code
    files["tools/checkpoint.py"] = Path(__file__).resolve().read_bytes()
    for helper_name in ("resume.py", "bootstrap.py", "guard.py", "claims.py", "sources.py"):
        helper = Path(__file__).resolve().with_name(helper_name)
        if helper.exists():
            files["tools/" + helper_name] = helper.read_bytes()
    manifest["files"] = {k: digest(v) for k, v in sorted(files.items())}
    files[MANIFEST] = json_bytes(manifest)
    return files


def validate_snapshot(files):
    require(MANIFEST in files, "Missing manifest.json.")
    manifest = json.loads(files[MANIFEST].decode("utf-8"))
    require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA, "Unsupported checkpoint schema.")
    require(manifest.get("tracker_schema") == TRACKER_SCHEMA, "Unsupported tracker schema.")
    if "workstream" in manifest:
        workstream_identity(manifest["workstream"])
    original = absolute(manifest.get("original_root"))
    require(str(original) != "/", "Workstream root cannot be /.")
    tracker_path = manifest.get("tracker")
    relative(tracker_path)
    hashes = manifest.get("files")
    require(isinstance(hashes, dict) and hashes, "Missing file hashes.")
    for path, expected in hashes.items():
        relative(path)
        require(path.startswith("source/") or re.fullmatch(r"originals/[a-f0-9]{64}\.prompt", path)
                or path in {"tools/tracker.py", "tools/checkpoint.py", "tools/resume.py", "tools/bootstrap.py",
                            "tools/guard.py", "tools/claims.py", "tools/sources.py"},
                f"Unexpected managed path: {path}")
        require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "Invalid SHA-256.")
    require(set(files) == set(hashes) | {MANIFEST}, "Extra or missing checkpoint files.")
    for path, expected in hashes.items():
        require(digest(files[path]) == expected, f"Changed checkpoint file: {path}")
    payloads = manifest.get("payloads", [])
    require(isinstance(payloads, list), "Invalid original payload records.")
    payload_paths = set()
    for record in payloads:
        require(isinstance(record, dict), "Invalid original payload record.")
        sha = record.get("sha256")
        require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha), "Invalid payload hash.")
        path = "originals/" + sha + ".prompt"
        require(path in hashes and digest(files[path]) == sha, "Missing or changed original payload.")
        relative(record.get("source_path"))
        absolute(record.get("source_root"))
        require(isinstance(record.get("dispatch", {}), dict), "Invalid original dispatch context.")
        if record.get("view_of_payload"):
            original_sha = record["view_of_payload"]
            require(isinstance(original_sha, str) and re.fullmatch(r"[a-f0-9]{64}", original_sha)
                    and "originals/" + original_sha + ".prompt" in hashes
                    and not record.get("dispatch"), "Invalid relocated payload provenance.")
        payload_paths.add(path)
    require({p for p in hashes if p.startswith("originals/")} == payload_paths,
            "Unindexed original payloads.")
    require("source/" + tracker_path in hashes and all(p in hashes for p in ("tools/tracker.py", "tools/checkpoint.py")),
            "Missing tracker or portable recovery helpers.")
    for path in hashes:
        require(not any(parent.as_posix() in hashes for parent in PurePosixPath(path).parents),
                f"File/directory collision: {path}")
    validate_mappings(manifest, hashes)
    origins = manifest.get("evidence_origins", [])
    require(isinstance(origins, list), "Invalid historical evidence mappings.")
    for origin in origins:
        require(isinstance(origin, dict) and set(origin) == {"original_root", "includes"},
                "Invalid historical evidence mapping.")
        validate_mappings(origin)
    executable = manifest.get("executable")
    if "executable" in manifest:
        require(isinstance(executable, dict) and set(executable) == {p[7:] for p in hashes if p.startswith("source/")}
                and all(type(flag) is bool for flag in executable.values()), "Invalid regular-file executable metadata.")
    tracker, trusted_code = tracker_module()
    require(files["tools/tracker.py"] == trusted_code or digest(files["tools/tracker.py"]) in LEGACY_TRACKERS,
            "Tracker helper differs from this pinned schema implementation.")
    source = {k[len("source/"):]: v for k, v in files.items() if k.startswith("source/")}
    validate_tracker(source, tracker_path, original, tracker)
    if "workstream" in manifest:
        check_binding(manifest, source, tracker, manifest["workstream"])
    scan = dependency_check(source, manifest, tracker)
    if "dependency_scan" in manifest:
        require(manifest["dependency_scan"] == scan, "Invalid prose dependency scan metadata.")
    return manifest, source, tracker


def validate_mappings(manifest, hashes=None):
    original = absolute(manifest.get("original_root"))
    require(str(original) != "/", "Workstream root cannot be /.")
    mappings = manifest.get("includes")
    require(isinstance(mappings, list), "Invalid include mappings.")
    for i, item in enumerate(mappings):
        require(isinstance(item, dict), "Invalid include mapping.")
        source = absolute(item.get("source"))
        relative(item.get("destination"))
        internal = source.is_relative_to(original) and source != original
        require(item.get("kind") in {"file", "directory"}
                and (item["destination"].startswith("dependencies/")
                     or internal and item["destination"] == source.relative_to(original).as_posix()),
                "Invalid dependency destination/kind.")
        require(item["source"] != str(original), "Include cannot replace the workstream root.")
        target = "source/" + item["destination"]
        if hashes is not None:
            require(target in hashes if item["kind"] == "file" else any(p.startswith(target + "/") for p in hashes),
                    f"Missing included dependency: {item['destination']}")
        for other in mappings[:i]:
            for field in ("source", "destination"):
                a, b = item[field], other[field]
                require(not (a == b or a.startswith(b + "/") or b.startswith(a + "/")),
                        "Overlapping include mappings.")


def archive_modes(manifest):
    return {"source/" + path: flag for path, flag in manifest.get("executable", {}).items()}


def check_stored_modes(files, modes):
    if MANIFEST in files:
        manifest = json.loads(files[MANIFEST])
        require(isinstance(manifest, dict), "Invalid checkpoint manifest.")
        flags = manifest.get("executable", {})
        require(isinstance(flags, dict) and all(type(v) is bool for v in flags.values()),
                "Invalid regular-file executable metadata.")
        for path, flag in flags.items():
            require(modes.get("source/" + path) == flag, f"Changed checkpoint executable flag: source/{path}")


def local_snapshot(path):
    path = absolute(str(path))
    no_symlinks(path)
    require(path.is_dir(), f"Not a checkpoint directory: {path}")
    result = {}
    modes = {}
    for entry in path.iterdir():
        if entry.name == ".git":
            require(not entry.is_symlink(), "Symlink .git is not allowed.")
            continue  # Clone metadata is never part of the managed checkpoint.
        relative(entry.name)
        for sub, content in files_under(entry).items():
            key = entry.name + ("/" + sub if sub else "")
            relative(key)
            result[key] = content
            modes[key] = bool((path / key).stat().st_mode & 0o111)
    check_stored_modes(result, modes)
    return result


def safe_remote(remote):
    require(isinstance(remote, str) and remote and not any(ord(c) < 32 for c in remote),
            "Invalid remote; supply an absolute local path or a clean HTTPS/SSH URL.")
    if remote.startswith("/"):
        absolute(remote)
        require("?" not in remote and "#" not in remote, "Invalid local remote path.")
        return
    require(not any(c.isspace() for c in remote), "Whitespace is not allowed in remote URLs.")
    parsed = urlsplit(remote)
    if parsed.scheme in {"https", "ssh"}:
        require(parsed.hostname and parsed.path and not parsed.query and not parsed.fragment
                and parsed.password is None and "%" not in remote
                and (parsed.username is None or parsed.scheme == "ssh" and parsed.username == "git"),
                "Credential-bearing or ambiguous remote rejected; use clean HTTPS or SSH git@host.")
        return
    require(re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", remote) is not None,
            "Unsupported or credential-bearing remote; use an absolute local path, HTTPS or SSH git@host.")


def git(directory, *args, input_data=None, timeout=None):
    # Isolate repository pointers and writes; retain normal helper-based authentication.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1"})
    command = ["git", "--no-replace-objects", "-C", str(directory),
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
               "-c", "commit.gpgSign=false",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
               "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
               "-c", "user.name=Workstream checkpoint", "-c", "user.email=checkpoint@localhost", *args]
    try:
        result = subprocess.run(command, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("Git source retrieval timed out; recovery remains unverified.") from None
    # Do not echo arbitrary remote diagnostics, which can contain authentication data.
    require(result.returncode == 0, f"git {args[0]} failed (exit {result.returncode}); "
            "check remote access/current branch state. No history rewrite or merge was attempted.")
    return result.stdout


def init_repo(path):
    path.mkdir()
    git(path, "init", "--quiet", "--template=")


def branch_ref(directory, branch):
    require(not branch.startswith("-"), "Invalid branch name.")
    ref = "refs/heads/" + branch
    git(directory, "check-ref-format", ref)
    return ref


def remote_head(directory, remote, ref):
    lines = git(directory, "ls-remote", "--refs", remote, ref).decode("utf-8").splitlines()
    require(len(lines) <= 1, "Ambiguous remote branch.")
    return lines[0].split()[0] if lines else None


def remote_snapshot(remote, branch, expected=None):
    safe_remote(remote)
    with tempfile.TemporaryDirectory(prefix="checkpoint-read-") as directory:
        repo = Path(directory) / "repo"
        init_repo(repo)
        ref = branch_ref(repo, branch)
        git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", remote, ref)
        commit = git(repo, "rev-parse", "FETCH_HEAD").decode().strip()
        require(expected is None or commit == expected, "Fresh remote read differs from the expected commit.")
        return commit, tree_files(repo, commit)


def tree_files(repo, commit):
    files, modes = {}, {}
    for row in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        header, key = row.split(b"\t", 1)
        mode, kind, oid = header.split()
        require(mode in {b"100644", b"100755"} and kind == b"blob", "Remote contains a symlink/submodule/special file.")
        path = key.decode("utf-8")
        relative(path)
        files[path] = git(repo, "cat-file", "blob", oid.decode("ascii"))
        modes[path] = mode == b"100755"
    check_stored_modes(files, modes)
    return files


def verify_files(files, expected_workstream_id=None):
    manifest, source, tracker = validate_snapshot(files)
    expected_identity(manifest, expected_workstream_id)
    with tempfile.TemporaryDirectory(prefix="checkpoint-verify-") as directory:
        target = Path(directory) / "active"
        active = active_view(source, manifest, target, tracker)
        write_files(target, active, manifest.get("executable"))
        validate_tracker(active, manifest["tracker"], target, tracker, check_sources=True)
    return manifest


def catalog_entries(content):
    data = json.loads(content)
    require(isinstance(data, dict) and data.get("schema") == CATALOG_SCHEMA,
            "Unsupported shared catalog schema.")
    entries = data.get("workstreams")
    require(isinstance(entries, list), "Catalog workstreams must be a list.")
    ids, branches, sources = set(), set(), set()
    for entry in entries:
        require(isinstance(entry, dict), "Invalid catalog entry.")
        for field in ("id", "project", "title", "source", "branch", "owner", "status"):
            require(isinstance(entry.get(field), str) and entry[field].strip(),
                    f"Catalog entry requires {field}.")
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)+", entry["id"]),
                "Workstream ID must be project/stable-workstream.")
        require(entry["id"].split("/", 1)[0] == entry["project"], "Workstream project differs from its ID.")
        require(entry["branch"].startswith("recovery/") and ".." not in entry["branch"],
                "Catalog branches must be dedicated recovery/ branches.")
        for value, seen, field in ((entry["id"], ids, "id"), (entry["branch"], branches, "branch"),
                                   (entry["source"], sources, "source")):
            require(value not in seen, f"Duplicate catalog {field}; reconcile the existing workstream.")
            seen.add(value)
    return entries


def catalog(remote, workstream_id=None):
    """Resolve the shared directory against current branch heads, without dispatching."""
    commit, files = remote_snapshot(remote, "main")
    require("registry.json" in files, "Shared repository has no registry.json on main.")
    entries = catalog_entries(files["registry.json"])
    with tempfile.TemporaryDirectory(prefix="checkpoint-catalog-") as directory:
        repo = Path(directory) / "repo"
        init_repo(repo)
        for entry in entries:
            branch_ref(repo, entry["branch"])
        lines = git(repo, "ls-remote", "--heads", remote).decode().splitlines()
    heads = {ref.removeprefix("refs/heads/"): sha for sha, ref in (line.split() for line in lines)}
    branches = {entry["branch"] for entry in entries}
    resolved = [dict(entry, checkpoint_commit=heads.get(entry["branch"]),
                     checkpoint_available=entry["branch"] in heads) for entry in entries]
    if workstream_id is not None:
        require(any(e["id"] == workstream_id for e in entries), "Unknown workstream ID; consult the full catalog before registering a new one.")
        resolved = [entry for entry in resolved if entry["id"] == workstream_id]
    return {"catalog_commit": commit, "workstreams": resolved,
            "unregistered_branches": sorted(b for b in heads if b.startswith("recovery/") and b not in branches),
            "note": "Branch heads are live observations, not recovery verification or an ownership lock."}


def publish(root, tracker_path, includes, remote, branch, expected, workstream_id=None, adoption=None):
    safe_remote(remote)
    require(expected == "absent" or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected),
            "--expected must be the full base SHA or 'absent'.")
    with tempfile.TemporaryDirectory(prefix="checkpoint-publish-") as directory:
        repo = Path(directory) / "repo"
        init_repo(repo)
        ref = branch_ref(repo, branch)
        base = remote_head(repo, remote, ref)
        require(base == (None if expected == "absent" else expected), "Stale base: remote branch differs from --expected; reread it before retrying.")
        prior = {}
        if base:
            git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", remote, ref)
            require(git(repo, "rev-parse", "FETCH_HEAD").decode().strip() == base,
                    "Remote changed while fetching; reread it before retrying.")
            top_level = git(repo, "ls-tree", "--name-only", base).decode().splitlines()
            require("registry.json" not in top_level,
                    "Refusing to overwrite the shared catalog branch; publish to the workstream's recovery/ branch.")
            prior = tree_files(repo, base)
        main_ref = "refs/heads/main"
        main_head = remote_head(repo, remote, main_ref)
        shared, selected = False, None
        if main_head:
            git(repo, "fetch", "--quiet", "--no-tags", "--depth=1", remote, main_ref)
            require(git(repo, "rev-parse", "FETCH_HEAD").decode().strip() == main_head,
                    "Shared registry changed while fetching; reread it before retrying.")
            main_files = tree_files(repo, main_head)
            shared = "registry.json" in main_files
            if shared:
                entries = catalog_entries(main_files["registry.json"])
                require(workstream_id is not None, "Shared repository publication requires --workstream-id; consult registry.json on main.")
                entry = next((e for e in entries if e["id"] == workstream_id), None)
                require(entry is not None, "Unknown workstream ID; consult registry.json on main.")
                require(entry["branch"] == branch, "Selected workstream requires its registered branch.")
                selected = workstream_identity({"id": entry["id"], "source": entry["source"]})
        require(workstream_id is None or shared, "--workstream-id requires registry.json on main.")
        require(adoption is None or selected is not None and adoption == selected["source"],
                "--adopt-workstream-source requires the selected registered canonical source.")
        files = make_snapshot(root, tracker_path, includes, selected, adoption)
        manifest = verify_files(files, workstream_id)
        if prior and (selected is not None or MANIFEST in prior):
            prior_manifest, prior_source, tracker = validate_snapshot(prior)
            if selected is not None:
                check_binding(prior_manifest, prior_source, tracker, selected, adoption, "prior branch checkpoint")
            else:
                require(prior_manifest.get("workstream") == manifest.get("workstream"),
                        "Prior branch workstream identity differs from the active checkpoint.")
        write_files(repo, files)
        # Hash raw bytes without filters, attributes, ignore rules or a checkout.
        index = []
        for path, payload in sorted(files.items()):
            oid = git(repo, "hash-object", "-w", "--stdin", input_data=payload).strip()
            mode = b"100755" if archive_modes(manifest).get(path, False) else b"100644"
            index.append(mode + b" " + oid + b"\t" + path.encode("utf-8") + b"\0")
        git(repo, "update-index", "-z", "--index-info", input_data=b"".join(index))
        tree = git(repo, "write-tree").decode().strip()
        parents = ["-p", base] if base else []
        commit = git(repo, "commit-tree", tree, *parents, input_data=b"Workstream checkpoint\n").decode().strip()
        require(remote_head(repo, remote, ref) == base, "Remote changed before push; reread it before retrying.")
        require(remote_head(repo, remote, main_ref) == main_head,
                "Shared registry/main changed before push; reread it before retrying.")
        # The commit was constructed directly on base. The exact lease adds an
        # atomic old-value guard even if the ref is deleted/rewound after lookup.
        try:
            git(repo, "push", "--porcelain", "--force-with-lease=" + ref + ":" + (base or ""),
                remote, commit + ":" + ref)
        except ValueError as error:
            raise ValueError(f"Push rejected or outcome uncertain for {commit}; verify the remote "
                             f"branch before retrying. {error}") from error
    try:
        confirmed, observed = remote_snapshot(remote, branch, expected=commit)
        verify_files(observed, workstream_id)
        require(observed == files, "Remote bytes differ from the published checkpoint.")
    except (ValueError, OSError) as error:
        raise ValueError(f"Push accepted ({commit}), but fresh remote confirmation failed: {error} Do not retry blindly.") from error
    return {"commit": confirmed, "remote": remote, "branch": branch,
            "remote_confirmed": True, "disaster_recovery_tested": False,
            "schema": SCHEMA, "managed_files": len(files) - 1,
            "workstream": manifest.get("workstream"), "limitations": limitations(manifest, files)}


def restore(files, destination, expected_workstream_id=None):
    # Complete integrity/path/dependency validation before creating the destination.
    manifest, source, tracker = validate_snapshot(files)
    expected_identity(manifest, expected_workstream_id)
    destination = absolute(str(destination))
    no_symlinks(destination)
    require(not destination.exists(), "Restore requires a NEW destination.")
    require(destination.parent.is_dir(), "Destination parent must already exist.")
    with tempfile.TemporaryDirectory(prefix="checkpoint-restore-", dir=destination.parent) as directory:
        stage = Path(directory) / "active"
        # Validate against the temporary path, then against the actual destination.
        active = active_view(source, manifest, stage, tracker)
        write_files(stage, active, manifest.get("executable"))
        validate_tracker(active, manifest["tracker"], stage, tracker, check_sources=True)
        final = active_view(source, manifest, destination, tracker)
        destination.mkdir()  # Exclusive creation; never overwrite an existing destination.
        try:
            write_files(destination, final, manifest.get("executable"))
            write_files(destination / ".checkpoint", files, archive_modes(manifest))
            validate_tracker(final, manifest["tracker"], destination, tracker, check_sources=True)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return {"restored": True, "tracker": str(destination / manifest["tracker"]),
            "exact_source_checkpoint": str(destination / ".checkpoint"),
            "active_schema_and_exports_checked": True, "disaster_recovery_tested": False,
            "workstream": manifest.get("workstream"), "limitations": limitations(manifest, files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publishing = commands.add_parser("publish", help="Append a checkpoint with an atomic expected-base lease")
    publishing.add_argument("--root", type=Path, required=True)
    publishing.add_argument("--tracker", default="tracker.html", help="Relative to --root")
    publishing.add_argument("--include", action="append", default=[], metavar="/ABS/PATH=relative",
                            help="External inputs go under dependencies/; internal inputs may retain their source-relative path")
    publishing.add_argument("--remote", required=True)
    publishing.add_argument("--branch", required=True)
    publishing.add_argument("--expected", required=True, help="Full current branch SHA, or absent")
    publishing.add_argument("--workstream-id", help="Required for a shared repository; resolves registry.json on main")
    publishing.add_argument("--adopt-workstream-source", help="Explicit canonical-source acknowledgement for unbound local-spec adoption")
    verifying = commands.add_parser("verify", help="Validate a local clone or perform a fresh remote read")
    origin = verifying.add_mutually_exclusive_group(required=True)
    origin.add_argument("--snapshot", type=Path)
    origin.add_argument("--remote")
    verifying.add_argument("--branch")
    verifying.add_argument("--expected", help="Optional exact remote SHA")
    verifying.add_argument("--expected-workstream-id", help="Require a bound manifest with this stable ID")
    restoring = commands.add_parser("restore", help="Restore an exact source checkpoint plus a relocated active view")
    restoring.add_argument("--snapshot", type=Path, required=True)
    restoring.add_argument("--destination", type=Path, required=True)
    restoring.add_argument("--expected-workstream-id", help="Check the binding before destination creation")
    directory = commands.add_parser("catalog", help="Find registered workstreams and their live checkpoint heads")
    directory.add_argument("--remote", required=True)
    directory.add_argument("--id", help="Optional stable project/workstream ID")
    args = parser.parse_args(argv)
    if args.command == "publish":
        result = publish(args.root, args.tracker, args.include, args.remote, args.branch, args.expected,
                         args.workstream_id, args.adopt_workstream_source)
    elif args.command == "restore":
        result = restore(local_snapshot(args.snapshot), args.destination, args.expected_workstream_id)
    elif args.command == "catalog":
        result = catalog(args.remote, args.id)
    else:
        if args.remote:
            require(args.branch is not None, "Remote verify requires --branch.")
            commit, files = remote_snapshot(args.remote, args.branch, args.expected)
        else:
            require(args.branch is None and args.expected is None, "--branch/--expected require --remote.")
            commit, files = None, local_snapshot(args.snapshot)
        manifest = verify_files(files, args.expected_workstream_id)
        result = {"verified": True, "schema": manifest["schema"], "commit": commit,
                  "remote_confirmed": bool(args.remote), "disaster_recovery_tested": False,
                  "workstream": manifest.get("workstream"), "limitations": limitations(manifest, files)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
        raise SystemExit(str(error))
