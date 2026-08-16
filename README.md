# lazynas

I've been running a snapraid+mergerfs setup for a few years at this point, but it was growing a little stale. The minute I needed to add a second array, or change a disk, I faced some issue that could only be resolved by running manual commands. Every change meant re-reading the same snapraid man pages and hoping I didn't break fstab. lazynas is the (hopeful) replacement.

You define a named **pool** once, and lazynas manages everything that pool means on a snapraid + mergerfs box from one CLI: the snapraid array config, the mergerfs union mount, the per-disk fstab entries, and the scheduled maintenance. Nothing runs in the background. The one scheduled piece is a cron line that calls `lazynas run <pool>`, which drives the `snapraid` binary with the right `-c <conf>`.

## What a pool is

One pool is one mergerfs mount plus one snapraid array. I've taken inspiration from how TrueNAS and Unraid work. Concretely, lazynas owns four things:

- `/etc/snapraid/<pool>.conf`, the generated snapraid config (parity, content, data, excludes)
- a managed block in `/etc/fstab`: one `UUID=` entry per data/parity disk plus the mergerfs line. The branch list is explicit, and **parity disks are structurally excluded from branches**.
- `/etc/cron.d/lazynas-<pool>`, the scheduled `lazynas run <pool>`
- an entry in `/etc/lazynas/pools.toml`, the thin registry lazynas re-renders from. The rendered system files are the source of truth; the registry is just the index that produces them.

Every write is atomic, and the previous version of any file it touches gets a timestamped backup in `/var/lib/lazynas/backups/`.

## Prerequisites

lazynas is a thin coordinator, so the storage stack needs to be in place first:

