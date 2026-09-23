"""Adopt an existing hand-written snapraid.conf (+ fstab mergerfs line)
into a Pool model, so lazynas can take ownership of a running array
without the user rebuilding anything."""

import re
from dataclasses import dataclass, field

from lazynas import disks, render
from lazynas.models import Pool
from lazynas.system import LazynasError

_PARITY_KEY = re.compile(r"^(?:([2-6])-)?parity$")


@dataclass
class ParsedConf:
    parity: list[tuple[int, str]] = field(default_factory=list)  # (level, parity file)
    data: list[tuple[str, str]] = field(default_factory=list)    # (name, mount)
    content: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    nohidden: bool = False
    unknown: list[str] = field(default_factory=list)  # directives lazynas does not model


def parse_snapraid_conf(text: str) -> ParsedConf:
    parsed = ParsedConf()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.partition(" ")
        key, rest = key.lower(), rest.strip()
        if m := _PARITY_KEY.match(key):
            parsed.parity.append((int(m.group(1) or 1), rest))
        elif key == "content":
            parsed.content.append(rest)
        elif key in ("data", "disk"):  # "disk" is the legacy alias
            name, _, mount = rest.partition(" ")
            parsed.data.append((name, mount.strip()))
        elif key == "exclude":
            parsed.excludes.append(rest)
        elif key == "nohidden":
            parsed.nohidden = True
        else:
            parsed.unknown.append(line)
    return parsed


def find_mergerfs_line(
    fstab_text: str, mount: str | None = None
) -> tuple[list[str], str, str] | None:
    """Return (branches, mountpoint, options) of the first mergerfs fstab
    entry, optionally filtered by mountpoint."""
    for raw in fstab_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4 or "mergerfs" not in fields[2]:
            continue
        if mount and fields[1].rstrip("/") != mount.rstrip("/"):
            continue
        return fields[0].split(":"), fields[1], fields[3]
    return None


def harvest_fstab_mounts(fstab_text: str) -> dict[str, tuple[str, str]]:
    """Map mountpoint → (uuid, fstype) from existing UUID= fstab entries."""
    mounts: dict[str, tuple[str, str]] = {}
    for raw in fstab_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 3 and fields[0].startswith("UUID="):
            mounts[fields[1].rstrip("/")] = (fields[0][5:], fields[2])
    return mounts


