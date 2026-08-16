"""lazynas — manage snapraid + mergerfs storage pools from one callsite."""

from pathlib import Path

import typer
from pydantic import ValidationError

from lazynas import __version__, disks, paths, render, runner, system
from lazynas.importer import import_pool, semantic_directives
from lazynas.models import MergerfsOptions, Pool, ScrubPolicy, Thresholds
from lazynas.registry import Registry
from lazynas.snapraid import Snapraid
from lazynas.system import LazynasError

app = typer.Typer(
    no_args_is_help=True,
    help="A lazy NAS: snapraid + mergerfs pools defined, applied, and maintained from one CLI.",
)
pool_app = typer.Typer(no_args_is_help=True, help="Define, apply, and modify pools.")
app.add_typer(pool_app, name="pool")


class Ctx:
    def __init__(self, registry: Registry, dry_run: bool):
        self.registry = registry
        self.dry_run = dry_run


def entry() -> None:
    """Console-script entry point: user-facing errors print without a traceback."""
    try:
        app()
    except (LazynasError, ValidationError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    registry: Path | None = typer.Option(
        None, "--registry", help="Pool registry file (default /etc/lazynas/pools.toml, or $LAZYNAS_REGISTRY)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print rendered files and commands without touching anything."
    ),
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
):
    if version:
        typer.echo(f"lazynas {__version__}")
        raise typer.Exit()
    ctx.obj = Ctx(Registry(registry), dry_run)


# --------------------------------------------------------------------------- helpers


def _split_spec(spec: str) -> tuple[str, str | None]:
    """'<device>[:<mount>]' where device is a path, UUID, or mountpoint."""
    head, sep, tail = spec.rpartition(":")
    if sep and head and tail.startswith("/"):
        return head, tail
    return spec, None


def _data_spec(spec: str) -> tuple[str | None, str, str | None]:
    """'[name=]<device>[:<mount>]' → (name, device, mount). The name is the
    snapraid `data NAME` identity; None means assign the next free dN."""
    name = None
    head, sep, rest = spec.partition("=")
    if sep and "/" not in head:
        name, spec = head, rest
    device, mount = _split_spec(spec)
    return name, device, mount


def _unmanaged_fstab_conflicts(fstab_text: str, pool: Pool) -> list[str]:
    """fstab lines outside this pool's managed block that mount the same
    places — typically leftovers from a hand-managed setup being imported."""
    unmanaged = system.remove_block(fstab_text, pool.name)
    mounts = {pool.mount, *pool.branches, *(p.mount for p in pool.parity)}
    conflicts = []
    for raw in unmanaged.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[1].rstrip("/") in mounts:
            conflicts.append(line)
    return conflicts


def _apply(c: Ctx, pool: Pool) -> None:
    """Render and write everything the pool owns: snapraid.conf, the fstab
    managed block, mount directories, and the cron.d drop-in."""
    if not c.dry_run:
        system.require_root()
    system.write_file(
        paths.snapraid_conf(pool.name), render.render_snapraid_conf(pool), dry_run=c.dry_run
    )
    fstab = paths.fstab_file()
    fstab_text = fstab.read_text(encoding="utf-8") if fstab.exists() else ""
    for line in _unmanaged_fstab_conflicts(fstab_text, pool):
        _warn(f"unmanaged fstab entry also mounts a path this pool owns — remove it: {line}")
    system.write_file(
        fstab,
        system.upsert_block(fstab_text, pool.name, render.render_fstab_block(pool)),
        dry_run=c.dry_run,
    )
    system.write_file(
        paths.cron_file(pool.name),
        render.render_cron_file(pool, system.cli_executable()),
        dry_run=c.dry_run,
    )
    if not c.dry_run:
        dirs = [pool.mount, *pool.branches, *(p.mount for p in pool.parity)]
        dirs += [Path(content).parent.as_posix() for content in pool.extra_content]
        for directory in dirs:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise LazynasError(
                    f"cannot create {directory} ({exc}); a dead mount may be in the way — "
                    f"check `findmnt {directory}` and umount it, then re-run "
                    f"`lazynas pool apply {pool.name}`"
                ) from exc