- **Linux.** It writes fstab, drops files in `/etc/cron.d`, and mounts FUSE filesystems. None of that translates anywhere else.
- **Python 3.11 or newer.** `install.sh` finds a suitable interpreter for you (see [Install](#install)).
- **snapraid 11.0 or newer.** v11 is where adding a parity level became safe (existing parity stays protected during the rebuild), and lazynas leans on that.
- **mergerfs, 2.40 or newer.** The mount options lazynas writes (`category.create=pfrd`, `branches-mount-timeout`) assume a modern version and skip legacy options like `allow_other` entirely.
- **A cron daemon that reads `/etc/cron.d`**: cronie, vixie-cron, whatever your distro ships. Almost certainly already there.
- **`lsblk`** (part of util-linux) for disk discovery. Also almost certainly already there.
- **Formatted disks.** lazynas mounts filesystems and tracks them by UUID; it never partitions or formats anything. Bring your own ext4/xfs.
- **Root**, for anything that writes system files. Read-only commands (`list`, `show`, `status`, `diff`) run fine as a normal user.

## Install

```sh
git clone https://github.com/aashishvasu/lazynas
cd lazynas
sudo ./install.sh            # uninstall: sudo ./install.sh uninstall
```

`install.sh` picks the newest Python 3.11+ it can find, sets lazynas up in its own venv under `/usr/lib/lazynas/venv`, and links a `lazynas` command into `/usr/local/bin`. After that it's on your PATH as both your user and under `sudo`.

If you'd rather use `pipx`, install it for root so `sudo lazynas` finds it:

```sh
sudo pipx install --global .   # pipx 1.5+
```

## Quickstart

> **Note:** `tank` in every example below is not a command or keyword. It's just the name I chose for the example pool. Use any name you like (`media`, `backups`, and so on); it becomes the pool's identity everywhere (`/etc/snapraid/<name>.conf`, the fstab block, the cron job).

```sh
# see candidate disks (device, size, fstype, uuid, current mount)
lazynas disks

# create a pool named "tank": 3 data disks + 1 parity (parity must be your largest disk)
sudo lazynas pool create tank \
    --data /dev/sda1 --data /dev/sdb1 --data /dev/sdc1 \
    --parity /dev/sdd1

sudo mount -a          # mount disks + pool (defaults: /mnt/lazynas/tank/dN, pool at /mnt/tank)
sudo lazynas sync tank # build initial parity
```

Disk specs accept a device path, filesystem UUID, or current mountpoint, with an optional `:<mount>` if you'd rather pick where each disk mounts, and an optional `name=` prefix on data disks to choose the snapraid `data NAME` identity (default: `d1`, `d2`, …). Pick names you like at create time — once synced, a name can never be changed:

```sh
sudo lazynas pool create tank -d films=/dev/sda1:/mnt/films -d tv=/dev/sdb1:/mnt/tv -p /dev/sdd1:/mnt/parity1
```

`create` also exposes the pool's policy knobs, all optional with defaults:

- `--delete-threshold N` and `--update-threshold N` set the sync gate limits; `-1` disables one (see [The sync gate](#the-sync-gate)).
- `--scrub` / `--no-scrub` controls whether a scrub is scheduled at all.
- `--schedule` and `--scrub-schedule` set the sync and scrub cron cadences (defaults: daily 3am, weekly Sunday 4am).
- `--mergerfs-opt OPT` appends an extra mergerfs mount option verbatim; repeatable.
- Content copies: every data disk carries one, and by default so does the OS disk (`/var/lib/lazynas/<pool>.content`) — snapraid recommends a copy outside the array. `--no-os-content` skips the OS copy; `--extra-content PATH` adds more elsewhere, repeatable.

## Adopting an existing setup

This was the feature I built last, since I already had running arrays:

```sh
lazynas pool import tank --conf /etc/snapraid.conf
```

It parses your hand-written snapraid.conf and your fstab (mergerfs line plus UUID entries), registers the pool, and prints a **semantic diff** between what lazynas would render and what you have. Nothing on the system changes until you run `sudo lazynas pool apply tank`. Check that the diff is empty (or only shows directives lazynas warned about) before applying. That's your proof the tool understood your setup.

## Day-to-day

The commands below are the quick reference. For how the scheduled maintenance and cron setup work, see [docs/Management.md](docs/Management.md).

```sh
lazynas pool list
lazynas pool show tank --render     # print the rendered files
lazynas status tank                 # snapraid status
lazynas smart tank                  # SMART health + failure-probability table
lazynas diff tank                   # pending changes
sudo lazynas sync tank              # diff-gated sync (see below)
sudo lazynas scrub tank             # scrub per pool policy (12% / older than 10d)
sudo lazynas run tank               # what cron runs daily: touch, then gated sync
```

Sync and scrub run on separate cadences. Cron gets a daily `run` (touch + gated sync) and, unless you passed `--no-scrub`, a weekly `scrub`. Set them with `--schedule` and `--scrub-schedule` at create time.

### The sync gate

Automated syncs are a little dangerous with any raid system, but snapraid makes it worse because *you* trigger them: once a deletion is synced, the data is unrecoverable. Every `sync` and `run` first checks `snapraid diff` and **aborts if more than the pool's delete threshold (default 40) files were removed**, so mass deletion or ransomware never propagates into parity on a schedule. An update threshold (`--update-threshold`, off by default) guards mass rewrites the same way. When it really was you, override with `lazynas sync tank --force`. Syncs always use `--pre-hash`.

## Growing and shrinking

```sh
sudo lazynas pool add-disk tank --data /dev/sde1          # next free dN, then: lazynas sync tank
sudo lazynas pool add-disk tank --parity /dev/sdf1        # next parity level, then: lazynas sync tank --full
sudo lazynas pool remove-disk tank d3                     # official sequence: empty dir, sync -E, drop entry
sudo lazynas pool destroy tank                            # stop managing; disks and data untouched
```

snapraid `data` names are identity: lazynas never renames or reorders them, and remove-disk keeps the remaining names stable.

## Dry runs and testing

Every command takes `--dry-run`, which prints the rendered files and the exact snapraid/mount commands and touches nothing. All system paths honor env overrides (`LAZYNAS_REGISTRY`, `LAZYNAS_FSTAB`, `LAZYNAS_CRON_DIR`, `LAZYNAS_SNAPRAID_DIR`, `LAZYNAS_BACKUP_DIR`, `LAZYNAS_STATE_DIR`, `LAZYNAS_LOG_DIR`), so the whole tool can be exercised against a scratch directory. That's because my main dev machine is Windows without a real mergerfs or snapraid binary in sight.

```sh
pip install -e .[dev]
pytest
```

## Roadmap

- Email/webhook notifications from `run` (cron-invoked, still no daemon)
- split parity (`parity p1,p2`)
- guided `restore`/`fix` (maybe?) (snapraid does not restore permissions/ownership/xattrs, and the tool will say so)
