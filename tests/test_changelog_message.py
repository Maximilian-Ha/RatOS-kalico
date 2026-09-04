#!/usr/bin/env python3
"""Prove a printer owner gets a readable changelog before pressing update.

Mainsail's update dialog lists the commits of the branch Moonraker tracks --
the DEPLOYMENT branch -- and shows each commit's subject, with the body behind
the "..." expander. Nothing else on that screen is under our control, so the
deployment commit message *is* the changelog, and it is assembled across two
files that cannot see each other:

* ``scripts/build-configurator-fork.sh`` composes it and parks it after a
  ``Printer changelog:`` line in the SOURCE commit;
* ``configurator/publish-kalico.yml.in`` lifts that section out and hands it to
  the deploy action.

Break the marker in either one and nothing fails: the workflow's fallback still
publishes, the printer still updates, and the changelog silently becomes
"Deploy v2.1.x-kalico <sha>" again -- the exact state this replaced. So the
check has to be explicit.

Usage:
    tests/test_changelog_message.py <built-configurator-checkout> [<template>]

Exit codes:
    0  the message is present, well-formed and reachable by the workflow
    1  it is not
    2  bad invocation
"""

import os
import subprocess
import sys

MARKER = "Printer changelog:"
TRAILER = "RatOS-Kalico-Definition:"
DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "configurator",
    "publish-kalico.yml.in",
)


def extract(message):
    """What the workflow's awk lifts out: everything after the marker line."""
    lines = message.splitlines()
    for i, line in enumerate(lines):
        if line == MARKER:
            return "\n".join(lines[i + 1 :]).strip("\n")
    return ""


def check(checkout, template_path):
    ok = True

    try:
        message = subprocess.run(
            ["git", "-C", checkout, "log", "-1", "--format=%B", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print("FAIL: cannot read the built commit in %s: %s" % (checkout, exc))
        return False

    # 1. the source commit records which fork definition built it -- without
    #    that trailer the NEXT build has no range to compute a changelog over.
    if TRAILER not in message:
        print("FAIL: the built commit carries no %s trailer, so the next build "
              "cannot tell what changed since it" % TRAILER)
        ok = False
    else:
        print("ok: the build records the fork definition it came from")

    # 2. the section the workflow reads exists and is not empty.
    changelog = extract(message)
    if not changelog:
        print("FAIL: the built commit has no '%s' section -- the workflow would "
              "fall back to naming the commit and the printer would see no "
              "changelog" % MARKER)
        return False
    print("ok: the '%s' section is present" % MARKER)

    lines = changelog.splitlines()

    # 3. a subject worth reading on its own: it is the only line Mainsail shows
    #    without expanding anything.
    subject = lines[0].strip()
    if not subject:
        ok = False
        print("FAIL: the changelog starts with an empty line, so the commit "
              "subject Mainsail lists would be blank")
    elif subject.startswith("-"):
        ok = False
        print("FAIL: the first line is a bullet, not a subject: %r" % subject)
    elif len(subject) > 120:
        ok = False
        print("FAIL: the subject is %d characters; Mainsail truncates it" % len(subject))
    else:
        print("ok: subject (%d chars): %s" % (len(subject), subject))

    # 4. bullets, or an explicit statement that there is no changelog. Silence
    #    is the one thing that may not happen.
    body = [l for l in lines[1:] if l.strip() and not l.startswith("Klipper firmware:")
            and not l.startswith("Built from ") and not l.startswith("the klipper entry")]
    bullets = [l for l in body if l.startswith("- ")]
    if not bullets:
        ok = False
        print("FAIL: the changelog body has no bullets at all")
    elif len(bullets) != len(body):
        ok = False
        wrapped = [l for l in body if not l.startswith("- ")]
        print("FAIL: %d body line(s) are not bullets -- a wrapped bullet truncates "
              "the subject mid-sentence: %r" % (len(wrapped), wrapped[:2]))
    else:
        print("ok: %d bullet(s), each on one line" % len(bullets))

    # 5. the firmware line -- the one item on the list worth reading BEFORE
    #    pressing update, because a moved pin means Klipper updates too.
    if not any(l.startswith("Klipper firmware:") for l in lines):
        ok = False
        print("FAIL: the changelog does not say whether the Klipper pin moved")
    else:
        print("ok: the changelog states the Klipper firmware pin")

    # 6. the workflow still reads exactly this marker and still uses the result.
    try:
        with open(template_path, "r") as handle:
            template = handle.read()
    except OSError as exc:
        print("FAIL: cannot read %s: %s" % (template_path, exc))
        return False

    if "/^%s$/" % MARKER not in template:
        ok = False
        print("FAIL: %s no longer greps for '%s' -- the two halves have drifted "
              "and the changelog would silently stop being published"
              % (os.path.basename(template_path), MARKER))
    elif "commit-message: ${{ steps.message.outputs.text }}" not in template:
        ok = False
        print("FAIL: the publish step no longer uses the composed message")
    else:
        print("ok: the publish workflow reads this section and commits it")

    print("\n--- what the printer will show ---\n%s\n---" % changelog)
    return ok


def main(argv):
    if len(argv) not in (2, 3):
        sys.stderr.write("usage: %s <built-configurator-checkout> [<template>]\n" % argv[0])
        return 2
    template = argv[2] if len(argv) == 3 else DEFAULT_TEMPLATE
    return 0 if check(argv[1], template) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