def _warn(message: str) -> None:
    typer.secho(f"warning: {message}", fg=typer.colors.YELLOW, err=True)


def _size_warnings(
    data_devs: list[disks.BlockDevice], parity_devs: list[disks.BlockDevice], minfreespace: str
) -> list[str]:
    """Size sanity for resolved devices; devices with unknown size are
    skipped, so UUID-only resolution stays usable."""
    warnings = []
    data = [d for d in data_devs if d.size_bytes]
    parity = [p for p in parity_devs if p.size_bytes]
    if data and parity:
        big = max(data, key=lambda d: d.size_bytes)
        small = min(parity, key=lambda p: p.size_bytes)
        if small.size_bytes < big.size_bytes:
            warnings.append(
                f"parity disk {small.name or small.uuid} ({disks.human_size(small.size_bytes)}) "
                f"is smaller than data disk {big.name or big.uuid} "
                f"({disks.human_size(big.size_bytes)}); snapraid needs parity >= the largest "
                "data disk, and sync will fail once that disk outgrows the parity"
            )
    mfs = disks.parse_size(minfreespace)
    if mfs and data and all(d.size_bytes <= mfs for d in data):
        warnings.append(
            f"minfreespace {minfreespace} is at least the size of every data disk; "
            "mergerfs will reject all new files (ENOSPC)"
        )
    return warnings


def _content_copy_check(pool: Pool) -> None:
    if shortfall := pool.content_copy_shortfall():
        _warn(
            f"pool has {len(pool.content_files())} content copies but the manual recommends "
            f"at least {len(pool.parity) + 1} (parity + 1); {shortfall} more would improve recoverability"
        )


# --------------------------------------------------------------------------- pool lifecycle


@pool_app.command()
def create(
    ctx: typer.Context,
    name: str,
    mount: str | None = typer.Option(None, "--mount", "-m", help="mergerfs mountpoint (default /mnt/<name>)."),
    data: list[str] = typer.Option(
        [], "--data", "-d",
        help="Data disk: [name=]<device>[:<mount>] — device is a path, UUID, or current "
        "mountpoint; name is the snapraid data identity (default: next free dN). Repeatable.",
    ),
    parity: list[str] = typer.Option(
        [], "--parity", "-p",
        help="Parity disk (same format); repeat for 2-parity, 3-parity, … Parity disks must be your largest.",
    ),
    create_policy: str = typer.Option("pfrd", help="mergerfs category.create policy (pfrd/mfs/lus/epmfs)."),
    minfreespace: str = typer.Option("200G", help="mergerfs minfreespace per branch."),
    mergerfs_opt: list[str] = typer.Option(
        [], "--mergerfs-opt", help="Extra mergerfs mount option, appended verbatim. Repeatable."
    ),
    os_content: bool = typer.Option(
        True, "--os-content/--no-os-content",
        help="Keep a content copy on the OS disk (/var/lib/lazynas/<pool>.content) — snapraid "
        "recommends one copy outside the array.",
    ),
    extra_content: list[str] = typer.Option(
        [], "--extra-content",
        help="Additional snapraid content file outside the data disks. Repeatable.",
    ),
    delete_threshold: int = typer.Option(
        40, help="Abort automated syncs when diff reports more deletions than this (-1 disables)."
    ),
    update_threshold: int = typer.Option(
        -1, help="Abort automated syncs when diff reports more updates than this (-1 disables)."
    ),
    scrub: bool = typer.Option(True, "--scrub/--no-scrub", help="Schedule a periodic scrub for this pool."),
    schedule: str = typer.Option("0 3 * * *", help="Cron schedule for the sync run."),
    scrub_schedule: str = typer.Option("0 4 * * 0", help="Cron schedule for the scrub (default weekly)."),
    apply_now: bool = typer.Option(True, "--apply/--no-apply", help="Write system files immediately."),
):
    """Create a pool: one mergerfs mount + one snapraid array."""
    c: Ctx = ctx.obj
    if name in c.registry.load():
        raise LazynasError(f"pool {name!r} already exists (see `lazynas pool show {name}`)")
    if not data:
        raise LazynasError("a pool needs at least one --data disk")
    mount = mount or f"/mnt/{name}"

    parsed_data = [_data_spec(spec) for spec in data]
    used_names = {n for n, _, _ in parsed_data if n}
    data_disks = []
    data_devs = []
    counter = 1
    for disk_name, device_spec, disk_mount in parsed_data:
        if disk_name is None:
            while f"d{counter}" in used_names:
                counter += 1
            disk_name = f"d{counter}"
            used_names.add(disk_name)
        dev = disks.resolve(device_spec)
        data_devs.append(dev)
        data_disks.append({
            "name": disk_name,
            "mount": disk_mount or f"/mnt/lazynas/{name}/{disk_name}",
            "uuid": dev.uuid,
            "fstype": dev.fstype or "ext4",
        })
    parity_disks = []
    parity_devs = []
    for level, spec in enumerate(parity, start=1):
        device_spec, disk_mount = _split_spec(spec)
        dev = disks.resolve(device_spec)
        parity_devs.append(dev)
        parity_disks.append({
            "level": level,
            "mount": disk_mount or f"/mnt/lazynas/{name}/p{level}",
            "uuid": dev.uuid,
            "fstype": dev.fstype or "ext4",
        })
    for message in _size_warnings(data_devs, parity_devs, minfreespace):
        _warn(message)

    extra_content = list(extra_content)
    if os_content:
        default_content = (paths.state_dir() / f"{name}.content").as_posix()
        if default_content not in extra_content:
            extra_content.insert(0, default_content)

    pool = Pool(
        name=name,
        mount=mount,
        data=data_disks,
        parity=parity_disks,
        mergerfs=MergerfsOptions(create_policy=create_policy, minfreespace=minfreespace, extra=mergerfs_opt),
        thresholds=Thresholds(delete=delete_threshold, update=update_threshold),
        scrub=ScrubPolicy(enabled=scrub, schedule=scrub_schedule),
        extra_content=extra_content,
        schedule=schedule,
    )
    _content_copy_check(pool)
    c.registry.upsert(pool, dry_run=c.dry_run)
    typer.echo(f"pool {name!r}: {len(data_disks)} data + {len(parity_disks)} parity disk(s) registered")
    if apply_now:
        _apply(c, pool)
        typer.echo("\nNext steps:")
        typer.echo("  1. mount everything:        mount -a   (or reboot)")
        typer.echo(f"  2. build initial parity:    lazynas sync {name}")


