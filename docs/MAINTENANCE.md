# Keeping the fork alive

The fork is defined as **transforms over pristine upstream**, never as a
vendored copy. Following a new release is therefore a re-run, not a merge.

```bash
tests/run-all.sh          # fetches both upstreams, rebuilds, proves it works
```

If that is green, current upstream is compatible. If it fails, it names the
transform whose anchor moved.

---

## Who may publish

Publishing — `--push` on either build script — force-pushes the branches every
printer on this fork updates from, rebuilt from pristine upstream plus whatever
the current checkout contains. The last publish therefore wins outright, and
two branches that both publish take turns deleting each other's work without a
word. That is how `power-sensors.cfg` vanished from a running printer whose
`printer.cfg` still included it: a publish from a branch that had never seen
the commit that added the file.

So there is one line, and the scripts enforce it:

```
feature branches ──merge/PR──▶ claude/kalico-rat-os-fork-dd8k9j ──PR──▶ main
                               (develop: the only branch that publishes)
```

- **Feature branches** are where work happens. They build freely — every
  script works without `--push` — but they never publish.
- **`claude/kalico-rat-os-fork-dd8k9j`** is the develop branch, `PUBLISH_BRANCH`
  in `fork.conf`. It is the only branch the build scripts publish from.
- **`main`** is updated from develop by pull request, as the record of what has
  gone out. It never publishes itself.

With `--push`, both build scripts refuse — before building anything — unless:

| Check | Why |
|---|---|
| the checkout is on `PUBLISH_BRANCH` | the rule itself |
| the tree is clean, untracked files included | the build reads the working tree, but the result is labelled with HEAD |
| HEAD is exactly what origin has | a commit only one machine has cannot be merged, diffed or rolled back to by anyone else |
| the live build names a RatOS-kalico commit that HEAD contains | otherwise publishing discards something printers already run |
| *(configurator only)* the Kalico commit it pins was built here, from the firmware patch in HEAD | otherwise firmware goes out under a label it does not match |

The fourth check reads the `RatOS-Kalico-Definition` trailer of the
configurator build that is currently published (see "The changelog the printer
shows") and requires that commit to be HEAD or an ancestor of it. It is what
still holds when someone *did* publish from elsewhere — with an older script,
or by hand. The Kalico build runs it too, although it does not touch the
configurator: the next configurator build pins whatever the Kalico build
pushes. For the last check, `build-kalico-fork.sh` records in
`.work/kalico-built-from.txt` which patch each build applied.

A build takes minutes, and everything checked at the start can change in that
time. So right before pushing, each script checks again that HEAD has not
moved and that nobody has published the configurator since. Both pushes are
also leased on the exact tips read at the start — the configurator branch, the
Kalico branch and its recovery alias — so a publish that lands mid-build makes
the push fail instead of vanishing under it.

When it refuses because the live build came from another branch, the message
names the commit and how to find the branch that has it. Merge that branch into
develop, push, publish again — which is exactly what repairing the
power-sensors incident took. `--allow-rollback` skips that one check, for the
rare case where discarding what is live really is the point, and only as a
flag: an exported `ALLOW_ROLLBACK` is ignored. Nothing skips the others.

**What this cannot stop.** The guard lives in the scripts, so it binds only
checkouts that have it. A branch that split off before it carries the old
scripts and can still publish unchecked. Do not run `--push` from such a branch;
merge its work into develop instead.

**How it is tested.** `tests/test_publish_guard.sh` checks each condition
against throwaway repositories, without network. `tests/test_publish_rehearsal.sh`
runs both build scripts for real, with `--push`, against local stand-ins for
every repository involved, and has a second publisher push in the middle of a
build. Both run in `tests/run-all.sh`.

**One thing that is not reproducible across machines.** The Kalico commit is a
pure function of base and patch only on one machine: a git configured to sign
commits (`commit.gpgsign`) writes the signature into the commit. The cloud
sessions that published so far sign. A build from a machine that does not sign
produces the same tree under a different commit, which printers are offered as
a firmware update with nothing in it.

---

## Following a new Kalico release

```bash
# on develop, clean and pushed -- see "Who may publish"
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
scripts/build-configurator-fork.sh --push      # on develop, clean and pushed
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

### The changelog the printer shows

Mainsail's update dialog lists the commits of the branch Moonraker tracks — the
deployment branch — and shows each commit's **subject**, with the body behind
the `...` expander. Nothing else on that screen is ours to write, so that commit
message *is* the changelog, and until now it read `Deploy v2.1.x-kalico <sha>`,
which tells a printer owner nothing about what is about to change on their
machine.

It is assembled by two halves that cannot see each other:

| Where | What it does |
|---|---|
| `scripts/build-configurator-fork.sh` | composes the text and parks it after a `Printer changelog:` line in the **source** commit, plus a `RatOS-Kalico-Definition: <sha>` trailer naming the RatOS-kalico commit it was built from |
| `configurator/publish-kalico.yml.in` | lifts that section out of the source commit and hands it to the deploy action as the commit message |

The bullets are `git log --no-merges` over the RatOS-kalico commits between the
**previously published** build's trailer and this one, so the list is derived,
never hand-maintained. The first bullet becomes the subject, with `(+N more)`
appended — that one line is all Mainsail shows without expanding anything. The
`Klipper firmware:` line says whether the pin moved, because a moved pin means
the klipper entry will offer an update too.

When the previous build's trailer is missing or names a commit this checkout
does not have, the changelog says so instead of guessing. A changelog nobody
can trust is worse than none.

Breaking this is silent by construction: the workflow falls back to the old
`Deploy <sha>` message, the publish still succeeds and the printer still
updates. `tests/test_changelog_message.py` therefore checks both halves
together — that the marker still matches, that the subject is a subject rather
than a wrapped bullet, and that the firmware line is there. It runs inside
`build-configurator-fork.sh` itself, on every build, not only in `run-all.sh`.

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
