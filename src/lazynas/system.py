"""Low-level plumbing: atomic writes with backups, marker-delimited managed
blocks in shared files (fstab), subprocess execution, privilege checks."""

import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from lazynas import paths


class LazynasError(RuntimeError):
    """A user-facing failure; the CLI prints it without a traceback."""


def require_root() -> None:
    geteuid = getattr(os, "geteuid", None)  # absent on Windows dev boxes
    if geteuid is not None and geteuid() != 0:
        raise LazynasError("this command writes system files; re-run with sudo")


def _begin(pool_name: str) -> str:
    return f"# BEGIN lazynas pool {pool_name}"


def _end(pool_name: str) -> str:
    return f"# END lazynas pool {pool_name}"


def upsert_block(text: str, pool_name: str, body: str) -> str:
    """Insert or replace this pool's managed block, leaving the rest of the
    file untouched (same idea as Ansible's blockinfile)."""
    begin, end = _begin(pool_name), _end(pool_name)
    block = f"{begin}\n{body.rstrip()}\n{end}\n"
    pattern = re.compile(
        rf"^{re.escape(begin)}\n.*?^{re.escape(end)}\n?", re.DOTALL | re.MULTILINE
    )
    if pattern.search(text):
        return pattern.sub(lambda _: block, text)
    if not text.strip():
        return block
    return text.rstrip("\n") + "\n\n" + block


def remove_block(text: str, pool_name: str) -> str:
    begin, end = _begin(pool_name), _end(pool_name)
    pattern = re.compile(
        rf"\n*^{re.escape(begin)}\n.*?^{re.escape(end)}\n?", re.DOTALL | re.MULTILINE
    )
    return pattern.sub("\n", text).lstrip("\n") if pattern.search(text) else text


def write_file(path: Path, content: str, *, dry_run: bool = False) -> None:
    """Backup-then-atomically-replace: copy the old file to the backup dir
    with a timestamp, write to a temp file, then os.replace."""
    if dry_run:
        print(f"--- would write {path} ---")
        print(content, end="" if content.endswith("\n") else "\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backups = paths.backup_dir()
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / f"{path.name}.{stamp}")
    tmp = path.with_name(path.name + ".lazynas-tmp")
    tmp.write_text(content, encoding="utf-8")
    # cron ignores group/world-writable files in /etc/cron.d, so the mode is
    # set explicitly: new files get 0644, an existing target keeps its mode.
    os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else 0o644)
    os.replace(tmp, path)


def remove_file(path: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"--- would remove {path} ---")
        return
    path.unlink(missing_ok=True)


def run(
    argv: list[str],
    *,
    ok_codes: tuple[int, ...] = (0,),
    capture: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """Run a command with a list argv (never shell=True). Returns None in
    dry-run mode. `capture=False` streams output (long syncs/scrubs)."""
    if dry_run:
        print("[dry-run] " + shlex.join(argv))
        return None
    try:
        proc = subprocess.run(argv, capture_output=capture, text=True)
    except FileNotFoundError as exc:
        raise LazynasError(f"{argv[0]!r} not found on PATH — is it installed?") from exc
    if proc.returncode not in ok_codes:
        detail = ""
        if capture:
            detail = "\n" + (proc.stderr.strip() or proc.stdout.strip())
        raise LazynasError(f"`{shlex.join(argv)}` exited {proc.returncode}{detail}")
    return proc


def cli_executable() -> str:
    """Absolute command for cron to invoke — cron has a minimal environment,
    so the installed entry point (or interpreter) must be an absolute path."""
    exe = shutil.which("lazynas")
    if exe:
        return exe
    return f"{sys.executable} -m lazynas"