@pool_app.command("import")
def import_(
    ctx: typer.Context,
    name: str,
    conf: Path = typer.Option(..., "--conf", exists=True, readable=True, help="Existing hand-written snapraid.conf."),
    pool_mount: str | None = typer.Option(None, "--pool-mount", help="mergerfs mountpoint, if not discoverable from fstab."),
):
    """Adopt an existing snapraid + mergerfs setup into lazynas.

    Registers the pool and shows a semantic diff against your current
    files; nothing on the system changes until `lazynas pool apply`.
    """
    c: Ctx = ctx.obj
    if name in c.registry.load():
        raise LazynasError(f"pool {name!r} already exists")
    fstab = paths.fstab_file()
    fstab_text = fstab.read_text(encoding="utf-8") if fstab.exists() else ""
    conf_text = conf.read_text(encoding="utf-8")
    pool, warnings = import_pool(name, conf_text, fstab_text=fstab_text, pool_mount=pool_mount)
    for message in warnings:
        _warn(message)

    existing = semantic_directives(conf_text)
    rendered = semantic_directives(render.render_snapraid_conf(pool))
    if existing == rendered:
        typer.secho("snapraid.conf round-trip is semantically identical.", fg=typer.colors.GREEN)
    else:
        typer.echo("semantic differences vs your existing snapraid.conf:")
        for line in sorted(existing - rendered):
            typer.secho(f"  - {line}", fg=typer.colors.RED)
        for line in sorted(rendered - existing):
            typer.secho(f"  + {line}", fg=typer.colors.GREEN)

    c.registry.upsert(pool, dry_run=c.dry_run)
    typer.echo(f"\npool {name!r} imported into the registry; no system files were changed.")
    typer.echo(f"Review the diff above, then take ownership with:  lazynas pool apply {name}")


