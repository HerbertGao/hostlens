#!/usr/bin/env python3
"""Lint Hostlens cassette files for secrets and schema drift.

Two modes, mutually exclusive:

1. Scan (default): walk every ``tests/fixtures/cassettes/*.jsonl`` file,
   validate each record's ``response`` field against ``MessageResponse``,
   and reject any line whose raw string matches an extended set of
   sensitive-data patterns (Anthropic / generic ``sk-`` keys, Bearer
   tokens, JWTs, ``password=`` / ``api_key=`` assignments, absolute home
   paths, ``.ssh`` paths, IPv4 addresses, hostname-like FQDNs, email
   addresses). Sensitive-substring hits → ``exit 1`` with a ``stderr``
   message naming the matched pattern. Schema validation failure →
   ``exit 1``. Clean → ``exit 0``.

2. ``--check-schema-drift --current-tools-hash <hex>``: cross-check the
   optional ``tools_schema_hash`` field on each cassette record against
   the supplied current hash. Drift produces a ``stdout`` warning but
   never sets a non-zero exit. The flag REQUIRES
   ``--current-tools-hash``; omitting it → ``exit 2`` with a stderr
   error (the spec rejects silent skips).

The lint is intentionally a standalone script: it imports only the
narrow ``hostlens.core.redact`` and ``hostlens.agent.backend`` symbols
needed to validate cassettes, and refuses to import the wider
``hostlens.tools`` / ``hostlens.agent.backends`` packages so a CI job can
run it without provisioning the full Agent runtime. The tools-schema
hash for ``--check-schema-drift`` is computed externally and injected
via ``--current-tools-hash``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

from hostlens.agent.backend import MessageResponse
from hostlens.core.redact import redact_text

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASSETTE_DIR = REPO_ROOT / "tests" / "fixtures" / "cassettes"


# Patterns checked in scan mode. Each tuple is ``(name, compiled_regex)``;
# ``name`` is what gets reported on a hit so a reviewer can identify the
# rule that fired without re-scanning the file. The set is intentionally
# broader than ``hostlens.core.redact`` so cassettes (which a reviewer
# will commit to git) are held to a higher standard than runtime log
# output (where the redact module only needs to scrub the most obvious
# leaks).
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # API-key prefixes (Anthropic / OpenAI ``sk-...``).
    ("anthropic_or_openai_sk_key", re.compile(r"sk-[A-Za-z0-9_-]{6,}")),
    # ``Authorization: Bearer <token>``.
    ("bearer_token", re.compile(r"(?i)\bBearer\s+\S+")),
    # Three-segment JWT (header.payload.signature).
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")),
    # ``password=...`` / ``api_key=...`` / ``token=...`` / ``secret=...``
    # assignment forms. The trailing value must be at least two chars to
    # avoid matching empty-value JSON-encoded ``"password":""`` keys that
    # cassettes occasionally need to express literal empty-string fields.
    (
        "credential_assignment",
        re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*\S{2,}"),
    ),
    # User home directories — macOS ``/Users/<name>`` and Linux ``/home/<name>``.
    ("user_home_path", re.compile(r"/(Users|home)/[A-Za-z0-9._-]+")),
    # ``.ssh`` directories, anywhere in the path.
    ("ssh_path", re.compile(r"\.ssh(/|\\)")),
    # IPv4 literals (not 0.0.0.0 / 127.0.0.1 ish — block both private and
    # public; cassettes have no business holding any specific IP).
    ("ipv4_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Email addresses.
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Hostnames / FQDNs with at least one label + a common TLD or environment
    # suffix. Catches strings like ``prod-db.internal.example.com`` or
    # ``auth.corp.local`` that may leak via inspector output into cassettes.
    # The suffix set is narrow on purpose: a generic ``\.[a-z]{2,}`` would
    # collide with model IDs ("claude-opus-4-7"), tool names, and other
    # legitimate dotted tokens we expect inside cassette bodies.
    (
        "hostname_or_fqdn",
        re.compile(
            r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"(?:internal|intranet|local|lan|corp|company|enterprise|prod|production"
            r"|staging|dev|test|example|home|office|com|net|org|io|app|cloud|tech)\b",
            re.IGNORECASE,
        ),
    ),
)


class LintError(Exception):
    """A cassette failed scan-mode validation.

    Carries enough context (file, line, reason) for the script's top-level
    handler to render a single human-readable message on stderr.
    """

    def __init__(self, *, path: Path, line_no: int, reason: str) -> None:
        super().__init__(f"{path}:{line_no}: {reason}")
        self.path = path
        self.line_no = line_no
        self.reason = reason


def iter_cassette_files(directory: Path) -> Iterator[Path]:
    """Yield every ``*.jsonl`` file under ``directory`` in sorted order."""

    if not directory.is_dir():
        return
    yield from sorted(directory.glob("*.jsonl"))


def scan_line_for_sensitive_substrings(line: str) -> str | None:
    """Return the name of the first sensitive pattern matched in ``line``.

    Uses both the curated ``SENSITIVE_PATTERNS`` set above and the
    ``hostlens.core.redact.redact_text`` baseline: if ``redact_text``
    rewrites the line at all, the line contained a secret per the runtime
    redaction rules. ``SENSITIVE_PATTERNS`` extends that with cassette-
    specific rules (paths / IPv4 / email) that ``redact_text`` does not
    cover.
    """

    for name, pattern in SENSITIVE_PATTERNS:
        if pattern.search(line):
            return name
    if redact_text(line) != line:
        return "redact_text_baseline"
    return None


def validate_record_schema(record: dict[str, object], *, path: Path, line_no: int) -> None:
    """Raise ``LintError`` if ``record["response"]`` is not a valid
    ``MessageResponse`` shape.

    The validator deliberately accepts records missing ``request`` /
    ``response`` keys and reports them with named reasons so a malformed
    cassette never silently passes lint.
    """

    if "response" not in record:
        raise LintError(path=path, line_no=line_no, reason="record missing 'response' key")
    try:
        MessageResponse.model_validate(record["response"])
    except Exception as exc:
        raise LintError(
            path=path,
            line_no=line_no,
            reason=f"response failed MessageResponse validation: {type(exc).__name__}",
        ) from exc


def scan_cassette_file(path: Path) -> None:
    """Run scan-mode checks on a single cassette file.

    Raises ``LintError`` on the first failing line so the script aborts
    early — running through all lines after a failure would just add
    noise.
    """

    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            hit = scan_line_for_sensitive_substrings(line)
            if hit is not None:
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"sensitive substring detected: {hit}",
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"invalid JSON: {exc.msg}",
                ) from exc
            if not isinstance(record, dict):
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"record not a JSON object (got {type(record).__name__})",
                )
            validate_record_schema(record, path=path, line_no=line_no)


def check_schema_drift(paths: Iterable[Path], *, current_hash: str) -> None:
    """Compare every cassette's ``tools_schema_hash`` against ``current_hash``.

    Drift produces a stdout warning naming the cassette, the stored hash,
    and the current hash. The function returns without raising on drift —
    schema drift is a soft signal for a reviewer to consider re-recording
    a cassette, not a CI-blocking failure.
    """

    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Skip — scan mode reports this as a hard error
                    # separately; here we only care about drift.
                    continue
                if not isinstance(record, dict):
                    continue
                cassette_hash = record.get("tools_schema_hash")
                if cassette_hash is None:
                    continue
                if cassette_hash != current_hash:
                    print(
                        f"WARNING: tools_schema_hash drift in cassette {path}:{line_no}: "
                        f"cassette={cassette_hash} current={current_hash}"
                    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    Defined as a function so the unit tests can exercise parsing logic
    without invoking ``main`` itself (which has side effects on argv /
    exit codes).
    """

    parser = argparse.ArgumentParser(
        description="Lint Hostlens cassettes for secrets and tools schema drift.",
    )
    parser.add_argument(
        "--cassette-dir",
        type=Path,
        default=DEFAULT_CASSETTE_DIR,
        help="Directory containing *.jsonl cassette files.",
    )
    parser.add_argument(
        "--check-schema-drift",
        action="store_true",
        help="Switch to drift-check mode; requires --current-tools-hash.",
    )
    parser.add_argument(
        "--current-tools-hash",
        type=str,
        default=None,
        help="SHA-256 hex of the current registered tools schema (drift mode only).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code (0 / 1 / 2)."""

    parser = build_argument_parser()
    args = parser.parse_args(argv)

    cassette_dir: Path = args.cassette_dir
    files = list(iter_cassette_files(cassette_dir))

    if args.check_schema_drift:
        if args.current_tools_hash is None:
            print(
                "error: --current-tools-hash required when using --check-schema-drift",
                file=sys.stderr,
            )
            return 2
        check_schema_drift(files, current_hash=args.current_tools_hash)
        return 0

    # Scan mode.
    try:
        for path in files:
            scan_cassette_file(path)
    except LintError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
