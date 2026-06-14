"""``assemble_save_entries`` credential-env threading (PR #102 Cursor finding).

With ``--include-unreachable`` a failed probe is written ``enabled=False``; its
``${VAR}`` credential refs must survive so re-enabling the host later does not
silently lose its auth.
"""

from __future__ import annotations

from hostlens.targets.config import SSHEntry
from hostlens.targets.import_plan import FailedProbe, ImportPlan
from hostlens.targets.onboard import assemble_save_entries
from hostlens.targets.probe import ProbeResult


def _unreachable_failed() -> FailedProbe:
    return FailedProbe(
        entry=SSHEntry(name="web1", type="ssh", host="10.0.0.1", user="root"),
        result=ProbeResult(
            reachable=False, capabilities=[], fingerprint={}, error_kind="unreachable"
        ),
        password_env="WEB1_PW",
        passphrase_env="WEB1_PASS",
    )


def test_include_unreachable_threads_credential_env() -> None:
    plan = ImportPlan(
        to_add=[], skipped=[], failed_probe=[_unreachable_failed()], invalid_candidate=[]
    )
    entries = assemble_save_entries(plan, include_unreachable=True)
    assert len(entries) == 1
    entry, password_env, passphrase_env = entries[0]
    assert entry.enabled is False
    assert password_env == "WEB1_PW"
    assert passphrase_env == "WEB1_PASS"


def test_skip_unreachable_omits_failed_entirely() -> None:
    plan = ImportPlan(
        to_add=[], skipped=[], failed_probe=[_unreachable_failed()], invalid_candidate=[]
    )
    assert assemble_save_entries(plan, include_unreachable=False) == []
