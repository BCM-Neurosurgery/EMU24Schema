"""
Standalone diagnostic script: walks a video-recording directory tree and, for a representative
sample of the chunk JSON files in each {emu_id}/{date} folder, records that file's last-modified
time and the last timestamp in its own "real_times" list - i.e. how long after the camera's own
last-recorded frame timestamp the chunk file was actually written to disk. Feeds into
collection_delay.ipynb's neural-vs-video acquisition-latency comparison.

"real_times" values are left as the raw ISO-like strings found in the JSON, not parsed here -
downstream notebooks parse them with pandas (format='ISO8601'), same as this project's other CSVs
(see collection_delay.ipynb/coverage_timeline_base.ipynb, which both hit inconsistent-ISO-format
issues parsing this pipeline's own timestamps).

Directory layout expected under --base-path:
    {emu_id}Datafile/VIDEO/{date}/<many chunk files, some of them .json>

Each {emu_id}/{date} folder is sampled independently (--fraction of its own .json files, at least
one if the folder has any), rather than sampling uniformly across the whole tree, so a folder with
far more files doesn't dominate the sample.

Two resume modes against an existing output file:
  - Default: an (emu_id, date) folder that already has ANY row in the output is left alone - only
    folders with no rows yet are sampled. This is for growing coverage across NEW folders (e.g.
    after more recording days have been added) without re-touching ones already looked at.
  - --resample: also draws MORE samples from folders that already have rows, --fraction of that
    folder's total file count, excluding files already sampled from it (keyed on
    (emu_id, date, filename)) - so repeated --resample runs converge a given folder toward full
    coverage instead of being stuck at whatever the first pass sampled.
Either way, a file already in the output is never re-sampled/duplicated.

Usage:
    python scripts/video_file_audit.py --base-path <path> [--fraction 0.1] [--seed 0]
        [--resample] [--out video_file_audit.csv]
"""

import argparse
import csv
import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ['emu_id', 'date', 'filename', 'last_modified_utc', 'last_real_time', 'issue']


def discover_json_folders(base_path):
    """Yield (emu_id, date, folder_path) for every {emu_id}Datafile/VIDEO/{date} folder under
    base_path. base_path is typically a network share - a transient drop (WinError 53/"network
    path not found") on any one directory listing is logged and that directory is skipped,
    rather than crashing the whole run and losing every sample gathered so far."""
    base_path = Path(base_path)
    try:
        patient_dirs = sorted(base_path.iterdir())
    except OSError as e:
        logging.error(f'could not list {base_path}: {e}')
        return

    for patient_dir in patient_dirs:
        if not patient_dir.is_dir() or not patient_dir.name.endswith('Datafile') or not patient_dir.name.startswith('Y'):
            continue
        emu_id = patient_dir.name[: -len('Datafile')]
        video_dir = patient_dir / 'VIDEO'
        if not video_dir.is_dir():
            continue
        try:
            date_dirs = sorted(video_dir.iterdir())
        except OSError as e:
            logging.error(f'could not list {video_dir}: {e}')
            continue
        for date_dir in date_dirs:
            if date_dir.is_dir():
                yield emu_id, date_dir.name, date_dir


def sample_json_files(folder_path, fraction, rng, already_sampled, emu_id, date):
    """~fraction of ALL .json files in folder_path (at least one, if any are left to sample),
    drawn only from those not already keyed in already_sampled - so the fraction is always
    relative to the folder's total size, and repeated runs converge on full coverage instead of
    each one re-shrinking the sample by whatever's already done."""
    try:
        json_files = sorted(folder_path.glob('*.json'))
    except OSError as e:
        logging.error(f'could not list {folder_path}: {e}')
        return []
    if not json_files:
        return []
    candidates = [p for p in json_files if (emu_id, date, p.name) not in already_sampled]
    if not candidates:
        return []
    sample_size = max(1, round(len(json_files) * fraction))
    return rng.sample(candidates, min(sample_size, len(candidates)))


def audit_json_file(path):
    """(last_modified_utc, last_real_time, issue) for one chunk JSON file. last_real_time is the
    raw last entry of the file's own "real_times" list, unconverted (see module docstring)."""
    issue = ''
    last_modified_utc = None
    last_real_time = None

    try:
        last_modified_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError as e:
        issue = f'could not stat file: {e}'

    try:
        with open(path) as f:
            data = json.load(f)
        real_times = data['real_times']
        if not real_times:
            issue = (issue + '; ' if issue else '') + 'real_times is empty'
        else:
            last_real_time = real_times[-1]
    except (OSError, json.JSONDecodeError, KeyError, IndexError) as e:
        logging.error(f'could not read real_times from {path}: {e}')
        issue = (issue + '; ' if issue else '') + f'could not read real_times: {e}'

    return last_modified_utc, last_real_time, issue


def load_sampled_keys(out_path):
    """(emu_id, date, filename) for every row already in the output CSV, so a rerun samples only
    files not already audited, and never adds a duplicate."""
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline='') as f:
        return {(row['emu_id'], row['date'], row['filename']) for row in csv.DictReader(f)}


def main(base_path, fraction, out_path, seed=None, resample=False):
    rng = random.Random(seed)
    already_sampled = load_sampled_keys(out_path)
    sampled_folders = {(emu_id, date) for emu_id, date, _ in already_sampled}
    if already_sampled:
        print(f'{len(already_sampled)} file(s) already sampled in {out_path}, across {len(sampled_folders)} folder(s)')

    out_exists = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    total_written = 0

    with open(out_path, 'a' if out_exists else 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not out_exists:
            writer.writeheader()

        for emu_id, date, folder_path in discover_json_folders(base_path):
            if not resample and (emu_id, date) in sampled_folders:
                continue

            sampled = sample_json_files(folder_path, fraction, rng, already_sampled, emu_id, date)
            if not sampled:
                continue

            print(f'{emu_id} {date}: sampling {len(sampled)} file(s)')
            for path in sampled:
                last_modified_utc, last_real_time, issue = audit_json_file(path)
                writer.writerow({
                    'emu_id': emu_id,
                    'date': date,
                    'filename': path.name,
                    'last_modified_utc': last_modified_utc,
                    'last_real_time': last_real_time,
                    'issue': issue,
                })
                total_written += 1

    print(f'Wrote {total_written} row(s) to {out_path} this run')


if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser(
        description='Sample video chunk JSON files and record their last-modified time and last "real_times" timestamp.',
    )
    arg_parser.add_argument(
        '--base-path', type=str, required=True,
        help='Root folder containing {emu_id}Datafile/VIDEO/{date}/ subfolders'
    )
    arg_parser.add_argument(
        '--fraction', type=float, default=0.1,
        help="Fraction of each folder's total .json files to sample per run (default 0.1)"
    )
    arg_parser.add_argument('--seed', type=int, default=None, help='Random seed for reproducible sampling')
    arg_parser.add_argument(
        '--resample', action='store_true',
        help='Also draw more samples from folders that already have rows, not just new ones'
    )
    arg_parser.add_argument('--out', type=str, default='video_file_audit.csv')
    args = arg_parser.parse_args()

    main(args.base_path, args.fraction, args.out, args.seed, args.resample)
