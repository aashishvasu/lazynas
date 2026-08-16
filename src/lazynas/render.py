"""Render the system files a pool owns: snapraid.conf, the fstab managed
block body, and the cron.d drop-in."""

from lazynas import paths
from lazynas.models import DataDisk, ParityDisk, Pool


def render_snapraid_conf(pool: Pool) -> str:
    parity = sorted(pool.parity, key=lambda p: p.level)
    lines = [
        f'# snapraid.conf for pool "{pool.name}" - managed by lazynas, do not edit by hand.',
        f"# Regenerate with: lazynas pool apply {pool.name}",
        "",
        *(f"{p.directive} {p.parity_file(pool.name)}" for p in parity),
        "",
        *(f"content {c}" for c in pool.content_files()),
        "",
        *(f"data {d.name} {d.mount}" for d in pool.data),
        "",
        *(["nohidden"] if pool.nohidden else []),
        *(f"exclude {e}" for e in pool.excludes),
    ]
    return "\n".join(lines) + "\n"


def mergerfs_options(pool: Pool) -> str:
    # Modern mergerfs option set (Linux 6.6+ guidance).
    opts = [
        "cache.files=off",
        f"category.create={pool.mergerfs.create_policy}",
        "func.getattr=newest",
        "dropcacheonclose=false",
        f"minfreespace={pool.mergerfs.minfreespace}",
        "branches-mount-timeout=30",
        "x-systemd.mount-timeout=45s",
        f"fsname={pool.name}",
    ]
    opts.extend(pool.mergerfs.extra)
    return ",".join(opts)


def _disk_line(disk: DataDisk | ParityDisk) -> str:
    if not disk.uuid:
        raise ValueError(
            f"disk mounted at {disk.mount} has no UUID recorded; "
            "cannot write a stable fstab entry (see `lazynas disks`)"
        )
    return f"UUID={disk.uuid} {disk.mount} {disk.fstype} defaults,nofail 0 2"


def render_fstab_block(pool: Pool) -> str:
    """Body of the pool's managed fstab block: one UUID entry per disk, then
    the mergerfs line. Branches are an explicit colon list built from the
    data disks only — parity can never be pooled."""
    lines = [_disk_line(d) for d in pool.data]
    lines += [_disk_line(p) for p in sorted(pool.parity, key=lambda p: p.level)]
    branches = ":".join(pool.branches)
    lines.append(f"{branches} {pool.mount} fuse.mergerfs {mergerfs_options(pool)} 0 0")
    return "\n".join(lines)


def render_cron_file(pool: Pool, executable: str) -> str:
    """Two independent cadences: a frequent sync (`run`) and, if scrub is
    enabled, a separate scrub. snapraid's --older-than already throttles how
    much a scrub does, so no state is tracked here.

    Both lines take the same non-blocking flock (snapraid forbids a sync and
    a scrub running together): if one is still running when the other fires,
    the second skips and its next cadence retries. Each line's log file
    derives from the command it invokes."""
    lock = f"flock -n /run/lazynas-{pool.name}.lock"
    jobs = [(pool.schedule, "run")]
    if pool.scrub.enabled:
        jobs.append((pool.scrub.schedule, "scrub"))
    lines = [
        f'# Maintenance for lazynas pool "{pool.name}" - managed by lazynas, do not edit by hand.',
        "SHELL=/bin/sh",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ]
    for schedule, command in jobs:
        log = paths.log_file(pool.name, command).as_posix()
        lines.append(f"{schedule} root {lock} {executable} {command} {pool.name} >>{log} 2>&1")
    return "\n".join(lines) + "\n"