@pool_app.command()
def apply(ctx: typer.Context, name: str):
    """(Re)render and write snapraid.conf, the fstab block, and the cron job."""
    c: Ctx = ctx.obj
    pool = c.registry.get(name)
    _apply(c, pool)
    if not c.dry_run:
        typer.echo(f"pool {name!r} applied: {paths.snapraid_conf(name)}, fstab block, {paths.cron_file(name)}")


@pool_app.command("list")
def list_(ctx: typer.Context):
    """List registered pools."""
    pools = ctx.obj.registry.load()
    if not pools:
        typer.echo("no pools defined — start with `lazynas pool create` or `lazynas pool import`")
        return
    for pool in pools.values():
        typer.echo(
            f"{pool.name:<16} mount={pool.mount}  data={len(pool.data)}  "
            f"parity={len(pool.parity)}  schedule='{pool.schedule}'"
        )


@pool_app.command()
def show(
    ctx: typer.Context,
    name: str,
    render_files: bool = typer.Option(False, "--render", help="Also print the rendered system files."),
):
    """Show a pool's definition (and optionally its rendered files)."""
    pool = ctx.obj.registry.get(name)
    typer.echo(pool.model_dump_json(indent=2))
    if render_files:
        typer.echo(f"\n# --- {paths.snapraid_conf(name)} ---")
        typer.echo(render.render_snapraid_conf(pool))
        typer.echo("# --- fstab managed block ---")
        typer.echo(render.render_fstab_block(pool))
        typer.echo(f"\n# --- {paths.cron_file(name)} ---")
        typer.echo(render.render_cron_file(pool, system.cli_executable()))


@pool_app.command("add-disk")
def add_disk(
    ctx: typer.Context,
    name: str,
    data: str | None = typer.Option(None, "--data", help="Data disk to add (device path, UUID, or mountpoint)."),
    parity: str | None = typer.Option(None, "--parity", help="Parity disk to add as the next parity level."),
    disk_name: str | None = typer.Option(None, "--disk-name", help="snapraid data NAME (default: next free dN)."),
    disk_mount: str | None = typer.Option(None, "--mount", help="Where to mount the disk (default /mnt/lazynas/<pool>/<name>)."),
):
    """Add a disk to a pool and re-apply its system files."""
    c: Ctx = ctx.obj
    if bool(data) == bool(parity):
        raise LazynasError("pass exactly one of --data or --parity")
    pool = c.registry.get(name)
    spec = data or parity
    assert spec is not None
    device_spec, spec_mount = _split_spec(spec)
    dev = disks.resolve(device_spec)
    mount_override = disk_mount or spec_mount

    try:
        by_uuid = {d.uuid: d for d in disks.list_devices() if d.uuid}
    except LazynasError:
        by_uuid = {}
    data_devs = [by_uuid[d.uuid] for d in pool.data if d.uuid in by_uuid]
    parity_devs = [by_uuid[p.uuid] for p in pool.parity if p.uuid in by_uuid]
    (data_devs if data else parity_devs).append(dev)
    for message in _size_warnings(data_devs, parity_devs, pool.mergerfs.minfreespace):
        _warn(message)

    dump = pool.model_dump()
    if data:
        new_name = disk_name or pool.next_data_name()
        dump["data"].append({
            "name": new_name,
            "mount": mount_override or pool.default_disk_mount(new_name),
            "uuid": dev.uuid,
            "fstype": dev.fstype or "ext4",
        })
        after = f"lazynas sync {name}"
        added = f"data disk {new_name}"
    else:
        level = len(pool.parity) + 1
        dump["parity"].append({
            "level": level,
            "mount": mount_override or pool.default_disk_mount(f"p{level}"),
            "uuid": dev.uuid,
            "fstype": dev.fstype or "ext4",
        })
        after = f"lazynas sync {name} --full   # -F rebuilds the new parity level"
        added = f"parity level {level}"
    pool = Pool.model_validate(dump)
    _content_copy_check(pool)
    c.registry.upsert(pool, dry_run=c.dry_run)
    _apply(c, pool)
    typer.echo(f"pool {name!r}: added {added}")
    typer.echo("\nNext steps:")
    typer.echo("  1. mount the new disk:  mount -a")
    typer.echo(f"  2. update parity:       {after}")


