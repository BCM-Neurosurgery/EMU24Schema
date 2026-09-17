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

Usage:
    python scripts/stitched_file_audit.py [--out stitched_file_audit.csv]
"""

import argparse
import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import datajoint as dj
from brpylib import NsxFile
from datajoint.errors import DataJointError
from pyNsXStitch.helpers import get_nsx_start_timestamp
from tqdm import tqdm

# Skips the slow content-hash step on every fetch (see module docstring) - the unconditional size
# check that raises on a genuinely corrupt/replaced file still runs regardless of this setting.
dj.config['filepath_checksum_size_limit'] = 0
# ...which makes DataJoint log a "Skipped checksum" WARNING on every successful fetch - expected
# and not useful here, since skipping it is intentional.
logging.getLogger('datajoint').setLevel(logging.ERROR)

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


def audit(schema_module):
    table = schema_module.StitchedChunks()
    heading = table.heading
    plain_attrs = [n for n in heading.names if not heading.attributes[n].is_filepath]
    pk_attrs = heading.primary_key
    chunk_identifiers = schema_module.StitchedChunks.chunk_identifiers

    rows = table.fetch(*plain_attrs, as_dict=True)

    for row in tqdm(rows, desc='Auditing stitched tasks', miniters=10):
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
            except DataJointError as e:
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
            out_row['data_start_utc'] = None
            out_row['data_end_utc'] = None
            issues.append(f'could not determine data range: {e}')

        out_row['issue'] = '; '.join(issues)
        yield out_row


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description='Audit whether stitched task files are corrupt, their last-modified time, and approximate data range.',
        add_help=False,
    )
    arg_parser.add_argument('--out', type=str, default='stitched_file_audit.csv')
    args = arg_parser.parse_args()

    connect(args)
    import emu24.schema as schema_module

    out_rows = list(audit(schema_module))

    # StitchedChunks' own non-filepath attribute names are discovered at runtime (see audit()),
    # so the column order is only known once we have at least one row.
    base_fields = [k for k in out_rows[0].keys() if k not in ADDED_FIELDS] if out_rows else []
    fieldnames = base_fields + ADDED_FIELDS

    with open(args.out, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f'Wrote {len(out_rows)} row(s) to {args.out}')