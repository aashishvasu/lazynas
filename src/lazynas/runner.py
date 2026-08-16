"""The cron entrypoint: touch, diff, threshold gate, then sync.

This is the safety-critical path. An automated sync must never run after a
mass deletion (ransomware, a fat-fingered rm) because syncing folds the
deletions into parity and makes the data unrecoverable. The gate prevents it.
"""

import logging
import sys

from lazynas import paths
from lazynas.models import Pool
from lazynas.snapraid import DiffResult, Snapraid
from lazynas.system import LazynasError

log = logging.getLogger("lazynas")


class ThresholdBreach(LazynasError):
    pass


def setup_logging(pool_name: str, op: str, *, to_file: bool = True) -> None:
    """Route this operation's log lines to <pool>-<op>.log. `op` is the CLI
    command name (callers pass ctx.command.name), so every operation derives
    its own file. Read-only queries (status, smart, diff, disks) never set
    up logging — their output belongs on stdout."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(paths.log_file(pool_name, op), encoding="utf-8"))
        except OSError:
            pass  # unprivileged run — stderr logging still works
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def gate(pool: Pool, diff: DiffResult) -> None:
    t = pool.thresholds
    if t.delete >= 0 and diff.removed > t.delete:
        raise ThresholdBreach(
            f"{diff.removed} files were deleted (threshold {t.delete}); refusing to sync. "
            f"If this was intentional: lazynas sync {pool.name} --force"
        )
    if t.update >= 0 and diff.updated > t.update:
        raise ThresholdBreach(
            f"{diff.updated} files were updated (threshold {t.update}); refusing to sync. "
            f"If this was intentional: lazynas sync {pool.name} --force"
        )


def gated_sync(
    pool: Pool,
    snap: Snapraid,
    *,
    force: bool = False,
    force_empty: bool = False,
    force_full: bool = False,
) -> bool:
    """Run diff, apply the threshold gate, then sync. Returns True if a
    sync actually ran."""
    diff = snap.diff()
    if snap.dry_run:
        snap.sync(force_empty=force_empty, force_full=force_full)
        return True
    if not diff.sync_required and not (force_full or force_empty):
        log.info("pool %s: already in sync (%s)", pool.name, diff.summary())
        return False
    if not force:
        gate(pool, diff)
    log.info("pool %s: syncing (%s)", pool.name, diff.summary())
    snap.sync(force_empty=force_empty, force_full=force_full)
    return True


def maintenance(pool: Pool, *, dry_run: bool = False, force: bool = False) -> None:
    """The frequent scheduled pass: touch then gated sync. Scrub runs on its
    own cron cadence (pool.scrub.schedule), not here."""
    snap = Snapraid(paths.snapraid_conf(pool.name), dry_run=dry_run)
    log.info("pool %s: maintenance starting", pool.name)
    snap.touch()
    gated_sync(pool, snap, force=force)
    log.info("pool %s: maintenance complete", pool.name)