@pool_app.command("remove-disk")
def remove_disk(
    ctx: typer.Context,
    name: str,
    disk_name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Remove a data disk using the official snapraid sequence.

    Points the disk's `data` entry at an empty directory, runs
    `snapraid sync --force-empty`, then drops the entry and the mergerfs
    branch. Remaining disk names are never renamed or reordered.
    """
    c: Ctx = ctx.obj
    pool = c.registry.get(name)
    try:
        disk = pool.disk(disk_name)
    except KeyError as exc:
        raise LazynasError(str(exc)) from exc
    # Both the interim and final pools must keep at least one content file.
    survivors = [d for d in pool.data if d.name != disk_name]
    if not any(d.content for d in survivors) and not pool.extra_content:
        raise LazynasError(
            f"{disk_name} carries the pool's only snapraid content file; set "
            "`content = true` on another data disk in the registry and "
            f"`lazynas pool apply {name}` before removing it"
        )

    typer.echo(f"Removing {disk_name} ({disk.mount}) from pool {name!r}:")
    typer.echo("  1. snapraid.conf points the disk at an empty directory")
    typer.echo("  2. snapraid sync --force-empty re-parities without it")
    typer.echo("  3. the data line, fstab entry, and mergerfs branch are removed")
    _warn(
        f"files on {disk.mount} leave the pool and its protection — move them "
        "onto the pool first if you want to keep them"
    )
    if not yes and not c.dry_run and not typer.confirm("Proceed?"):
        raise typer.Exit(1)
    if not c.dry_run:
        system.require_root()

    empty_dir = paths.state_dir() / "empty" / f"{name}-{disk_name}"
    if not c.dry_run:
        empty_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: interim conf — disk pointed at the empty dir, its content file dropped.
    interim_dump = pool.model_dump()
    for entry in interim_dump["data"]:
        if entry["name"] == disk_name:
            entry["mount"] = empty_dir.as_posix()
            entry["content"] = False
    interim = Pool.model_validate(interim_dump)
    system.write_file(
        paths.snapraid_conf(name), render.render_snapraid_conf(interim), dry_run=c.dry_run
    )

    # Step 2: re-parity without the disk.
    snap = Snapraid(paths.snapraid_conf(name), dry_run=c.dry_run)
    try:
        snap.sync(force_empty=True)
    except LazynasError as exc:
        raise LazynasError(
            f"{exc}\nsnapraid.conf still points {disk_name} at the empty directory; "
            f"nothing else was changed — restore it with: lazynas pool apply {name}"
        ) from exc

    # Step 3: final pool without the disk; re-apply everything.
    final_dump = pool.model_dump()
    final_dump["data"] = [d for d in final_dump["data"] if d["name"] != disk_name]
    final = Pool.model_validate(final_dump)
    _content_copy_check(final)
    c.registry.upsert(final, dry_run=c.dry_run)
    _apply(c, final)
    typer.echo(f"pool {name!r}: {disk_name} removed; unmount it when convenient:  umount {disk.mount}")


@pool_app.command()
def destroy(
    ctx: typer.Context,
    name: str,
    purge: bool = typer.Option(False, "--purge", help="Also delete the generated snapraid.conf."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Stop managing a pool: remove its fstab block, cron job, and registry
    entry. Disks and their data are untouched."""
    c: Ctx = ctx.obj
    pool = c.registry.get(name)
    if not yes and not c.dry_run and not typer.confirm(
        f"Remove pool {name!r} from lazynas management? (data is untouched)"
    ):
        raise typer.Exit(1)
    if not c.dry_run:
        system.require_root()
    fstab = paths.fstab_file()
    if fstab.exists():
        system.write_file(
            fstab, system.remove_block(fstab.read_text(encoding="utf-8"), name), dry_run=c.dry_run
        )
    system.remove_file(paths.cron_file(name), dry_run=c.dry_run)
    if purge:
        system.remove_file(paths.snapraid_conf(name), dry_run=c.dry_run)
    c.registry.remove(name, dry_run=c.dry_run)
    typer.echo(f"pool {name!r} is no longer managed; mounts stay active until you `umount` them")
    _ = pool  # looked up above so a bad name fails before any change


# --------------------------------------------------------------------------- operations


@app.command("disks")
def disks_(ctx: typer.Context):
    """List block devices with filesystems (candidates for pools)."""
    for dev in disks.list_devices():
        if not dev.fstype:
            continue
        mounted = dev.mountpoint or "not mounted"
        typer.echo(
            f"{dev.name:<32} {disks.human_size(dev.size_bytes):>8}  "
            f"{dev.fstype:<8} uuid={dev.uuid or '-':<38} {mounted}"
        )


def _snap(ctx: typer.Context, name: str) -> tuple[Ctx, Pool, Snapraid]:
    c: Ctx = ctx.obj
    pool = c.registry.get(name)
    return c, pool, Snapraid(paths.snapraid_conf(name), dry_run=c.dry_run)


@app.command()
def status(ctx: typer.Context, name: str):
    """snapraid status for a pool."""
    _, _, snap = _snap(ctx, name)
    typer.echo(snap.status())


@app.command()
def smart(ctx: typer.Context, name: str):
    """SMART health and failure-probability table (snapraid smart)."""
    _, _, snap = _snap(ctx, name)
    typer.echo(snap.smart())


@app.command()
def diff(ctx: typer.Context, name: str):
    """Show pending changes (snapraid diff)."""
    _, _, snap = _snap(ctx, name)
    result = snap.diff()
    typer.echo(result.summary())
    if result.sync_required:
        typer.echo(f"sync required — run: lazynas sync {name}")


@app.command()
def touch(ctx: typer.Context, name: str):
    """Fix zero sub-second timestamps (snapraid touch)."""
    _, _, snap = _snap(ctx, name)
    snap.touch()


@app.command()
def sync(
    ctx: typer.Context,
    name: str,
    force: bool = typer.Option(False, "--force", help="Skip the deletion-threshold gate."),
    full: bool = typer.Option(False, "--full", help="snapraid sync -F (after adding a parity level)."),
    empty: bool = typer.Option(False, "--empty", help="snapraid sync -E (normally only used by remove-disk)."),
):
    """Diff-gated sync: refuses to fold mass deletions into parity."""
    c, pool, snap = _snap(ctx, name)
    runner.setup_logging(name, ctx.command.name, to_file=not c.dry_run)
    synced = runner.gated_sync(pool, snap, force=force, force_empty=empty, force_full=full)
    if synced and not c.dry_run:
        typer.echo(f"pool {name!r} synced")


@app.command()
def scrub(
    ctx: typer.Context,
    name: str,
    plan: int | None = typer.Option(None, help="Percent of the array to check (default: pool policy)."),
    older_than: int | None = typer.Option(None, help="Only data unscrubbed for this many days (default: pool policy)."),
):
    """Scrub a slice of the array (snapraid scrub)."""
    c, pool, snap = _snap(ctx, name)
    runner.setup_logging(name, ctx.command.name, to_file=not c.dry_run)
    plan_pct = plan if plan is not None else pool.scrub.plan
    older = older_than if older_than is not None else pool.scrub.older_than
    runner.log.info("pool %s: scrub starting (plan %s%%, older than %sd)", name, plan_pct, older)
    snap.scrub(plan=plan_pct, older_than=older)
    runner.log.info("pool %s: scrub complete", name)


@app.command("run")
def run_(
    ctx: typer.Context,
    name: str,
    force: bool = typer.Option(False, "--force", help="Skip the deletion-threshold gate."),
):
    """What cron invokes on the pool schedule: touch, then gated sync.
    Scrub runs on its own cron cadence (see `lazynas scrub`)."""
    c, pool, _ = _snap(ctx, name)
    runner.setup_logging(name, ctx.command.name, to_file=not c.dry_run)
    try:
        runner.maintenance(pool, dry_run=c.dry_run, force=force)
    except LazynasError as exc:
        runner.log.error("pool %s: %s", name, exc)
        raise typer.Exit(1)
