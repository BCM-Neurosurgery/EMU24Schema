"""
Standalone diagnostic script: list every NEV/NS3/NS5 chunk file whose external-storage record
(DataJoint's own tracking of size and content hash, from when the file was first inserted)
no longer matches what's actually on disk.

This deliberately never calls .fetch() on a filepath-typed column (nev_file/ns3_file/ns5_file):
DataJoint raises a DataJointError the moment such a fetch hits a size mismatch (see
datajoint.external.ExternalTable._need_checksum), which is exactly the crash this script exists
to survey around. Instead, each chunk table's raw stored UUID is joined directly, in SQL, against
the external tracking table's own `hash` column - both are plain binary(16) values (a filepath
attribute is literally declared as a SQL foreign key to that column, see
datajoint.declare.substitute_special_type), so this read never goes through DataJoint's
checksum-on-fetch logic at all.

By default only file SIZE is checked against the recorded value (cheap: one stat() per file, and
it's the specific check that was raising during normal processing). Pass --deep to also compute
each file's actual content hash and compare it to the recorded contents_hash - much slower (reads
every byte of every file), but catches a file that was replaced with different content of the
same size, which a size-only pass cannot.

Output is one CSV row per mismatched file, with patient_id/emu_id/admission_id/toc_id/chunk_id/
nsp_id columns matching the schema's own patient -> admission -> TOC -> chunk/nsp organization.

Usage:
    python scripts/find_hash_mismatches.py [--out mismatched_files.csv] [--deep]
"""

import argparse
import csv
from pathlib import Path

from datajoint.hash import uuid_from_file
from tqdm import tqdm

CHUNK_TABLES = [
    ('nev', 'NEVChunks', 'nev_file'),
    ('ns3', 'NS3Chunks', 'ns3_file'),
    ('ns5', 'NS5Chunks', 'ns5_file'),
]
STORE = 'Ext_Chunk'

FIELDS = [
    'filetype', 'patient_id', 'emu_id', 'admission_id', 'toc_id', 'chunk_id', 'nsp_id',
    'relative_path', 'recorded_size', 'actual_size', 'issue',
]


def find_mismatches(schema_module, deep=False):
    """Yield one dict per chunk file whose recorded size (and, if deep, content hash) disagrees
    with what's actually on disk, or that's missing/unreadable entirely."""
    Patient = schema_module.Patient
    ext = schema_module.schema.external[STORE]
    stage = Path(ext.spec['stage'])

    for filetype, table_name, file_attr in CHUNK_TABLES:
        table = getattr(schema_module, table_name)()
        sql = f"""
            SELECT c.patient_id, p.emu_id, c.admission_id, c.toc_id, c.chunk_id, c.nsp_id,
                   ext.size AS recorded_size, ext.contents_hash AS recorded_hash,
                   ext.filepath AS relative_path
            FROM {table.full_table_name} AS c
            JOIN {Patient().full_table_name} AS p ON c.patient_id = p.patient_id
            JOIN {ext.full_table_name} AS ext ON c.`{file_attr}` = ext.`hash`
        """
        rows = table.connection.query(sql, as_dict=True).fetchall()

        for row in tqdm(rows, desc=f'Checking {filetype} files', miniters=100):
            base = {
                'filetype': filetype,
                'patient_id': row['patient_id'],
                'emu_id': row['emu_id'],
                'admission_id': row['admission_id'],
                'toc_id': row['toc_id'],
                'chunk_id': row['chunk_id'],
                'nsp_id': row['nsp_id'],
                'relative_path': row['relative_path'],
                'recorded_size': row['recorded_size'],
            }
            local_path = stage / row['relative_path']

            try:
                actual_size = local_path.stat().st_size
            except OSError as e:
                yield {**base, 'actual_size': None, 'issue': f'file missing or unreadable: {e}'}
                continue

            if actual_size != row['recorded_size']:
                yield {**base, 'actual_size': actual_size, 'issue': 'size mismatch'}
            elif deep and uuid_from_file(local_path) != row['recorded_hash']:
                yield {**base, 'actual_size': actual_size, 'issue': 'content hash mismatch (same size)'}


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description='List every NEV/NS3/NS5 chunk file whose recorded size/hash disagrees with disk.',
        add_help=False,
    )
    arg_parser.add_argument('--out', type=str, default='mismatched_files.csv')
    arg_parser.add_argument(
        '--deep', action='store_true',
        help='Also verify content hash for files whose size matches (slow: reads every byte)'
    )
    args = arg_parser.parse_args()

    connect(args)
    import emu24.schema as schema_module

    with open(args.out, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        n = 0
        for row in find_mismatches(schema_module, deep=args.deep):
            writer.writerow(row)
            n += 1
            print(f"  {row['issue']}: {row['filetype']} patient={row['emu_id']} "
                  f"admission={row['admission_id']} toc={row['toc_id']} chunk={row['chunk_id']} "
                  f"nsp={row['nsp_id']}")

    print(f'\n{n} mismatched file(s) written to {args.out}')
