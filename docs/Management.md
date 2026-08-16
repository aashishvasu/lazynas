# Managing a pool day to day

You've created (or imported) a pool and built its initial parity. This guide covers living with it: the commands you'll type, what the scheduled maintenance does, and how to check on it.

The only thing that runs on its own is the cron job lazynas writes, which is `lazynas run <pool>` on a timer. Everything below you can also run by hand.

Throughout, `tank` is just the example pool name. Swap in yours.

## The commands you'll reach for

Read-only, safe to run as a normal user any time:

```sh
lazynas pool list                 # every pool lazynas manages
lazynas pool show tank            # tank's full definition (JSON)
lazynas pool show tank --render   # ...plus the exact files it renders
lazynas status tank               # snapraid status: how protected are you
lazynas smart tank                # SMART health + failure-probability table
lazynas diff tank                 # what's changed since the last sync
```

The two that change parity (run with `sudo`):

```sh
sudo lazynas sync tank            # fold pending changes into parity (gated, see below)
sudo lazynas scrub tank           # verify a slice of existing data against parity
```

And the one cron calls on the daily schedule, which you can also run yourself:

```sh
sudo lazynas run tank             # touch, then gated sync
```

`run` is the frequent maintenance pass, two steps back to back:

1. **touch** fixes files with zero sub-second timestamps so snapraid can tell them apart.
2. **sync** is the gated sync (details below); it folds new, changed, and deleted files into parity.

Scrub is deliberately not part of `run`. It reads back a slice of already-synced data and checks it against parity, catching bit rot while you still have the parity to fix it, but it wakes every disk to do so. Syncing wants to happen daily; scrubbing does not. So scrub runs on its own weekly cron line instead, and you can drop it with `--no-scrub` or retime it with `--scrub-schedule` at create. How big a slice, and how old, comes from the pool's scrub policy (default: 12% of the array, nothing scrubbed in the last 10 days).

## The sync gate, and when it bites

Every automated `sync` (and the sync inside `run`) first runs `snapraid diff` and **refuses to proceed if more files were deleted than the pool's delete threshold** (40 by default). This is the one safety rail that matters: once a deletion is synced into parity, that data is gone for good. The gate is what stops a `rm -rf` accident, or ransomware, from quietly propagating into your parity overnight.

There's an optional update threshold too (off by default, set with `--update-threshold` at create), which guards mass rewrites the same way.

When the gate trips, the sync aborts with a message telling you exactly how many files were involved. If it really was you, say you cleaned out a few TB of old media on purpose, override it:

```sh
sudo lazynas sync tank --force    # skip the gate, this once
```

When cron hits the gate, the run exits non-zero and logs the reason (see below). Parity is left as it was, and the next scheduled run retries. Confirm the deletions were intentional, then run a `--force` sync yourself.

## The scheduled run (cron)

When you create or apply a pool, lazynas writes `/etc/cron.d/lazynas-tank` for you. That cron line is the entire scheduler; there is no lazynas daemon. The file looks roughly like:

```
# Maintenance for lazynas pool "tank" - managed by lazynas, do not edit by hand.
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * * root flock -n /run/lazynas-tank.lock /usr/local/bin/lazynas run tank >>/var/log/lazynas/tank-run.log 2>&1
0 4 * * 0 root flock -n /run/lazynas-tank.lock /usr/local/bin/lazynas scrub tank >>/var/log/lazynas/tank-scrub.log 2>&1
```

Two lines, two cadences: `run` (touch + gated sync) daily at 03:00, and `scrub` weekly on Sunday at 04:00. Both run as root, each appends to its own log file, and both use an absolute path to `lazynas` because cron runs with a bare environment. The shared `flock` keeps snapraid from tripping over itself. If Sunday's sync is still going at 04:00, the scrub skips and tries again next week. The scrub line disappears entirely if the pool was created with `--no-scrub`.

**Prerequisite:** you need a cron daemon that reads `/etc/cron.d` (cronie, vixie-cron, whatever your distro ships). It's almost certainly already installed and running. Confirm with `systemctl status crond` (or `cron`).

### Changing the schedule

The sync schedule is set with `--schedule` (default `0 3 * * *`, 3am daily), the scrub schedule with `--scrub-schedule` (default `0 4 * * 0`, Sunday 4am). Both use standard five-field cron syntax. To change either afterwards, edit the pool's entry in the registry and re-apply:

```sh
sudo $EDITOR /etc/lazynas/pools.toml   # change `schedule` or the scrub `schedule` for tank
sudo lazynas pool apply tank           # re-renders the cron file (and everything else)
```

`apply` re-renders every file the pool owns from the registry, so the cron drop-in picks up the new schedule immediately. A few handy schedules:

```
0 3 * * *      every day at 3am (default)
0 3 * * 0      Sundays at 3am
0 */6 * * *    every six hours
```

### Watching it work

Each command gets its own per-pool log file, named after the command. That
keeps two operations from talking over each other:

```sh
tail -f /var/log/lazynas/tank-run.log     # the daily scheduled run
tail -f /var/log/lazynas/tank-scrub.log   # the weekly scrub
tail -f /var/log/lazynas/tank-sync.log    # manual syncs, including --force overrides
```

In the run log you'll see lines for maintenance starting, whether a sync was
needed, the diff summary, and completion, or a threshold-breach message if the
gate stopped a sync. Read-only commands (`status`, `smart`, `diff`, `disks`)
print to your terminal and never write to the logs. If you put one on a timer
yourself, the redirect is yours to add. To check whether cron ran at all, your
system log has the record:

```sh
journalctl -t CROND --since today     # or: grep CRON /var/log/syslog
```

## Trying things without touching anything

Every command takes `--dry-run`, which prints the files it *would* write and the exact `snapraid`/`mount` commands it *would* run, then does nothing. It's the fastest way to see what a change means before committing to it:

```sh
lazynas --dry-run pool apply tank     # show the rendered files, write none
lazynas --dry-run run tank            # show the snapraid commands cron would run
```

## When a disk is failing

`lazynas smart tank` gives you snapraid's failure-probability estimate per disk. If one is looking grim but still readable, get its files onto the pool before `lazynas pool remove-disk`; anything left behind leaves the pool and its protection. If the disk is already dead, that's a snapraid recovery job: use `snapraid fix`. lazynas does not wrap that rescue yet.

## Every path is overridable

If you want to rehearse any of this against a scratch directory instead of your real system files, all the paths honor environment overrides: `LAZYNAS_REGISTRY`, `LAZYNAS_FSTAB`, `LAZYNAS_CRON_DIR`, `LAZYNAS_SNAPRAID_DIR`, `LAZYNAS_BACKUP_DIR`, `LAZYNAS_STATE_DIR`, `LAZYNAS_LOG_DIR`. Point them at a temp folder and add the global `--dry-run` option. That keeps root and `/etc` out of the experiment.
