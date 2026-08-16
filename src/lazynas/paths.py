"""System paths, each overridable via a LAZYNAS_* environment variable.

The overrides exist so the whole tool can be exercised against a scratch
directory (tests, dry runs on a dev machine) without touching the system.
"""

import os
from pathlib import Path


def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default))


def registry_file() -> Path:
    return _env_path("LAZYNAS_REGISTRY", "/etc/lazynas/pools.toml")


def fstab_file() -> Path:
    return _env_path("LAZYNAS_FSTAB", "/etc/fstab")


def cron_dir() -> Path:
    return _env_path("LAZYNAS_CRON_DIR", "/etc/cron.d")


def snapraid_conf_dir() -> Path:
    return _env_path("LAZYNAS_SNAPRAID_DIR", "/etc/snapraid")


def backup_dir() -> Path:
    return _env_path("LAZYNAS_BACKUP_DIR", "/var/lib/lazynas/backups")


def state_dir() -> Path:
    return _env_path("LAZYNAS_STATE_DIR", "/var/lib/lazynas")


def log_dir() -> Path:
    return _env_path("LAZYNAS_LOG_DIR", "/var/log/lazynas")


def snapraid_conf(pool_name: str) -> Path:
    return snapraid_conf_dir() / f"{pool_name}.conf"


def cron_file(pool_name: str) -> Path:
    return cron_dir() / f"lazynas-{pool_name}"


def log_file(pool_name: str, op: str) -> Path:
    """Per-operation log file (<pool>-sync.log, <pool>-scrub.log, ...), so
    operations that may run concurrently never interleave in one file."""
    return log_dir() / f"{pool_name}-{op}.log"