def semantic_directives(conf_text: str) -> set[str]:
    """Whitespace-normalized, comment-free directive set — for comparing a
    rendered conf against a hand-written one semantically, not textually."""
    return {
        " ".join(line.split())
        for line in conf_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def import_pool(
    name: str,
    conf_text: str,
    *,
    fstab_text: str = "",
    pool_mount: str | None = None,
    system_lookup: bool = True,
) -> tuple[Pool, list[str]]:
    """Build a Pool from existing configs. Returns (pool, warnings). Never
    touches the system — the caller decides when to `pool apply`."""
    warnings: list[str] = []
    parsed = parse_snapraid_conf(conf_text)
    if parsed.unknown:
        warnings.append(
            "directives lazynas does not model are preserved verbatim but not managed: "
            + "; ".join(parsed.unknown)
        )
    if not parsed.data:
        raise LazynasError("no `data` lines found — is this a snapraid.conf?")

    parity_disks: list[dict] = []
    for level, parity_file in sorted(parsed.parity):
        if "," in parity_file:
            raise LazynasError("split parity (comma-separated parity files) is not supported yet")
        parity_disks.append(
            {"level": level, "mount": parity_file.rsplit("/", 1)[0], "file": parity_file}
        )

    data_disks = [{"name": n, "mount": m.rstrip("/"), "content": False} for n, m in parsed.data]
    data_by_mount = {d["mount"]: d for d in data_disks}
    extra_content: list[str] = []
    for content_file in parsed.content:
        parent = content_file.rsplit("/", 1)[0]
        if parent in data_by_mount:
            data_by_mount[parent]["content"] = True
            data_by_mount[parent]["content_file"] = content_file  # keep the original path verbatim
        else:
            extra_content.append(content_file)

    mergerfs: dict = {}
    existing_opts = None
    found = find_mergerfs_line(fstab_text, pool_mount)
    if found:
        branches, pool_mount, existing_opts = found
        if any("*" in b for b in branches):
            warnings.append(
                "existing fstab uses a glob for branches; lazynas will render an "
                "explicit branch list from the data disks instead"
            )
        else:
            extra_branches = {b.split("=")[0].rstrip("/") for b in branches} - set(data_by_mount)
            if extra_branches:
                warnings.append(
                    "fstab branches that are not snapraid data disks will be dropped: "
                    + ", ".join(sorted(extra_branches))
                )
        if m := re.search(r"category\.create=([^,]+)", existing_opts):
            mergerfs["create_policy"] = m.group(1)
        if m := re.search(r"minfreespace=([^,]+)", existing_opts):
            mergerfs["minfreespace"] = m.group(1)
    elif pool_mount is None:
        raise LazynasError(
            "no mergerfs entry found in fstab; pass --pool-mount to say where the pool mounts"
        )
    else:
        warnings.append("no mergerfs fstab entry found; using default mergerfs options")

    fstab_mounts = harvest_fstab_mounts(fstab_text)
    system_devices: list[disks.BlockDevice] = []
    if system_lookup:
        try:
            system_devices = disks.list_devices()
        except LazynasError:
            warnings.append("lsblk unavailable; UUIDs taken from fstab only")
    by_mountpoint = {d.mountpoint: d for d in system_devices if d.mountpoint}
    for disk in data_disks + parity_disks:
        if disk["mount"] in fstab_mounts:
            disk["uuid"], disk["fstype"] = fstab_mounts[disk["mount"]]
        elif disk["mount"] in by_mountpoint:
            dev = by_mountpoint[disk["mount"]]
            disk["uuid"], disk["fstype"] = dev.uuid, dev.fstype or "ext4"
        else:
            msg = (
                f"no UUID found for {disk['mount']}; record one before `pool apply` "
                "(see `lazynas disks`)"
            )
            if "level" in disk:  # parity: the mount was guessed from the file path
                msg += (
                    f" — mount was derived from the parity file path {disk['file']}; "
                    "if that is not the real mountpoint, fix `mount` in the registry too"
                )
            warnings.append(msg)

    pool = Pool(
        name=name,
        mount=pool_mount,
        data=data_disks,
        parity=parity_disks,
        mergerfs=mergerfs,
        excludes=parsed.excludes,  # imported verbatim, keeping the semantic diff clean
        extra_content=extra_content,
        extra_directives=parsed.unknown,
        nohidden=parsed.nohidden,
    )
    if existing_opts is not None:
        # Partition the user's options against the set render manages. Options
        # lazynas does not understand survive verbatim; a managed key keeps
        # render's value and warns, so the option string never has two values
        # for one key.
        managed = dict(render.mergerfs_option_pairs(pool))
        preserve = []
        for opt in existing_opts.split(","):
            if not opt:
                continue
            key, sep, value = opt.partition("=")
            if not sep:
                if opt != "nofail":  # render emits nofail on the fstab line itself
                    preserve.append(opt)
            elif key not in managed:
                preserve.append(opt)
            elif value != managed[key]:
                warnings.append(
                    f"mergerfs option {opt} is managed by lazynas and will be rendered as "
                    f"{key}={managed[key]}"
                )
        pool.mergerfs.extra = preserve
    if pool.content_copy_shortfall():
        warnings.append(
            f"only {len(pool.content_files())} content copies for {len(pool.parity)} "
            f"parity disk(s); the manual recommends at least {len(pool.parity) + 1}"
        )
    return pool, warnings
