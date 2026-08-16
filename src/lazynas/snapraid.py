"""Thin wrapper around the snapraid binary.

Each pool has its own conf, so every invocation carries `-c <conf>` — the
idiomatic way to run multiple arrays on one host.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from lazynas.system import run

# Summary lines at the end of `snapraid diff`, e.g. "     127 added"
_DIFF_LINE = re.compile(
    r"^\s*(\d+)\s+(equal|added|removed|updated|moved|copied|restored)\s*$", re.MULTILINE
)

# snapraid exit codes: 0 ok, 1 error, 2 (diff only) "sync required" —
# informational, not a failure.
SYNC_REQUIRED = 2


@dataclass
class DiffResult:
    counts: dict[str, int] = field(default_factory=dict)
    sync_required: bool = False

    @property
    def removed(self) -> int:
        return self.counts.get("removed", 0)

    @property
    def updated(self) -> int:
        return self.counts.get("updated", 0)

    def summary(self) -> str:
        interesting = {k: v for k, v in self.counts.items() if v and k != "equal"}
        return ", ".join(f"{v} {k}" for k, v in interesting.items()) or "no changes"


def parse_diff(output: str) -> dict[str, int]:
    return {kind: int(count) for count, kind in _DIFF_LINE.findall(output)}


class Snapraid:
    def __init__(self, conf: Path, *, binary: str = "snapraid", dry_run: bool = False):
        self.conf = conf
        self.binary = binary
        self.dry_run = dry_run

    def _argv(self, *args: str) -> list[str]:
        return [self.binary, "-c", self.conf.as_posix(), *args]

    def diff(self) -> DiffResult:
        proc = run(self._argv("diff"), ok_codes=(0, SYNC_REQUIRED), dry_run=self.dry_run)
        if proc is None:  # dry run
            return DiffResult()
        return DiffResult(parse_diff(proc.stdout), proc.returncode == SYNC_REQUIRED)

    def sync(self, *, force_empty: bool = False, force_full: bool = False) -> None:
        # --pre-hash reads everything twice to catch silent read errors
        # before they reach the parity (mitigates non-ECC RAM).
        args = ["sync", "--pre-hash"]
        if force_empty:
            args.append("--force-empty")
        if force_full:
            args.append("--force-full")
        run(self._argv(*args), capture=False, dry_run=self.dry_run)

    def scrub(self, *, plan: int, older_than: int) -> None:
        run(
            self._argv("scrub", "--plan", str(plan), "--older-than", str(older_than)),
            capture=False,
            dry_run=self.dry_run,
        )

    def touch(self) -> None:
        run(self._argv("touch"), capture=False, dry_run=self.dry_run)

    def status(self) -> str:
        proc = run(self._argv("status"), dry_run=self.dry_run)
        return proc.stdout if proc else ""

    def smart(self) -> str:
        proc = run(self._argv("smart"), dry_run=self.dry_run)
        return proc.stdout if proc else ""
