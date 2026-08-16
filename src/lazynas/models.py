"""Domain models for lazynas pools.

The rendered system files (snapraid.conf, the fstab managed block, the
cron.d drop-in) are the source of truth; these models exist to render and
validate them.
"""

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, model_validator

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

DEFAULT_EXCLUDES = ["*.unrecoverable", "/tmp/", "/lost+found/"]

MAX_PARITY_LEVELS = 6  # snapraid supports parity through 6-parity


def _clean_mount(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("refusing to use / as a mount point")
    if not value.startswith("/"):
        raise ValueError(f"mount point must be an absolute path, got {value!r}")
    return value


def _validate_cron(value: str) -> str:
    if len(value.split()) != 5:
        raise ValueError(f"cron schedule needs 5 fields (min hour dom mon dow), got {value!r}")
    return value


def _validate_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a valid name (lowercase alphanumeric plus '-'/'_', e.g. 'tank', 'd1')"
        )
    return value


Name = Annotated[str, AfterValidator(_validate_name)]
Mount = Annotated[str, AfterValidator(_clean_mount)]
Cron = Annotated[str, AfterValidator(_validate_cron)]


class DataDisk(BaseModel):
    """A data disk: a mergerfs branch and a snapraid `data NAME MOUNT` entry.

    `name` is the disk's identity in the parity — the mount may change
    freely, but the name must never be renamed or reordered once synced.
    """

    name: Name
    mount: Mount
    uuid: str = ""
    fstype: str = "ext4"
    content: bool = True  # keep a copy of the snapraid content file on this disk
    content_file: str = ""  # verbatim path override (imports); default is <mount>/snapraid.<pool>.content


class ParityDisk(BaseModel):
    """A parity disk. Its own type, so the mergerfs branch list (built from
    DataDisk entries) structurally excludes it."""

    level: int = Field(default=1, ge=1, le=MAX_PARITY_LEVELS)
    mount: Mount
    uuid: str = ""
    fstype: str = "ext4"
    file: str = ""  # verbatim path override (imports); default is <mount>/snapraid.<pool>.<N->parity

    @property
    def directive(self) -> str:
        return "parity" if self.level == 1 else f"{self.level}-parity"

    def parity_file(self, pool_name: str) -> str:
        if self.file:
            return self.file
        suffix = "parity" if self.level == 1 else f"{self.level}-parity"
        return f"{self.mount}/snapraid.{pool_name}.{suffix}"


class MergerfsOptions(BaseModel):
    create_policy: str = "pfrd"  # pfrd | mfs | lus | epmfs (path-preserving)
    minfreespace: str = "200G"
    extra: list[str] = Field(default_factory=list)


class Thresholds(BaseModel):
    """Sync gate: abort an automated sync when `snapraid diff` reports more
    removed/updated files than this. -1 disables a threshold. This is the
    central safety mechanism — after a sync, deleted data is unrecoverable."""

    delete: int = 40
    update: int = -1


class ScrubPolicy(BaseModel):
    enabled: bool = True
    plan: int = 12        # percent of the array checked per run
    older_than: int = 10  # only data not scrubbed within this many days
    schedule: Cron = "0 4 * * 0"  # weekly (Sun 4am); scrub is decoupled from the daily sync


class Pool(BaseModel):
    """A named pool: one mergerfs mount + one snapraid array + the disk
    mounts and cron job that serve them."""

    name: Name
    mount: Mount
    data: list[DataDisk] = Field(default_factory=list)
    parity: list[ParityDisk] = Field(default_factory=list)
    mergerfs: MergerfsOptions = Field(default_factory=MergerfsOptions)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    scrub: ScrubPolicy = Field(default_factory=ScrubPolicy)
    excludes: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    extra_content: list[str] = Field(default_factory=list)  # content files outside the data disks
    schedule: Cron = "0 3 * * *"
    nohidden: bool = False

    @model_validator(mode="after")
    def _cross_checks(self) -> "Pool":
        names = [d.name for d in self.data]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate data disk names in pool {self.name!r}")
        mounts = [d.mount for d in self.data] + [p.mount for p in self.parity]
        if len(set(mounts)) != len(mounts):
            raise ValueError(f"two disks in pool {self.name!r} share a mount point")
        for m in mounts:
            if m == self.mount or m.startswith(self.mount + "/"):
                raise ValueError(
                    f"disk mount {m} lies under the pool mount {self.mount}; "
                    "the mergerfs mount would shadow it"
                )
        levels = sorted(p.level for p in self.parity)
        if levels != list(range(1, len(levels) + 1)):
            raise ValueError(f"parity levels must be contiguous starting at 1, got {levels}")
        if self.data and not self.content_files():
            raise ValueError("at least one disk must carry a snapraid content file")
        return self

    @property
    def branches(self) -> list[str]:
        """mergerfs branches — data disks only; parity is structurally excluded."""
        return [d.mount for d in self.data]

    def content_files(self) -> list[str]:
        files = [
            d.content_file or f"{d.mount}/snapraid.{self.name}.content"
            for d in self.data
            if d.content
        ]
        return files + list(self.extra_content)

    def content_copy_shortfall(self) -> int:
        """How many content copies short of the recommended parity+1 we are.

        snapraid recovery needs a surviving content copy, so the manual
        recommends at least parity-count + 1 copies on different disks.
        """
        recommended = len(self.parity) + 1
        return max(0, recommended - len(self.content_files()))

    def disk(self, name: str) -> DataDisk:
        for d in self.data:
            if d.name == name:
                return d
        known = ", ".join(d.name for d in self.data) or "(none)"
        raise KeyError(f"pool {self.name!r} has no data disk named {name!r}; known: {known}")

    def next_data_name(self) -> str:
        used = {d.name for d in self.data}
        n = 1
        while f"d{n}" in used:
            n += 1
        return f"d{n}"

    def default_disk_mount(self, disk_name: str) -> str:
        return f"/mnt/lazynas/{self.name}/{disk_name}"
