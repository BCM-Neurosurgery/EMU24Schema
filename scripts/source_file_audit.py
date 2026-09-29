"""
Standalone diagnostic script: randomly sample a custom number of pre-stitch source chunk files
(NS3Chunks/NS5Chunks, NSP_ID=2 - NEV is excluded, since its event-packet timestamps aren't a
reliable proxy for a file's actual data start/end, same caveat as stitched_file_audit.py) and
record each sampled file's approximate UTC data start/end and its last-modified time. Data
start/end come from the file's own packet timestamps, anchored via its own TimeOrigin - reusing
data_coverage.py's iter_file_packets/PTP-tick anchoring (first packet's tick is that file's own
zero-reference), rather than reimplementing it. A file that fails DataJoint's own fetch (corrupt)
or can't otherwise be read is logged as an error and recorded with the failure in `issue`, not
skipped silently. File paths themselves aren't recorded in the output, same as
stitched_file_audit.py - only emu_id/filetype/toc_id/chunk_id identify each sampled file.

Rerunning against an existing output file draws --n MORE files and appends them, excluding
whatever (emu_id, filetype, toc_id, chunk_id) combinations are already in it - so repeated runs
grow the sample without re-auditing (or duplicating) a file already covered.

--untrustworthy, if given, points to discontinuity_corrections.py's untrustworthy_ranges.csv -
any chunk file whose (emu_id, toc_id, chunk_id) falls inside a logged untrustworthy range (see
UNTRUSTWORTHY_RANGE_CALCULATORS there) is excluded from the sample pool, since its own
data_start_utc/data_end_utc here are derived from the same BRK ticks that range is untrustworthy
about - including one would quietly corrupt the collection-to-availability delay measurement this
audit feeds into (see collection_delay.ipynb).

Usage:
    python scripts/source_file_audit.py --n 100 [--seed 0] [--out source_file_audit.csv]
        [--untrustworthy untrustworthy_ranges.csv]
"""

import argparse
import csv
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path

import datajoint as dj
from datajoint.errors import DataJointError
from tqdm import tqdm

from data_coverage import ChunkFile, iter_file_packets

NSP_ID = 2
CHUNK_TABLES = [('NS3Chunks', 'ns3_file', 'ns3'), ('NS5Chunks', 'ns5_file', 'ns5')]

FIELDS = [
    'emu_id', 'filetype', 'toc_id', 'chunk_id',
    'last_modified_utc', 'data_start_utc', 'data_end_utc', 'issue',
]


def load_untrustworthy_ranges(path):
    """{(patient_id, admission_id, toc_id): [(first_chunk, last_chunk), ...]} from
    discontinuity_corrections.py's untrustworthy_ranges.csv - a candidate chunk file is excluded
    from sampling if its own chunk_id falls in one of these ranges for its (admission, toc) (see
    is_untrustworthy). Keyed by patient_id/admission_id, not emu_id alone: toc_id/chunk_id are
    scoped per admission, not globally unique per patient, so a patient with multiple admissions
    needs admission_id to avoid matching one admission's range against another's chunks. Returns
    {} (nothing excluded) if path is None or doesn't exist yet."""
    if not path or not os.path.exists(path):
        return {}
    ranges = {}
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            key = (row['patient_id'], row['admission_id'], row['toc_id_before'])
            ranges.setdefault(key, []).append((int(row['first_chunk']), int(row['last_chunk'])))
    return ranges


def is_untrustworthy(patient_id, admission_id, toc_id, chunk_id, untrustworthy_ranges):
    return any(
        first <= chunk_id <= last
        for first, last in untrustworthy_ranges.get((str(patient_id), str(admission_id), str(toc_id)), [])
    )


def fetch_candidates(schema_module, already_sampled, untrustworthy_ranges):
    """Every NSP_ID=2 NS3/NS5 chunk file, across all patients, as (filetype, file_attr, row)
    tuples to sample from - row carries emu_id plus the key fields needed to re-query the DB.
    Excludes anything already keyed in already_sampled (see load_sampled_keys), and anything
    falling inside untrustworthy_ranges (see load_untrustworthy_ranges/is_untrustworthy)."""
    Patient = schema_module.Patient
    candidates = []
    excluded_untrustworthy = 0
    for table_name, file_attr, filetype in CHUNK_TABLES:
        table = getattr(schema_module, table_name)()
        rows = (table * Patient & {'nsp_id': NSP_ID}).fetch(
            'patient_id', 'emu_id', 'admission_id', 'toc_id', 'chunk_id', 'nsp_id', as_dict=True
        )
        for row in rows:
            key = (str(row['emu_id']), filetype, str(row['toc_id']), str(row['chunk_id']))
            if key in already_sampled:
                continue
            if is_untrustworthy(row['patient_id'], row['admission_id'], row['toc_id'], row['chunk_id'], untrustworthy_ranges):
                excluded_untrustworthy += 1
                continue
            candidates.append((table_name, file_attr, filetype, row))
    if excluded_untrustworthy:
        print(f'Excluded {excluded_untrustworthy} candidate(s) falling inside an untrustworthy time range')
    return candidates


