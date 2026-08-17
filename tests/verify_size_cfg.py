#!/usr/bin/env python3
"""Cross-check a RatOS size .cfg against the printer definition.

The configurator derives the axis limits it writes into printer.cfg from the
`bedMargin` in printer-definition.json, while the position_max/position_endstop
values live in the size .cfg. If the two disagree the printer will happily home
into its own frame, so this script verifies they match.

Checked (mirrors src/templates/printers/v-core-4-1-idex.ts):
    stepper_x.position_max          == size.x
    stepper_x.position_min/endstop  == -bedMargin.x[0]
    dual_carriage.position_max      == size.x + bedMargin.x[1]
    dual_carriage.position_endstop  == size.x + bedMargin.x[1]
    stepper_y.position_max          == size.y + bedMargin.y[1]
    stepper_y.position_min/endstop  == -bedMargin.y[0]
    stepper_z.position_max          == size.z

Usage:
    verify-size-cfg.py <printer-definition.json> --size 600
"""

import argparse
import json
import re
import sys


def parse_cfg(path):
    """Minimal klipper-config parser: {section: {key: value}}."""
    sections = {}
    current = None
    with open(path, 'r', encoding='utf-8') as handle:
        for line in handle:
            line = line.split('#', 1)[0].rstrip()
            if not line.strip():
                continue
            header = re.match(r'^\[([^\]]+)\]\s*$', line)
            if header:
                current = header.group(1).strip()
                sections[current] = {}
                continue
            if current is None or line[0] in ' \t':
                continue
            if ':' in line:
                key, value = line.split(':', 1)
                sections[current][key.strip()] = value.strip()
    return sections


def num(value):
    return float(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('definition', help='path to printer-definition.json')
    parser.add_argument('--size', required=True, help='size key to verify, e.g. 600')
    parser.add_argument('--cfg', help='path to the size .cfg (default: <definition dir>/<size>.cfg)')
    args = parser.parse_args()

    with open(args.definition, 'r', encoding='utf-8') as handle:
        definition = json.load(handle)

    if args.size not in definition.get('sizes', {}):
        raise SystemExit(f'error: size "{args.size}" is not defined in {args.definition}')

    size = definition['sizes'][args.size]
    margin = definition.get('bedMargin')
    if margin is None:
        raise SystemExit('error: printer definition has no bedMargin, nothing to cross-check')

    cfg_path = args.cfg
    if cfg_path is None:
        base = args.definition.rsplit('/', 1)[0] if '/' in args.definition else '.'
        cfg_path = f'{base}/{args.size}.cfg'

    cfg = parse_cfg(cfg_path)

    mx0, mx1 = margin['x'][0], margin['x'][1]
    my0, my1 = margin['y'][0], margin['y'][1]

    expected = [
        ('stepper_x', 'position_max', size['x']),
        ('stepper_x', 'position_min', -mx0),
        ('stepper_x', 'position_endstop', -mx0),
        ('stepper_y', 'position_max', size['y'] + my1),
        ('stepper_y', 'position_min', -my0),
        ('stepper_y', 'position_endstop', -my0),
        ('stepper_z', 'position_max', size['z']),
    ]
    if 'dual_carriage' in cfg:
        expected += [
            ('dual_carriage', 'position_max', size['x'] + mx1),
            ('dual_carriage', 'position_endstop', size['x'] + mx1),
        ]

    failures = []
    for section, key, want in expected:
        if section not in cfg:
            failures.append(f'[{section}] missing from {cfg_path}')
            continue
        if key not in cfg[section]:
            failures.append(f'[{section}] {key} missing from {cfg_path}')
            continue
        got = num(cfg[section][key])
        if abs(got - want) > 1e-9:
            failures.append(f'[{section}] {key}: cfg has {got}, definition implies {want}')

    print(f'checking {cfg_path} against {args.definition} (size {args.size})')
    if failures:
        for failure in failures:
            print(f'  FAIL  {failure}')
        print(f'\n{len(failures)} mismatch(es) found.')
        return 1

    print(f'  OK    all {len(expected)} axis limits agree with bedMargin and size {args.size}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
