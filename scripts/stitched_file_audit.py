"""
Standalone analysis script: for every stitched task (StitchedChunks row), record whether any of
its output files are corrupt, its last-modified time, and its approximate UTC data start/end.

One row per task: a task's nev/ns3/ns5 files are cut/verified together, so corrupt and
last_modified_utc are single per-task values (a task is corrupt if ANY of its present files are;
last_modified_utc is the latest mtime among them), not one set per filetype. Per-file paths are
still reported, purely as identifying info for locating a flagged file.

The data range comes from the raw start/stop task-comment timestamps (StartComments.timestamp/
StopComments.timestamp - the exact ticks used to define the stitching boundaries when the file
was created, per StitchedChunks.make()/do_stitching), not by re-scanning the stitched file's own
packets. Each comment's originating raw chunk (patient_id/admission_id/toc_id/nsp_id/chunk_id,
looked up via TaskComments) is resolved to its NS3/NS5 file (preferring ns5, matching this
pipeline's convention elsewhere - see data_coverage.py), whose header TimeOrigin anchors the
comment's raw tick to UTC the same way data_coverage.py anchors packet ticks: PTP ticks (FileSpec
>=3) are an absolute, continuously-running counter, not reset to 0 per file, so
pyNsXStitch.helpers.get_nsx_start_timestamp (tested elsewhere in this pipeline) gives that chunk
file's own zero-reference tick. NEV is never used for timing - it holds event packets, not a
uniform sample stream, so its own timestamps aren't a reliable proxy for a task's actual
start/end.

Corruption check: this script exists to find out whether stitched files are corrupt, so every
filepath column touched (StitchedChunks.nev_file/ns3_file/ns5_file, and the original chunk's
ns3_file/ns5_file used for the data range) is fetched the normal DataJoint way -
(table & key).fetch1(attr) - which verifies on-disk SIZE against what DataJoint recorded at
insert time (that check runs unconditionally on every fetch, regardless of
filepath_checksum_size_limit - see datajoint.external.ExternalTable._need_checksum).
filepath_checksum_size_limit is kept at 0 so the slower content-hash step is skipped - full
verification of every file would be prohibitively slow, and size mismatch is what actually raises
in practice. A file that fails this is recorded as corrupt/an issue and simply skipped - no
round-about fallback path resolution - since file issues have turned out to be rare enough not to
need special-casing.

Processed and resumable per patient, same as data_coverage.py: if the output file already exists,
any patient with a row already in it is treated as fully processed and skipped, and new rows are
appended rather than overwriting. If a previous run died partway through a patient, that patient's
rows are never partially written (each patient's rows are buffered in memory and only written
once every one of their stitched tasks has been audited), so resuming is always safe.

Usage:
    python scripts/stitched_file_audit.py [--patient EMU-ID] [--exclude EMU-ID]
        [--out stitched_file_audit.csv]
"""

import argparse
import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import datajoint as dj
from brpylib import NsxFile
from datajoint.errors import DataJointError
from pyNsXStitch.helpers import get_nsx_start_timestamp
from tqdm import tqdm

FILE_ATTRS = {'nev_file': 'nev', 'ns3_file': 'ns3', 'ns5_file': 'ns5'}
ADDED_FIELDS = [
    'nev_path', 'ns3_path', 'ns5_path',
    'last_modified_utc', 'corrupt',
    'data_start_utc', 'data_end_utc', 'issue',
]


