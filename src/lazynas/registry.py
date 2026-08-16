"""Pool registry: a thin TOML file mapping pool name → definition.

The rendered system files are the source of truth; the registry remembers
what lazynas manages so it can re-render them.
"""

import tomllib
from pathlib import Path

import tomli_w
from pydantic import ValidationError

from lazynas import paths
from lazynas.models import Pool
from lazynas.system import LazynasError, write_file


def _cross_pool_check(pools: dict[str, Pool]) -> None:
    """One disk, one pool: a UUID or mount claimed by two pools would render
    duplicate fstab entries and overlapping snapraid arrays."""
    mounts: dict[str, str] = {}
    uuids: dict[str, str] = {}
    for name, pool in sorted(pools.items()):
        disks = [*pool.data, *pool.parity]
        for mount in [pool.mount, *(d.mount for d in disks)]:
            owner = mounts.setdefault(mount, name)
            if owner != name:
                raise LazynasError(f"mount {mount} is used by both pool {owner!r} and {name!r}")
        for uuid in (d.uuid for d in disks if d.uuid):
            owner = uuids.setdefault(uuid, name)
            if owner != name:
                raise LazynasError(f"disk UUID {uuid} is used by both pool {owner!r} and {name!r}")


class Registry:
    def __init__(self, path: Path | None = None):
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or paths.registry_file()

    def load(self) -> dict[str, Pool]:
        # The file may be hand-edited; errors name it and the offending pool.
        if not self.path.exists():
            return {}
        try:
            raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise LazynasError(f"registry {self.path} is not valid TOML: {exc}") from exc
        pools: dict[str, Pool] = {}
        for name, body in raw.get("pools", {}).items():
            try:
                pools[name] = Pool.model_validate({"name": name, **body})
            except ValidationError as exc:
                raise LazynasError(
                    f"registry {self.path}: pool {name!r} is invalid: {exc}"
                ) from exc
        return pools

    def get(self, name: str) -> Pool:
        pools = self.load()
        if name not in pools:
            known = ", ".join(sorted(pools)) or "(none)"
            raise LazynasError(f"no pool named {name!r}; known pools: {known}")
        return pools[name]

    def save(self, pools: dict[str, Pool], *, dry_run: bool = False) -> None:
        _cross_pool_check(pools)
        doc = {
            "pools": {
                name: pool.model_dump(mode="json", exclude={"name"})
                for name, pool in sorted(pools.items())
            }
        }
        write_file(self.path, tomli_w.dumps(doc), dry_run=dry_run)

    def upsert(self, pool: Pool, *, dry_run: bool = False) -> None:
        pools = self.load()
        pools[pool.name] = pool
        self.save(pools, dry_run=dry_run)

    def remove(self, name: str, *, dry_run: bool = False) -> None:
        pools = self.load()
        pools.pop(name, None)
        self.save(pools, dry_run=dry_run)
