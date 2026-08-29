# Keeping the fork alive

The fork is defined as **transforms over pristine upstream**, never as a
vendored copy. Following a new release is therefore a re-run, not a merge.

```bash
tests/run-all.sh          # fetches both upstreams, rebuilds, proves it works
```

If that is green, current upstream is compatible. If it fails, it names the
transform whose anchor moved.

---

## Following a new Kalico release

```bash
scripts/build-kalico-fork.sh --push
scripts/build-configurator-fork.sh --push      # re-pins moonraker.conf
```

The Kalico branch is rebuilt from scratch each time — the delta is one derived
commit, so there is no history worth preserving and nothing to rebase. That is
deliberate: maintaining the original six RatOS commits as a rebasable series
would mean re-resolving the same formatting and structural conflicts on every
single Kalico bump, forever.

**If the port stops applying**, `build-kalico-fork.sh` aborts before writing
anything. Re-derive it: read `kalico/PROVENANCE.md`, which lists what each hunk
is for and which three are mandatory, then re-anchor by hand against the new
`bed_mesh.py`. Run `tests/test_bed_mesh_port.py` — it compares the new mesh
against pristine Kalico's, so a mis-port shows up as a numeric difference rather
than as a crash three weeks later.

Never ship a partially applied firmware patch. The printer keeps running either
way; only one of those states is diagnosable.

---

## Following a new RatOS release

```bash
scripts/build-configurator-fork.sh --push
```

Each transform asserts its anchor matches **exactly once** and aborts naming
itself otherwise. When that happens, open the file, see what RatOS changed, and
decide whether the transform is still needed or needs re-anchoring.

The transform most likely to move is `t_migration_constants` — RatOS is actively
working on `klipper-fork-migration.sh`. The kinematics transforms are the least
likely; that file has been stable.

---

## The deployment branch

Moonraker does **not** pull `~/ratos-configurator` from the source branch. It
pulls `primary_branch`, and that is a build artifact branch: RatOS CI builds
`src/` with pnpm, deletes the source-only directories, renames `src/` → `app/`,
rewrites `RATOS_SCRIPT_DIR` in `.env` from `/src/scripts` to `/app/scripts`, and
force-pushes the result. The systemd unit's `WorkingDirectory` points into
`app/` and `ExecStart` is `pnpm start`, so a source branch leaves the
configurator service with nothing to serve.

The fork carries its own workflow for this. The template lives at
`configurator/publish-kalico.yml.in`; `patch_configurator.py` substitutes the
branch names from `fork.conf`, installs it as
`.github/workflows/publish-kalico.yml`, and **removes upstream's publish
workflows** — they target RatOS' branch names, and a live workflow in a fork
pushing to branches nobody watches is a trap.

Three deliberate differences from upstream's:

- **Triggered by a push to the source branch**, plus `workflow_dispatch` — not
  by a `workflow_run` of "CI". A fresh fork has no CI history to key off, and
  the source branch here is *rebuilt* by `build-configurator-fork.sh` rather
  than developed on.
- **No `last-successful-commit-action`.** It looks up prior runs *by workflow
  filename*, so on a fork's first publish it finds nothing and the commit-count
  step produces garbage. The commit message names the source commit instead.
- **Publishes straight to the deployment branch.** Upstream goes via `staging/`
  and has a human fast-forward it; for a single-owner fork that is ceremony
  without a reviewer.

It also asserts, before publishing, that `configuration/scripts/ratos-common.sh`
and `configuration/klippy/requirements.txt` survived and that `app/.env` no
longer points at `/src/scripts`. Moonraker hard-errors on the first two, and
that error surfaces on the printer rather than in CI.

**Caveat.** The "Delete files not needed in deployment" list is copied verbatim
from upstream and is coupled to RatOS' source layout — if a release moves
directories under `src/`, that list moves with it.

The workflow itself is no longer unproven: every push to the source branch so
far has published successfully, in about two minutes. Read the run rather than
assuming it, though — a green publish means the branch was written, not that
the printer accepted it.

---

## Things that must move together

| If you change | You must also change |
|---|---|
| the Kalico branch tip | `moonraker.conf`'s `pinned_commit` — `build-configurator-fork.sh` does this, and verifies it awk-parses |
| the Kalico branch name | `FORK_KALICO_BRANCH` in `fork.conf`; the recovery alias is rebuilt automatically |
| the fork's repo URLs | `fork.conf` only — nothing else hardcodes them |
| the kinematics contract | `ratos_hybrid_corexy.py` **and** `ratos_homing.py` — they are coupled; Kalico hands the kinematics a string, so patching only one leaves a `TypeError` on every `G28` |

---

## Why some things are shaped the way they are

**Why the recovery alias branch.** Moonraker's klipper updater cannot be told
which branch to track — `primary_branch` is not overridable for the built-in
klipper entry and defaults to `master`. Its Recover button checks that branch
out; Hard Recover `rmtree`s `~/klipper` and clones it. Without a `master`
pointing at the same commit, those buttons strand the printer.

That is a *partial* mitigation only, and `docs/RISKS.md` §11 spells out why: the
migration never repoints `origin`, so a machine that was migrated rather than
cloned from the fork keeps `origin = Klipper3d` and a stale local `master`. Do
not use Mainsail's Recover buttons on a migrated printer.

**Why `origin` is left alone.** Upstream's migration script never rewrites
`origin` — it adds a second remote and resets the branch. The fork keeps that
behaviour. It means the "already migrated, skip" path stays unreachable and the
migration re-runs its fetch and `reset --hard` on every update, which is
wasteful but harmless. Rewriting `origin` would change what Moonraker's klipper
entry sees, and that interaction is unverified.

**Why the transforms assert exactly one match.** A half-applied patch to an
update script is worse than an unpatched one: the printer keeps running either
way, but only the loud failure tells you which.