def load_sampled_keys(out_path):
    """(emu_id, filetype, toc_id, chunk_id) for every row already in the output CSV, so a rerun
    samples only from files not already audited, and never re-adds a duplicate."""
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline='') as f:
        return {
            (row['emu_id'], row['filetype'], row['toc_id'], row['chunk_id'])
            for row in csv.DictReader(f)
        }


def audit_file(schema_module, table_name, file_attr, key_dict):
    """(last_modified_utc, data_start_utc, data_end_utc, issue) for one source chunk file,
    isoformat strings (or None on failure) - same corruption/missing-file handling as
    stitched_file_audit.py."""
    table = getattr(schema_module, table_name)()
    issues = []

    try:
        path = (table & key_dict).fetch1(file_attr)
    except (DataJointError, OSError) as e:
        logging.error(f'{file_attr} fetch failed for {key_dict}: {e}')
        return None, None, None, f'{file_attr}: {e}'

    local_path = Path(path)
    try:
        last_modified_utc = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError as e:
        issues.append(f'file missing or unreadable: {e}')
        last_modified_utc = None

    data_start_utc = None
    data_end_utc = None
    try:
        chunk = ChunkFile(
            toc_id=int(key_dict['toc_id']), chunk_id=int(key_dict['chunk_id']),
            filepath=path, toc_base_file='',
        )
        packets = list(iter_file_packets(chunk))
        if packets:
            data_start_utc = packets[0].utc_start.isoformat()
            data_end_utc = packets[-1].utc_end.isoformat()
        else:
            issues.append('no packets found in file')
    except Exception as e:
        logging.error(f'could not determine data range for {key_dict}: {e}')
        issues.append(f'could not determine data range: {e}')

    return last_modified_utc, data_start_utc, data_end_utc, '; '.join(issues)


def main(schema_module, n, out_path, seed=None, untrustworthy_path=None):
    # See stitched_file_audit.py: set after connect()/schema import (both already done by the
    # time main() runs), not at module import time, so nothing in that startup sequence can reset
    # it out from under us. Skips the slow content-hash step on every file fetch below.
    dj.config['filepath_checksum_size_limit'] = 0
    logging.getLogger('datajoint').setLevel(logging.ERROR)

    already_sampled = load_sampled_keys(out_path)
    if already_sampled:
        print(f'{len(already_sampled)} file(s) already sampled in {out_path}, excluding from candidate pool')

    untrustworthy_ranges = load_untrustworthy_ranges(untrustworthy_path)
    if untrustworthy_path and not untrustworthy_ranges:
        print(f'Warning: no untrustworthy ranges loaded from {untrustworthy_path} - nothing will be excluded on that basis')

    candidates = fetch_candidates(schema_module, already_sampled, untrustworthy_ranges)
    print(f'{len(candidates)} NSP_ID={NSP_ID} source chunk file(s) available to sample from')

    sample = random.Random(seed).sample(candidates, min(n, len(candidates)))
    print(f'Sampling {len(sample)} file(s)')

    out_exists = os.path.exists(out_path) and os.path.getsize(out_path) > 0

    with open(out_path, 'a' if out_exists else 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not out_exists:
            writer.writeheader()

        for table_name, file_attr, filetype, row in tqdm(sample, desc='Auditing source files'):
            key_dict = {k: row[k] for k in ('patient_id', 'admission_id', 'toc_id', 'chunk_id', 'nsp_id')}
            last_modified_utc, data_start_utc, data_end_utc, issue = audit_file(
                schema_module, table_name, file_attr, key_dict
            )
            writer.writerow({
                'emu_id': row['emu_id'],
                'filetype': filetype,
                'toc_id': row['toc_id'],
                'chunk_id': row['chunk_id'],
                'last_modified_utc': last_modified_utc,
                'data_start_utc': data_start_utc,
                'data_end_utc': data_end_utc,
                'issue': issue,
            })

    print(f'Wrote {len(sample)} row(s) to {out_path} this run')


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description='Randomly sample NS3/NS5 source chunk files and record their data start/end and last-modified time.',
        add_help=False,
    )
    arg_parser.add_argument('--n', type=int, default=100, help='Number of files to sample')
    arg_parser.add_argument('--seed', type=int, default=None, help='Random seed for reproducible sampling')
    arg_parser.add_argument('--out', type=str, default='source_file_audit.csv')
    arg_parser.add_argument(
        '--untrustworthy', type=str, default=None,
        help='Path to discontinuity_corrections.py\'s untrustworthy_ranges.csv - excludes any chunk file '
             'falling inside a logged untrustworthy time range from the sample pool'
    )
    args = arg_parser.parse_args()

    connect(args)
    import emu24.schema as schema_module

    main(schema_module, args.n, args.out, args.seed, args.untrustworthy)