def chunk_time_origin(schema_module, chunk_identifiers, chunk_key):
    """
    Resolve one raw chunk (identified by chunk_identifiers/chunk_key, from a TaskComments row) to
    its NS3/NS5 file - preferring ns5, matching this pipeline's convention elsewhere - and return
    (origin_utc, ts_resolution, first_tick) from that file's own header.

    :raises LookupError: if the chunk has neither an NS3 nor NS5 file recorded
    :raises DataJointError: if the file that does exist fails DataJoint's own fetch (corrupt)
    """
    key_dict = dict(zip(chunk_identifiers, chunk_key))
    for table_name, file_attr in [('NS5Chunks', 'ns5_file'), ('NS3Chunks', 'ns3_file')]:
        table = getattr(schema_module, table_name)()
        restricted = table & key_dict
        if not restricted:
            continue

        path = restricted.fetch1(file_attr)
        nsx = NsxFile(path, verbose=False, interactive=False)
        try:
            origin_utc = nsx.basic_header['TimeOrigin']
            ts_resolution = nsx.basic_header['TimeStampResolution']
        finally:
            nsx.datafile.close()
        first_tick = get_nsx_start_timestamp(path)
        return origin_utc, ts_resolution, first_tick

    raise LookupError('no NS3/NS5 file recorded for this chunk')


def comment_utc(schema_module, chunk_identifiers, comment_id):
    """Approximate UTC of one TaskComments row's raw timestamp, anchored via its originating
    chunk's own NS3/NS5 TimeOrigin (see module docstring)."""
    TaskComments = schema_module.TaskComments
    raw_ts, *chunk_key = (TaskComments & {'comment_id': int(comment_id)}).fetch1(
        'timestamp', *chunk_identifiers
    )
    origin_utc, ts_resolution, first_tick = chunk_time_origin(schema_module, chunk_identifiers, chunk_key)
    return origin_utc + timedelta(seconds=(raw_ts - first_tick) / ts_resolution)


def fetch_patients(schema_module, patient_emu_id=None, exclude_emu_ids=None):
    """Every patient with at least one StitchedChunks row, as (patient_id, emu_id) dicts."""
    query = schema_module.Patient
    if patient_emu_id:
        query = query & f'emu_id="{patient_emu_id}"'
    for excluded in exclude_emu_ids or []:
        query = query & f'emu_id!="{excluded}"'
    rows = query.fetch('patient_id', 'emu_id', as_dict=True)
    return rows


def load_completed_patients(out_path):
    """
    Patients that already have a row in the output CSV are treated as fully done.

    A patient's rows are only ever written (see main) after every one of their stitched tasks has
    been audited without error, so this is a safe completion marker even if a previous run died
    partway through a patient - no partial rows for that patient are ever on disk to begin with.
    """
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline='') as f:
        return {int(row['patient_id']) for row in csv.DictReader(f)}


def audit_patient(schema_module, patient_id):
    table = schema_module.StitchedChunks()
    heading = table.heading
    plain_attrs = [n for n in heading.names if not heading.attributes[n].is_filepath]
    pk_attrs = heading.primary_key
    chunk_identifiers = schema_module.StitchedChunks.chunk_identifiers

    rows = (table & {'patient_id': patient_id}).fetch(*plain_attrs, as_dict=True)

    for row in tqdm(rows, desc=f'Auditing patient {patient_id}', miniters=10):
        key_dict = {p: row[p] for p in pk_attrs}
        out_row = dict(row)
        issues = []
        mtimes = []
        corrupt = False

        for file_attr, filetype in FILE_ATTRS.items():
            if file_attr not in heading.names:
                continue

            try:
                value = (table & key_dict).fetch1(file_attr)
            except (DataJointError, OSError) as e:
                # DataJointError covers a corrupt/replaced file (size or hash mismatch).
                # OSError (FileNotFoundError included) covers a file that's missing outright -
                # DataJoint's own checksum check does Path(local_filepath).stat() internally and
                # raises that directly, before it ever gets a chance to raise a DataJointError.
                logging.error(f'{filetype} fetch failed for {key_dict}: {e}')
                out_row[f'{filetype}_path'] = None
                corrupt = True
                issues.append(f'{filetype}: {e}')
                continue

            if value is None:
                out_row[f'{filetype}_path'] = None
                continue

            local_path = Path(value)
            out_row[f'{filetype}_path'] = str(local_path)
            try:
                mtimes.append(local_path.stat().st_mtime)
            except OSError as e:
                issues.append(f'{filetype} file missing or unreadable: {e}')

        out_row['last_modified_utc'] = (
            datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat() if mtimes else None
        )
        out_row['corrupt'] = corrupt

        try:
            start_utc = comment_utc(schema_module, chunk_identifiers, row['start_tid'])
            end_utc = comment_utc(schema_module, chunk_identifiers, row['stop_tid'])
            out_row['data_start_utc'] = start_utc.isoformat()
            out_row['data_end_utc'] = end_utc.isoformat()
        except Exception as e:
            # Already crash-safe (bare Exception covers FileNotFoundError/OSError too), but the
            # failure was only ever recorded in the CSV's issue column - log it too.
            logging.error(f'could not determine data range for {key_dict}: {e}')
            out_row['data_start_utc'] = None
            out_row['data_end_utc'] = None
            issues.append(f'could not determine data range: {e}')

        out_row['issue'] = '; '.join(issues)
        yield out_row


