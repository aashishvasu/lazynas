"""Block-device discovery and resolution via lsblk JSON output, with a
direct blkid probe as fallback (lsblk reads UUIDs from the udev db, which
can lag right after mkfs)."""

import json
import os
import re
from dataclasses import dataclass

from lazynas.system import LazynasError, run

_UUID_RE = re.compile(r"[0-9A-Fa-f]{4,}(-[0-9A-Fa-f]{2,})*")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]?)I?B?", re.IGNORECASE)


@dataclass
class BlockDevice:
    name: str  # /dev/sdb1
    uuid: str
    fstype: str
    size_bytes: int  # 0 when unknown
    mountpoint: str


def parse_size(text: str) -> int:
    """'200G' → bytes; 0 when unparseable."""
    m = _SIZE_RE.fullmatch(text.strip())
    if not m:
        return 0
    mult = 1024 ** ("KMGTP".index(m.group(2).upper()) + 1) if m.group(2) else 1
    return int(float(m.group(1)) * mult)


def human_size(n: int) -> str:
    if n <= 0:
        return "-"
    x = float(n)
    for unit in "BKMGTP":
        if x < 1024 or unit == "P":
            break
        x /= 1024
    return f"{x:.0f}{unit}" if unit == "B" else f"{x:.1f}{unit}"


def list_devices() -> list[BlockDevice]:
    proc = run(["lsblk", "-J", "-b", "-p", "-o", "NAME,UUID,FSTYPE,SIZE,MOUNTPOINT"])
    assert proc is not None
    devices: list[BlockDevice] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            devices.append(
                BlockDevice(
                    name=node.get("name") or "",
                    uuid=node.get("uuid") or "",
                    fstype=node.get("fstype") or "",
                    size_bytes=int(node.get("size") or 0),
                    mountpoint=node.get("mountpoint") or "",
                )
            )
            walk(node.get("children") or [])

    walk(json.loads(proc.stdout).get("blockdevices", []))
    return devices


def _probe(device: str) -> BlockDevice | None:
    """Ask blkid directly; it probes the device instead of the udev db."""
    try:
        proc = run(["blkid", "-o", "export", device], ok_codes=(0, 2))
    except LazynasError:
        return None
    if proc is None or not proc.stdout.strip():
        return None
    info = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if not info.get("UUID"):
        return None
    return BlockDevice(
        name=device, uuid=info["UUID"], fstype=info.get("TYPE", ""), size_bytes=0, mountpoint=""
    )


def resolve(spec: str) -> BlockDevice:
    """Resolve a device path, filesystem UUID, or current mountpoint to a
    filesystem with a UUID (the stable identity lazynas writes to fstab)."""
    try:
        devices = list_devices()
    except LazynasError:
        # No lsblk (e.g. dev box). A literal UUID is still usable.
        if _UUID_RE.fullmatch(spec):
            return BlockDevice(name="", uuid=spec, fstype="ext4", size_bytes=0, mountpoint="")
        raise
    for dev in devices:
        if spec in (dev.name, dev.uuid, dev.mountpoint) and dev.uuid:
            return dev
    real = os.path.realpath(spec)  # follow /dev/disk/by-id/... symlinks
    for dev in devices:
        if dev.name == real and dev.uuid:
            return dev
    if spec.startswith("/dev/"):
        if probed := _probe(real if real.startswith("/dev/") else spec):
            return probed
    raise LazynasError(
        f"could not resolve {spec!r} to a filesystem with a UUID; "
        "run `lazynas disks` to see candidates"
    )
