# Where `0001-ratos-compat-bed_mesh-gcode_macro.patch` comes from

RatOS 2.1 does not run on stock Klipper. It runs on `Rat-OS/klipper`, branch
`ratos/v2.1.x`, which is upstream Klipper `b7233d11` plus **six commits in two
files**:

| Commit | Date | Subject |
|---|---|---|
| `8c94072b15a2ad502fbe047636f44547b6d9eb12` | 2025-05-08 | bed_mesh: make logging of mesh points configurable |
| `93c8ad8a017c45f4502e73ad1dc174cf7d3c3b90` | 2025-05-08 | bed_mesh: fix cpu hogging when processing large meshes |
| `61db5410da4072e55cb38de78397fabad4be6a7e` | 2025-06-28 | bed_mesh: fix potential cpu hogging when saving profiles |
| `33ffa6276d6b9dce2cecaed06357109dbdf8f27b` | 2025-06-25 | gcode_macro: log warnings about slow template rendering and slow access to items of `printer` |
| `7dd901ecc5c1ba6c1174bff8895ac14f26b26988` | 2025-08-05 | bed_mesh: improve fix for potential cpu hogging |
| `2817b348e23c779b68ae5f27f2b9b9af8cfcf0da` | 2025-08-28 | bed_mesh: reduce split_z_delta minval |

All six are by Tom Glastonbury `<t@tg73.net>`. `git diff --stat
b7233d1..ratos/v2.1.x` is exactly:

```
 klippy/extras/bed_mesh.py    | 113 +++++++++++++++-----
 klippy/extras/gcode_macro.py |  37 ++++---
 2 files changed, 111 insertions(+), 39 deletions(-)
```

There is nothing else in the fork — no MCU/C changes, no new modules, no build
changes.

## Why this is a re-implementation, not a cherry-pick

None of the six patches apply to Kalico. `git am -3` and `git apply --3way`
both fail on every one, with 2 to 14 conflict regions each, for two independent
reasons:

1. **Formatting.** Kalico is ruff-formatted at line length 80 with double
   quotes. Every context line in every hunk differs from Klipper's style, so
   nothing matches.
2. **Structure.** Kalico's `bed_mesh.py` forked from Klipper *before* the
   `ProbeManager` / `RapidScanHelper` refactor. It has no `ProbeManager`, no
   `RapidScanHelper`, no `_handle_dump_request` and no `bed_mesh/dump_mesh`
   webhook; it keeps the older `_generate_points()` / `self.points` /
   `self.substituted_indices` model and adds Kalico-only `cmd_BED_MESH_CHECK`,
   `bed_mesh_default` and an N-axis-aware `MoveSplitter`. `gcode_macro.py` has
   diverged harder still: 502 lines against upstream's 194, with
   `GetStatusWrapper` renamed to `GetStatusWrapperJinja` and `TemplateWrapper`
   to `TemplateWrapperJinja`, so commit `33ffa62`'s target classes do not exist
   by name.

The patch in this directory is therefore a **hand port of the end state** of all
six commits onto Kalico `ae261624f57fb597dcffaa46062b1c58d3e5999d`, not a
replay of the series. It is +131 / −42 across the two files.

Keep it that way. Maintaining the original six as a rebasable series would mean
re-resolving the same formatting and structural conflicts on every single Kalico
bump, forever. The end state is small enough to re-derive by hand.

## What is mandatory versus what is quality-of-life

Three parts of this patch are **hard startup blockers** for RatOS on Kalico.
Without them Klippy does not start, or a core RatOS extension raises
`TypeError`:

| Change | Why it is mandatory |
|---|---|
| `split_delta_z` minval `0.01` → `0.001` | Six V-Core 4 printer profiles ship `split_delta_z: 0.001`. Kalico's `MoveSplitter` rejects it: *"Option 'split_delta_z' in section 'bed_mesh' must have minimum of 0.01"*. Klippy refuses to start. |
| `log_points` / `log_points_truncate` options | `configuration/z-probe/beacon.cfg:41` sets `log_points: False`. Kalico's `error_on_unused_config_options` defaults to **True**, so an unknown option aborts startup. |
| `ZMesh.__init__(self, params, name, reactor=None)` | Three RatOS call sites pass a third argument: `ratos.py:445` (the **core** extension, not an optional one), `beacon_mesh.py:519` and `beacon_mesh.py:1285`. Kalico's two-argument constructor raises `TypeError`. |

The rest — the module-level `pause()` helper, `_reactor_yield`, the yields
through `probe_finalize`, `get_mesh_matrix`, `get_probed_matrix`, `print_mesh`,
`build_mesh`, `_sample_bicubic` and `ProfileManager.save_profile`, and the
`update_status` atomicity fix — is why RatOS forked Klipper in the first place:
it stops a large Beacon mesh from blocking the Klippy greenlet and tripping
`Timer too close`.

## Deliberate deviations from the RatOS original

- **`print_generated_points` merges both projects' solutions.** Kalico already
  had a `truncate` flag (hardcoded 50 points) plus a `log_bed_mesh_at_startup`
  danger option; RatOS added `log_points` / `log_points_truncate`. The port
  keeps both gates rather than replacing Kalico's. Note the resulting rule is
  `50 if (truncate and (limit == 0 or limit > 50)) else limit` — so at startup
  `log_points_truncate: 0`, which RatOS documents as "no truncation", is
  reinterpreted as 50.
- **`_handle_dump_request` hunk dropped.** Kalico has no such method and does
  not register the `bed_mesh/dump_mesh` endpoint. Grepping the configurator,
  the OS image repo and the user's printer config for `dump_mesh` returns zero
  matches, so nothing needs it.
- **`_sample_lagrange` left alone.** RatOS does not yield there either, and the
  saved profiles in use are `algo = bicubic`.
- **`BedMesh.update_status` rewritten to swap a new dict in at the end.** Kalico
  has the same latent bug as Klipper: it publishes a half-filled status dict and
  then fills it in place while the now-yielding getters run, so a status
  subscriber polling during the yields can observe a torn or empty mesh. Adding
  the yields without this fix would actively make that race worse.

## Verified

- `git apply --check` against pristine Kalico `ae261624`: clean, both files.
- `python3 -m py_compile` on both patched files: clean.
- `ruff format --check` and `ruff check`: clean — but under ruff **0.15.8**,
  not the 0.15.22 Kalico pins in `pyproject.toml:23`. Re-check before shipping.

## Not verified

Kalico's own `test/klippy/bed_mesh_check.test` and `bed_mesh_default.test`
exercise exactly these code paths and are the cheapest runtime gate available.
They could not be run here: PyPI is blocked by this environment's egress policy
(HTTP 403), so `numpy` and `jinja2` could not be installed. Run them on the
printer, or on any machine with a Klippy venv:

```bash
cd ~/klipper && python3 scripts/test_klippy.py test/klippy/bed_mesh_check.test
cd ~/klipper && python3 scripts/test_klippy.py test/klippy/bed_mesh_default.test
```

Be aware that in batch mode `reactor.pause` falls back to a real `time.sleep`,
so every yield costs 6 ms of wall clock. The tests are expected to be slow, not
hung.