def main(schema_module, out_path, patient_emu_id=None, exclude_emu_ids=None):
    # Skips the slow content-hash step on every fetch of a stitched (nev/ns3/ns5_file) or raw
    # task (NS3Chunks/NS5Chunks) file - the unconditional size check that raises on a genuinely
    # corrupt/replaced file still runs regardless of this setting. Set here, right before any
    # fetch happens and after connect()/the schema import have both already run, rather than at
    # module import time, so nothing later in that startup sequence can reset it out from under us.
    dj.config['filepath_checksum_size_limit'] = 0
    print(f"filepath_checksum_size_limit={dj.config['filepath_checksum_size_limit']} (file hashing disabled)")
    # ...which would otherwise make DataJoint log a "Skipped checksum" WARNING on every successful
    # fetch - expected and not useful here, since skipping it is intentional.
    logging.getLogger('datajoint').setLevel(logging.ERROR)

    patients = fetch_patients(schema_module, patient_emu_id, exclude_emu_ids)

    completed_patients = load_completed_patients(out_path)
    if completed_patients:
        print(f'Resuming: {len(completed_patients)} patient(s) already have rows in {out_path}, skipping them')

    out_exists = os.path.exists(out_path)
    total_written = 0

    with open(out_path, 'a' if out_exists else 'w', newline='') as f:
        writer = None
        for patient in patients:
            if patient['patient_id'] in completed_patients:
                print(f"Skipping patient {patient['emu_id']} (already complete)")
                continue

            print(f"Auditing patient {patient['emu_id']}...")
            patient_rows = list(audit_patient(schema_module, patient['patient_id']))
            if not patient_rows:
                print('  No stitched tasks found, skipping')
                continue

            if writer is None:
                # StitchedChunks' own non-filepath attribute names are discovered at runtime (see
                # audit_patient()), so the column order is only known once we have a row.
                base_fields = [k for k in patient_rows[0].keys() if k not in ADDED_FIELDS]
                writer = csv.DictWriter(f, fieldnames=base_fields + ADDED_FIELDS)
                if not out_exists:
                    writer.writeheader()

            writer.writerows(patient_rows)
            f.flush()
            total_written += len(patient_rows)
            print(f'  {len(patient_rows)} row(s) written')

    print(f'\nWrote {total_written} row(s) to {out_path} this run')


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description='Audit whether stitched task files are corrupt, their last-modified time, and approximate data range.',
        add_help=False,
    )
    arg_parser.add_argument('--patient', type=str, help='Restrict to the patient with this EMU identifier')
    arg_parser.add_argument(
        '--exclude', type=str, action='append', default=[], metavar='EMU-ID',
        help='Exclude the patient with this EMU identifier from the analysis (repeatable)'
    )
    arg_parser.add_argument('--out', type=str, default='stitched_file_audit.csv')
    args = arg_parser.parse_args()

    connect(args)
    import emu24.schema as schema_module

    main(schema_module, args.out, args.patient, args.exclude)