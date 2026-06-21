"""``ImportPlan`` — the four-bucket, serialisable, redaction-safe import preview.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`ImportPlan` 必须四分类、可序列化 round-trip、渲染禁泄露.

``ImportPlan`` is the last **read-only** artefact before a write. It sorts
candidates into four named buckets (each element is a named Pydantic model,
not a bare tuple — tuples deserialise positionally in Pydantic v2 and clash
with the ``TargetEntry`` discriminated union):

- ``to_add``      → ``PendingAdd``      (probe OK + name free; entry + env refs)
- ``skipped``     → ``str``             (name already in ``targets.yaml``)
- ``failed_probe``→ ``FailedProbe``     (promoted but unreachable)
- ``invalid_candidate`` → ``InvalidCandidate`` (promotion failed; redacted summary)

The whole model is pure Pydantic so it ``model_dump_json`` /
``model_validate_json`` round-trips (for dry-run artefact persistence and
proposal B's ``--from-plan`` reuse). Rendering (diff + ``--json``) for the
``failed_probe`` / ``invalid_candidate`` buckets emits only ``error_kind`` +
candidate name — never a raw host / ``user@host`` / traceback / fingerprint
value. ``to_add`` deliberately lists every connection address so an operator
can audit for unexpected hosts before passing ``--yes``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.targets.config import LocalEntry, SSHEntry, _atomic_write_yaml
from hostlens.targets.inventory.models import (
    CandidateTarget,
    contains_unsafe_display_chars,
)
from hostlens.targets.probe import ProbeResult

# Mirror of ``targets/config.py:_PLACEHOLDER_PATTERN``'s inner var name and
# ``inventory/models.py:_ENV_VAR_NAME_PATTERN``: a credential ``*_env`` ref is a
# **bare** env var name (``MY_PASS``), never the ``${VAR}`` placeholder form. The
# ``${...}`` wrapper is synthesised by ``_entry_to_dict`` at save time from this
# bare name; checking for ``${VAR}`` here would reject every plan ``build_import_plan``
# legitimately produces.
_ENV_VAR_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z_][A-Z0-9_]*$")

__all__ = [
    "FailedProbe",
    "ImportPlan",
    "InvalidCandidate",
    "PendingAdd",
]


class PendingAdd(BaseModel):
    """A candidate that probed reachable and whose name is free to add.

    ``entry.password`` / ``passphrase`` are always ``None`` (credentials are
    env references); the ``password_env`` / ``passphrase_env`` names are
    threaded separately so ``save_targets_config`` re-derives the ``${VAR}``
    placeholder from the env name (never from an inlined entry field).
    """

    model_config = ConfigDict(extra="forbid")

    entry: LocalEntry | SSHEntry
    password_env: str | None = None
    passphrase_env: str | None = None
    # The source's original identifier (ssh_config Host alias / yaml dict-key)
    # before normalization, so the plan can surface the required
    # ``原始标识 → 派生 name`` mapping when it differs from ``entry.name``.
    raw_identifier: str | None = None


class FailedProbe(BaseModel):
    """A promoted entry whose probe failed (unreachable / auth / timeout).

    Carries the same ``password_env`` / ``passphrase_env`` refs as
    ``PendingAdd``: with ``--include-unreachable`` a failed entry is still
    written (``enabled=False``), and its ``${VAR}`` credential placeholder
    must be preserved so re-enabling the host later does not lose its auth.
    """

    model_config = ConfigDict(extra="forbid")

    entry: LocalEntry | SSHEntry
    result: ProbeResult
    password_env: str | None = None
    passphrase_env: str | None = None


class InvalidCandidate(BaseModel):
    """A candidate that failed promotion to a ``TargetEntry``.

    ``error_summary`` is a redacted scalar — a short ``ValidationError``
    digest with field names only, never host / credential values. Rendering
    surfaces only this summary + the candidate name.
    """

    model_config = ConfigDict(extra="forbid")

    candidate: CandidateTarget
    error_summary: str


class ImportPlan(BaseModel):
    """The four-bucket import preview — pure Pydantic, round-trippable.

    ``version`` is fixed to the string ``"1"`` (mirrors ``TargetsConfig.version``).
    The plan graduated from an in-process dry-run artefact into a cross-process
    ``propose → land`` contract (MCP ``propose_target_import`` may emit it on one
    host, ``target import --from-plan`` lands it on another, possibly across a
    Hostlens upgrade), so it needs a version field to refuse an opaque
    cross-version load. A ``.save`` artefact written before this field existed has
    no ``version`` key and loads as ``"1"`` via the default (explicit
    backward-compat); any other value fails validation.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["1"] = "1"
    to_add: list[PendingAdd] = []
    skipped: list[str] = []
    failed_probe: list[FailedProbe] = []
    invalid_candidate: list[InvalidCandidate] = []

    @property
    def is_empty(self) -> bool:
        """True when every bucket is empty (e.g. empty inventory → empty plan)."""

        return not (self.to_add or self.skipped or self.failed_probe or self.invalid_candidate)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_diff(self) -> str:
        """Render a human-readable, redaction-safe diff of the plan.

        - ``to_add`` lists each entry's connection address (the pre-write
          audit point so an operator spots an unexpected host).
        - ``skipped`` lists names only.
        - ``failed_probe`` / ``invalid_candidate`` surface only
          ``error_kind`` / ``error_summary`` + name — never a raw host,
          ``user@host``, traceback, or fingerprint value.
        """

        if self.is_empty:
            return "nothing to import"

        lines: list[str] = []

        if self.to_add:
            lines.append(f"to_add ({len(self.to_add)}):")
            for item in self.to_add:
                lines.append(f"  + {_pending_add_label(item)}")
        if self.skipped:
            lines.append(f"skipped ({len(self.skipped)}):")
            for name in self.skipped:
                lines.append(f"  = {name} (already in targets.yaml)")
        if self.failed_probe:
            lines.append(f"failed_probe ({len(self.failed_probe)}):")
            for failed in self.failed_probe:
                kind = failed.result.error_kind or "unknown"
                lines.append(f"  ! {failed.entry.name} ({kind})")
        if self.invalid_candidate:
            lines.append(f"invalid_candidate ({len(self.invalid_candidate)}):")
            for invalid in self.invalid_candidate:
                lines.append(f"  x {invalid.candidate.name} ({invalid.error_summary})")

        return "\n".join(lines)

    def to_json_obj(self) -> dict[str, Any]:
        """Return a redaction-safe ``--json`` object for stdout.

        ``to_add`` includes the connection address (operator audit need);
        ``failed_probe`` / ``invalid_candidate`` carry only the name +
        ``error_kind`` / ``error_summary`` (no host / traceback / fingerprint
        value). Capabilities (non-sensitive) are included for ``failed_probe``
        but the fingerprint dict is dropped from the JSON surface to avoid
        leaking a smuggled value.
        """

        return {
            "to_add": [
                {
                    "name": item.entry.name,
                    "raw_identifier": (
                        _strip_control_chars(item.raw_identifier)
                        if item.raw_identifier is not None
                        else None
                    ),
                    "type": item.entry.type,
                    "host": item.entry.host if isinstance(item.entry, SSHEntry) else None,
                    "password_env": item.password_env,
                    "passphrase_env": item.passphrase_env,
                }
                for item in self.to_add
            ],
            "skipped": list(self.skipped),
            "failed_probe": [
                {"name": failed.entry.name, "error_kind": failed.result.error_kind}
                for failed in self.failed_probe
            ],
            "invalid_candidate": [
                {"name": invalid.candidate.name, "error_summary": invalid.error_summary}
                for invalid in self.invalid_candidate
            ],
        }

    def render_json(self) -> str:
        """Serialise ``to_json_obj`` to a stable, sorted JSON string."""

        return json.dumps(self.to_json_obj(), indent=2, sort_keys=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist the full plan (incl. ``to_add`` hosts) to ``path`` at ``0o600``.

        The serialised plan carries ``to_add``'s plaintext host (a
        lateral-movement map), so it reuses ``save_targets_config``'s atomic
        ``0o600`` write discipline (``_atomic_write_yaml``) — it must never be
        world-readable. This is the persistence path for dry-run artefacts /
        proposal B's ``--from-plan``; the ``--json`` stdout surface (operator
        audit) is separate and may show hosts.
        """

        raw = json.loads(self.model_dump_json())
        _atomic_write_yaml(path, raw)

    @classmethod
    def load(cls, path: Path) -> ImportPlan:
        """Load + re-validate a serialised ``ImportPlan`` from ``path`` (trust boundary).

        ``yaml.safe_load`` → ``model_validate``: because JSON is a subset of YAML,
        one loader accepts both the YAML ``.save`` writes and the JSON an MCP
        client produces from ``model_dump()``. A missing ``version`` key (an old
        ``.save`` artefact) loads as ``"1"`` via the field default; any other
        value fails validation.

        This is a **trust boundary**: ``--from-plan`` skips proposal A's
        ``promote_candidate`` and lands the file's entries straight through
        ``assemble_save_entries`` → ``save_targets_config`` (which re-derives the
        ``${VAR}`` placeholder but does NOT re-check the entry), so a tampered /
        hand-edited / cross-version plan could otherwise write a disabled target,
        an unexpandable ``${illegal}`` placeholder, or a control-char host into
        ``targets.yaml``. Every entry that can reach ``save_targets_config`` —
        ``to_add`` always, ``failed_probe`` under ``--include-unreachable`` — is
        re-checked against ``promote_candidate``'s invariants (see
        ``_assert_save_invariants``). ``skipped`` (``list[str]``) and
        ``invalid_candidate`` never project to a save entry, so they are
        deliberately exempt.

        All failure modes (unreadable file / malformed YAML / schema or version
        mismatch / invariant violation) are normalised to ``ConfigError`` — the
        project's exit-2 carrier — so the ``--from-plan`` branch never leaks a
        bare ``ValidationError`` / ``YAMLError`` / ``OSError`` traceback.
        """

        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                "failed to read import plan file (not readable / not UTF-8)",
                kind="import_plan_read_error",
                original=exc,
                path=str(path),
            ) from exc

        try:
            parsed = yaml.safe_load(raw_text)
        except (yaml.YAMLError, ValueError) as exc:
            raise ConfigError(
                "failed to parse import plan file",
                kind="import_plan_parse_error",
                original=exc,
                path=str(path),
            ) from exc

        try:
            plan = cls.model_validate(parsed)
        except ValidationError as exc:
            raise ConfigError(
                "import plan file does not match the ImportPlan schema",
                kind="import_plan_invalid",
                original=exc,
                path=str(path),
            ) from exc

        plan._assert_save_invariants()
        return plan

    def _assert_save_invariants(self) -> None:
        """Re-assert ``promote_candidate``'s invariants on every save-bound entry.

        Applies to ``to_add`` (always lands) and ``failed_probe`` (lands as
        ``enabled=False`` under ``--include-unreachable``). The check is run
        unconditionally — ``--include-unreachable`` is a CLI decision the loader
        cannot see, so a malformed ``failed_probe`` entry is rejected regardless
        rather than slipping through when the flag is later set. Raises
        ``ConfigError(kind="import_plan_invalid")`` on the first violation.
        """

        for item in self.to_add:
            _assert_entry_invariants(item.entry, require_enabled=True)
            _assert_env_name_invariants(item.password_env, item.passphrase_env)
        for failed in self.failed_probe:
            # ``assemble_save_entries`` forces ``enabled=False`` on a failed
            # entry, so we do not require ``enabled is True`` here.
            _assert_entry_invariants(failed.entry, require_enabled=False)
            _assert_env_name_invariants(failed.password_env, failed.passphrase_env)


def _assert_entry_invariants(entry: LocalEntry | SSHEntry, *, require_enabled: bool) -> None:
    """Re-check one promoted entry against ``promote_candidate``'s guarantees.

    - ``password`` / ``passphrase`` must be ``None`` — credentials travel only as
      ``*_env`` references (defence-in-depth: ``_entry_to_dict`` writes a password
      only from the ``password_env`` param, never from ``entry.password``, so this
      guards contract integrity rather than plugging a leak).
    - ``host`` / ``user`` / ``key_path`` (``SSHEntry`` only — ``LocalEntry`` has
      none of these) must not carry control / bidi / line-separator characters,
      mirroring ``promote_candidate``'s SSH branch via the exported
      ``contains_unsafe_display_chars``.
    - ``enabled`` must be ``True`` when ``require_enabled`` (the ``to_add`` bucket).
    """

    if require_enabled and entry.enabled is not True:
        raise ConfigError(
            "import plan to_add entry is not enabled",
            kind="import_plan_invalid",
            target=entry.name,
        )

    if isinstance(entry, SSHEntry):
        if entry.password is not None or entry.passphrase is not None:
            raise ConfigError(
                "import plan entry inlines a plaintext credential",
                kind="import_plan_invalid",
                target=entry.name,
            )
        for field in (entry.host, entry.user, entry.key_path):
            if field is not None and contains_unsafe_display_chars(field):
                raise ConfigError(
                    "import plan entry has a control / bidirectional character in "
                    "host / user / key_path",
                    kind="import_plan_invalid",
                    target=entry.name,
                )
        # ``key_path`` lands verbatim (it is NOT in ``_PLACEHOLDER_ALLOWED_FIELDS``
        # — only ``password`` / ``passphrase`` are). A ``${VAR}`` in it would be
        # written to ``targets.yaml`` literally, then poison every subsequent
        # ``load_targets_config(expand_env=True)`` (registry / doctor / inspect /
        # scheduled run) with ``env_placeholder_not_allowed_here`` — a persistent
        # shared-config DoS. The source layer rejects this in ``resolve_key_path``;
        # ``--from-plan`` skips promotion, so re-assert it here (mirror that guard).
        if entry.key_path is not None and "${" in entry.key_path:
            raise ConfigError(
                "import plan entry has a ${VAR} placeholder in key_path "
                "(it must be a literal path)",
                kind="import_plan_invalid",
                target=entry.name,
            )


def _assert_env_name_invariants(password_env: str | None, passphrase_env: str | None) -> None:
    """Re-check the ``*_env`` references are **bare** env names (not ``${VAR}``).

    A non-None ``password_env`` / ``passphrase_env`` must match
    ``^[A-Z_][A-Z0-9_]*$`` so ``_entry_to_dict`` synthesises an expandable
    ``${VAR}`` placeholder. A tampered plan could carry ``${illegal}`` (which
    ``_entry_to_dict`` would blindly wrap into an unexpandable nested placeholder)
    — reject it here.
    """

    for env_name in (password_env, passphrase_env):
        if env_name is not None and _ENV_VAR_NAME_PATTERN.fullmatch(env_name) is None:
            # Echo the offending reference (control-char-stripped — it is an env
            # var NAME, not a secret value, but a tampered plan could embed spoof
            # chars) so the operator can fix the plan from the CLI error alone.
            raise ConfigError(
                "import plan entry has a credential env reference that is not a bare "
                f"env var name: {_strip_control_chars(env_name)!r}",
                kind="import_plan_invalid",
            )


# Unicode categories dropped from a displayed host/user: C0+C1 controls + DEL
# (``Cc``), zero-width / bidi-override format chars (``Cf``, incl. U+202E RLO),
# and line / paragraph separators (``Zl`` / ``Zp``, incl. U+2028 / U+2029).
_UNSAFE_DISPLAY_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _strip_control_chars(value: str) -> str:
    """Drop control / format / line-separator chars from an operator string.

    ``host`` / ``user`` come from the inventory (ssh_config ``HostName`` / yaml
    ``host``) and are echoed verbatim in the dry-run audit diff. A crafted
    inventory could embed ``\\r`` / the single-byte CSI ``\\x9b`` / a bidi
    override (U+202E) to overwrite or spoof the very preview line the operator
    inspects before passing ``--yes``. Categorise each char so both the C0 *and*
    C1 control ranges, zero-width / bidi format chars, and line separators are
    removed — making the audit line unforgeable.
    """

    return "".join(ch for ch in value if unicodedata.category(ch) not in _UNSAFE_DISPLAY_CATEGORIES)


def _pending_add_label(item: PendingAdd) -> str:
    """Render one ``to_add`` row including its connection address.

    SSH entries show ``name -> user@host:port`` so the operator can audit the
    final connection target before the write; local entries show the name only.
    The host / user are control-char-stripped so a crafted inventory cannot
    spoof the audit line.
    """

    entry = item.entry
    name = _name_with_origin(entry.name, item.raw_identifier)
    if isinstance(entry, SSHEntry):
        port = "" if entry.port == 22 else f":{entry.port}"
        user = f"{_strip_control_chars(entry.user)}@" if entry.user else ""
        return f"{name} -> {user}{_strip_control_chars(entry.host)}{port}"
    return f"{name} (local)"


def _name_with_origin(name: str, raw_identifier: str | None) -> str:
    """Render ``name`` annotated with its pre-normalization origin when they differ.

    Surfaces the spec-required ``原始标识 → 派生 name`` mapping (e.g. ``Web_1`` →
    ``web-1``) so an operator sees how each name was derived before ``--yes``.
    The raw identifier is control-char-stripped — it too is operator inventory
    text echoed into the audit line.
    """

    if raw_identifier is not None and raw_identifier != name:
        return f"{name} (from {_strip_control_chars(raw_identifier)})"
    return name
