#!/usr/bin/env python3
"""Exclusive ticket claims in one Git authority; stdlib + git, no dispatch.

Every success is a newly leased update confirmed by a fresh fetch. Errors never
grant a send permit, even when the remote may have accepted an interrupted push.
The authority must retain its append-only refs: a stateless new client cannot
detect an administrator deleting or rewinding history before its first read.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit
import uuid


MAX_RECORD_BYTES = 8192
MAX_HISTORY = 1024
GIT_TIMEOUT = 30
AUTHOR = "Workstream checkpoint <checkpoint@localhost>"
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, label, limit=256):
    _require(isinstance(value, str) and value.strip() and len(value) <= limit
             and not any(ord(c) < 32 or ord(c) == 127 for c in value),
             f"Invalid {label}.")
    return value


def _ticket(ticket):
    _text(ticket, "ticket URL", 2048)
    _require(not any(c.isspace() for c in ticket) and "?" not in ticket and "#" not in ticket,
             "Ticket must be a canonical URL without query or fragment.")
    try:
        parsed = urlsplit(ticket)
        port = parsed.port
        _require(parsed.scheme.lower() in {"https", "http"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.path.startswith("/") and parsed.path != "/"
                 and "%" not in parsed.netloc and "\\" not in ticket,
                 "Invalid or credential-bearing ticket URL.")
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        authority = host + (f":{port}" if port is not None else "")
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path, "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid or credential-bearing ticket URL.") from None


def _remote(remote):
    _text(remote, "claim remote", 4096)
    if remote.startswith("/"):
        _require("?" not in remote and "#" not in remote, "Invalid local claim remote.")
        return remote
    _require(not any(c.isspace() for c in remote) and "?" not in remote
             and "#" not in remote and "%" not in remote and "\\" not in remote,
             "Use a clean HTTPS/SSH remote or absolute local bare repository path.")
    try:
        parsed = urlsplit(remote)
        _ = parsed.port
        if parsed.scheme in {"https", "ssh"}:
            _require(parsed.hostname and parsed.path and parsed.password is None
                     and (parsed.username is None or parsed.scheme == "ssh" and parsed.username == "git"),
                     "Credential-bearing or ambiguous claim remote rejected.")
            return remote
    except (ValueError, UnicodeError):
        raise ValueError("Invalid claim remote.") from None
    _require(re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", remote),
             "Use a clean HTTPS/SSH remote or absolute local bare repository path.")
    return remote


def _identity(ticket):
    ticket = _ticket(ticket)
    key = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    return ticket, key, "refs/heads/claims/" + key


def _git(repo, *args, input_data=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1"})
    command = ["git", "--no-replace-objects", "-C", str(repo),
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
               "-c", "commit.gpgSign=false", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=always",
               "-c", "protocol.ssh.allow=always", "-c", "user.name=Workstream checkpoint",
               "-c", "user.email=checkpoint@localhost", *args]
    try:
        result = subprocess.run(command, input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Claim Git transport failed or timed out; reconcile before dispatch.") from None
    _require(result.returncode == 0, "Claim Git operation failed; reconcile before dispatch.")
    return result.stdout


def _init(repo):
    _git(repo, "init", "--quiet", "--template=", "--object-format=sha1")


def _head(repo, remote, ref):
    try:
        rows = _git(repo, "ls-remote", "--refs", remote, ref).decode("ascii").splitlines()
        _require(len(rows) <= 1, "Ambiguous claim ref.")
        if not rows:
            return None
        oid, observed_ref = rows[0].split("\t")
        _require(OID.fullmatch(oid) and observed_ref == ref, "Invalid claim ref observation.")
        return oid
    except (UnicodeError, ValueError):
        raise ValueError("Claim ref could not be observed reliably.") from None


def _proof(proof):
    _require(isinstance(proof, dict) and set(proof) == {"outcome", "source"}
             and isinstance(proof["outcome"], str) and proof["outcome"] in {"not_delivered", "terminal"},
             "Release requires a conclusive not_delivered or terminal proof.")
    _text(proof["source"], "release observation locator", 2048)
    return dict(proof)


def _record(value, ticket, key):
    fields = {"ticket", "key", "request_id", "owner", "payload_sha256", "phase"}
    _require(isinstance(value, dict) and value.get("phase") in {"held", "released"},
             "Invalid claim record phase.")
    _require(set(value) == fields | ({"proof"} if value["phase"] == "released" else set()),
             "Invalid claim record fields.")
    _require(value["ticket"] == ticket and value["key"] == key, "Claim ticket identity changed.")
    _text(value["request_id"], "claim request ID")
    _text(value["owner"], "claim owner")
    _require(isinstance(value["payload_sha256"], str) and SHA256.fullmatch(value["payload_sha256"]),
             "Invalid claim payload digest.")
    if value["phase"] == "released":
        _proof(value["proof"])
    return value


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate claim record key.")
        result[key] = value
    return result


def _history(repo, head, ticket, key):
    """Validate a bounded linear chain, including all release identity bindings."""
    try:
        rows = _git(repo, "rev-list", "--parents", f"--max-count={MAX_HISTORY + 1}", head).decode("ascii").splitlines()
        _require(0 < len(rows) <= MAX_HISTORY, "Claim history exceeds validation bound.")
        previous = None
        seen_requests = set()
        latest = None
        for row in reversed(rows):
            parts = row.split()
            _require(len(parts) in {1, 2} and all(OID.fullmatch(p) for p in parts),
                     "Claim history must be linear.")
            commit = parts[0]
            _require(parts[1:] == ([] if previous is None else [previous[0]]),
                     "Claim history is incomplete or changed.")
            metadata = _git(repo, "show", "-s", "--format=%an <%ae>%n%cn <%ce>", commit).decode("utf-8").splitlines()
            _require(metadata == [AUTHOR, AUTHOR], "Claim automation identity changed.")
            tree = _git(repo, "ls-tree", "-z", commit).split(b"\0")
            _require(len(tree) == 2 and tree[-1] == b"", "Invalid claim tree.")
            metadata, name = tree[0].split(b"\t")
            mode, kind, blob = metadata.split()
            _require(mode == b"100644" and kind == b"blob" and name == b"claim.json"
                     and OID.fullmatch(blob.decode("ascii")), "Invalid claim file.")
            size = int(_git(repo, "cat-file", "-s", blob.decode("ascii")))
            _require(0 < size <= MAX_RECORD_BYTES, "Claim record exceeds size bound.")
            value = _record(json.loads(_git(repo, "cat-file", "blob", blob.decode("ascii")),
                                       object_pairs_hook=_json_pairs), ticket, key)
            if previous is None:
                _require(value["phase"] == "held", "Claim history must start held.")
            elif value["phase"] == "released":
                prior = previous[1]
                _require(prior["phase"] == "held"
                         and all(value[f] == prior[f] for f in ("ticket", "key", "request_id", "owner", "payload_sha256")),
                         "Release changed the held claim identity.")
            else:
                _require(previous[1]["phase"] == "released", "Held claim was overwritten without release.")
            if value["phase"] == "held":
                _require(value["request_id"] not in seen_requests, "Claim request ID was reused.")
                seen_requests.add(value["request_id"])
            previous = (commit, value)
            latest = value
        _require(previous[0] == head, "Claim head differs from validated history.")
        return latest, seen_requests
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError("Invalid claim history or record.") from None


def _snapshot(repo, remote, ticket, key, ref):
    if remote.startswith("/"):
        _require(_git(repo, "--git-dir=" + remote, "rev-parse", "--is-bare-repository").strip() == b"true",
                 "Local claim authority must be a bare Git repository.")
    head = _head(repo, remote, ref)
    if head is None:
        _require(_head(repo, remote, ref) is None, "Claim ref changed during observation.")
        return None, None, set()
    _git(repo, "fetch", "--quiet", "--no-tags", remote, ref)
    fetched = _git(repo, "rev-parse", "FETCH_HEAD").decode("ascii").strip()
    _require(fetched == head, "Claim ref changed during fetch.")
    value, requests = _history(repo, head, ticket, key)
    _require(_head(repo, remote, ref) == head, "Claim ref changed or disappeared during validation.")
    return head, value, requests


def _commit(repo, value, parent):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    _require(len(data) <= MAX_RECORD_BYTES, "Claim record exceeds size bound.")
    blob = _git(repo, "hash-object", "-w", "--stdin", input_data=data).decode("ascii").strip()
    tree = _git(repo, "mktree", input_data=f"100644 blob {blob}\tclaim.json\n".encode("ascii")).decode("ascii").strip()
    # Without a nonce, simultaneous identical attempts can produce the same OID;
    # Git then reports 'up to date' without enforcing an absent-value lease.
    message = f"Ticket claim {value['phase']}\n\nAttempt: {uuid.uuid4().hex}\n".encode("ascii")
    return _git(repo, "commit-tree", tree, *(["-p", parent] if parent else []), input_data=message).decode("ascii").strip()


def _push(repo, remote, ref, parent, commit):
    try:
        output = _git(repo, "push", "--porcelain", f"--force-with-lease={ref}:{parent or ''}",
                      remote, f"{commit}:{ref}")
        rows = output.decode("utf-8").splitlines()
        _require(any(row.startswith(("*\t", " \t", "+\t")) and row.split("\t")[1] == f"{commit}:{ref}"
                     for row in rows if row.count("\t") >= 2), "Claim push did not confirm a new ref update.")
    except (ValueError, UnicodeError):
        raise ValueError("Claim push conflicted or its outcome is unknown; inspect and reconcile before dispatch.") from None


def _publish(repo, remote, ref, parent, value):
    commit = _commit(repo, value, parent)
    _push(repo, remote, ref, parent, commit)
    try:
        observed = inspect(remote, value["ticket"])
        _require(observed is not None and observed["claim_commit"] == commit
                 and {k: v for k, v in observed.items() if k not in {"claim_commit", "ref"}} == value,
                 "Remote claim differs from intended update.")
    except ValueError:
        raise ValueError("Claim push completed but fresh confirmation failed; inspect and reconcile before dispatch.") from None
    return observed


def acquire(remote, ticket, request_id, owner, payload_sha256):
    """Return one newly confirmed held claim; every existing held claim blocks."""
    remote = _remote(remote)
    ticket, key, ref = _identity(ticket)
    value = _record({"ticket": ticket, "key": key, "request_id": request_id, "owner": owner,
                     "payload_sha256": payload_sha256, "phase": "held"}, ticket, key)
    with tempfile.TemporaryDirectory(prefix="ticket-claim-acquire-") as directory:
        repo = Path(directory)
        _init(repo)
        parent, prior, requests = _snapshot(repo, remote, ticket, key, ref)
        _require(prior is None or prior["phase"] == "released", "Ticket already has a held claim; reconcile before dispatch.")
        _require(request_id not in requests, "A released request ID cannot acquire another send permit.")
        return _publish(repo, remote, ref, parent, value)


def inspect(remote, ticket):
    """Freshly fetch and validate a held/released claim, or return None if absent."""
    remote = _remote(remote)
    ticket, key, ref = _identity(ticket)
    with tempfile.TemporaryDirectory(prefix="ticket-claim-inspect-") as directory:
        repo = Path(directory)
        _init(repo)
        commit, value, _ = _snapshot(repo, remote, ticket, key, ref)
        return None if value is None else dict(value, claim_commit=commit, ref=ref)


def release(remote, ticket, request_id, claim_commit, proof):
    """Release exactly the observed owner/request/commit using conclusive proof.

    The API's request ID and immutable held commit bind its owner; there is no
    separate caller identity or authentication assertion in this helper.
    """
    remote = _remote(remote)
    ticket, key, ref = _identity(ticket)
    _text(request_id, "release request ID")
    _require(isinstance(claim_commit, str) and OID.fullmatch(claim_commit), "Invalid held claim commit.")
    proof = _proof(proof)
    with tempfile.TemporaryDirectory(prefix="ticket-claim-release-") as directory:
        repo = Path(directory)
        _init(repo)
        parent, prior, _ = _snapshot(repo, remote, ticket, key, ref)
        _require(parent == claim_commit and prior is not None and prior["phase"] == "held"
                 and prior["request_id"] == request_id, "Release request/owner or held commit does not match.")
        return _publish(repo, remote, ref, parent, dict(prior, phase="released", proof=proof))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("acquire", "inspect", "release"):
        sub = commands.add_parser(command)
        sub.add_argument("--remote", required=True)
        sub.add_argument("--ticket", required=True)
        if command != "inspect":
            sub.add_argument("--request-id", required=True)
        if command == "acquire":
            sub.add_argument("--owner", required=True)
            sub.add_argument("--payload-sha256", required=True)
        if command == "release":
            sub.add_argument("--claim-commit", required=True)
            sub.add_argument("--proof", required=True, help='JSON: {"outcome":"terminal|not_delivered","source":"observation locator"}')
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        if command == "release":
            args["proof"] = json.loads(args["proof"], object_pairs_hook=_json_pairs)
        result = {"acquire": acquire, "inspect": inspect, "release": release}[command](**args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError):
        # Inputs and remote diagnostics may contain credentials; keep CLI errors finite.
        parser.exit(1, "Claim operation blocked or uncertain; inspect and reconcile before dispatch.\n")


if __name__ == "__main__":
    raise SystemExit(main())
