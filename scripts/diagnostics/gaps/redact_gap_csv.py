"""
Standalone utility: redact every data_coverage.py output CSV in a folder for sharing outside the
institution (discontinuity_summary.csv, visit_summary.csv, packet_summary.csv, and any older
gap_summary.csv/imputed_gaps.csv - anything that follows the same column-naming conventions).

No column names are hardcoded - columns are sorted into cleaning groups by what's IN their name:
    - Any column whose name contains "utc" is treated as a pandas-parseable timestamp and shifted.
    - Any column whose name contains "file" is treated as holding either a full filesystem path
      (blanked entirely, since a local path can reveal machine/folder structure) or a short
      base-file-style label with an embedded BRK timestamp string like "19700125-233441"
      somewhere in it (that embedded date is shifted in place, rest of the label untouched). A
      value counts as a "full path" if it contains a path separator ('/' or '\\'); otherwise it's
      treated as a label.

The shift is one fixed offset PER PATIENT (grouped by the `emu_id` column, present in every one
of these CSVs), not a single offset for the whole folder - so every file's dates for a given
patient move together and stay comparable across files, while different patients' offsets are
independent, matching data_coverage.py's own emu_id grouping. Each patient's offset is chosen so
their earliest known recording start becomes 1970-01-01: preferably from a "*start*utc*" column
(visit_summary.csv's recording_start_utc, if present in the folder), falling back to the minimum
across all "*utc*" columns for that patient if no such column is found anywhere in the folder.

The chosen offsets are never printed or written anywhere, since they'd let the real dates be
reconstructed from the redacted (shifted) ones.

Usage:
    python scripts/redact_gap_csv.py input_dir output_dir
"""

import argparse
import csv
import os
import re
from datetime import datetime, timedelta

import pandas as pd

BRK_DATE_RE = re.compile(r'\d{8}-\d{6}')
BRK_DATE_FMT = '%Y%m%d-%H%M%S'
EPOCH = datetime(1970, 1, 1)


def is_utc_column(name):
    return 'utc' in name.lower()


def is_file_column(name):
    return 'file' in name.lower()


def is_start_utc_column(name):
    lowered = name.lower()
    return 'start' in lowered and 'utc' in lowered


def is_full_path(value):
    return '/' in value or '\\' in value


def parse_utc(value):
    if not value:
        return None
    try:
        return pd.Timestamp(value).to_pydatetime()
    except (ValueError, TypeError):
        return None


def shift_brk_date(value, offset):
    match = BRK_DATE_RE.search(value)
    if not match:
        return value
    shifted = (datetime.strptime(match.group(), BRK_DATE_FMT) + offset).strftime(BRK_DATE_FMT)
    return value[:match.start()] + shifted + value[match.end():]


def load_csv(path):
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def compute_patient_offsets(csv_paths):
    """{emu_id: offset} so each patient's earliest known recording start becomes 1970-01-01.
    Prefers a "*start*utc*" column wherever found across the folder; falls back to the minimum
    over all "*utc*" columns for patients with no such column."""
    preferred_dates = {}
    fallback_dates = {}

    for path in csv_paths:
        fieldnames, rows = load_csv(path)
        if 'emu_id' not in fieldnames:
            continue
        utc_columns = [c for c in fieldnames if is_utc_column(c)]
        start_columns = [c for c in utc_columns if is_start_utc_column(c)]

        for row in rows:
            emu_id = row.get('emu_id')
            if not emu_id:
                continue
            for col in start_columns:
                dt = parse_utc(row.get(col))
                if dt is not None:
                    preferred_dates.setdefault(emu_id, []).append(dt)
            for col in utc_columns:
                dt = parse_utc(row.get(col))
                if dt is not None:
                    fallback_dates.setdefault(emu_id, []).append(dt)

    offsets = {}
    all_emu_ids = set(preferred_dates) | set(fallback_dates)
    for emu_id in all_emu_ids:
        dates = preferred_dates.get(emu_id) or fallback_dates.get(emu_id)
        offsets[emu_id] = EPOCH - min(dates)
    return offsets


def redact_row(row, fieldnames, offset):
    for col in fieldnames:
        value = row.get(col)
        if not value:
            continue
        if is_utc_column(col):
            dt = parse_utc(value)
            if dt is not None:
                row[col] = (dt + offset).isoformat()
        elif is_file_column(col):
            row[col] = '' if is_full_path(value) else shift_brk_date(value, offset)


def redact_folder(input_dir, output_dir):
    csv_paths = [
        os.path.join(input_dir, name)
        for name in sorted(os.listdir(input_dir))
        if name.lower().endswith('.csv')
    ]
    if not csv_paths:
        print(f'No CSV files found in {input_dir}')
        return

    print(f'Found {len(csv_paths)} CSV file(s): {[os.path.basename(p) for p in csv_paths]}')
    offsets = compute_patient_offsets(csv_paths)
    print(f'Computed a per-patient offset for {len(offsets)} patient(s)')

    os.makedirs(output_dir, exist_ok=True)

    for path in csv_paths:
        fieldnames, rows = load_csv(path)
        if 'emu_id' in fieldnames:
            for row in rows:
                offset = offsets.get(row.get('emu_id'), timedelta(0))
                redact_row(row, fieldnames, offset)
        else:
            print(f'  {os.path.basename(path)}: no emu_id column, left untouched')

        name, ext = os.path.splitext(os.path.basename(path))
        out_path = os.path.join(output_dir, f'{name}_safe{ext}')
        with open(out_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f'  Wrote {out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Redact every data_coverage.py output CSV in a folder for sharing.')
    parser.add_argument('input_dir')
    parser.add_argument('output_dir')
    args = parser.parse_args()
    redact_folder(args.input_dir, args.output_dir)
