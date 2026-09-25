"""
Standalone analysis script: measure recording coverage/gaps for patients in the emu24 schema.

For each admission, every NS5 data packet (across all TOCInstances/chunks) on NSP_ID is listed
out and sorted in time; a point where one packet ends before the next one starts is a gap.

UTC anchoring: BRK ticks are only comparable within one TOCInstance. Each TOC is anchored ONCE,
from its own chronologically-first chunk file's TimeOrigin - every other file in that TOC converts
ticks using that single shared origin (via pyNsXStitch.helpers.brk_toc_ticks_to_utc), never its own
TimeOrigin, since a file's own TimeOrigin can carry its own small clock latency/inaccuracy.

Insane gaps: a gap that's negative (beyond float rounding) or bigger than MAX_SANE_OFFSET_SECONDS
means the underlying tick/TimeOrigin was corrupted, not a real gap. These are flagged `insane=True`
in gap_summary.csv with the raw (nonsensical) value left in `duration_hours` - correcting them is a
deferred second pass, not done here. A gap of 0-1 ticks at a file cut is a normal artifact, not
reported at all.

Only NS5 on a single NSP is used. Assumes all NS5 files are FileSpec 3.0 (PTP, 64-bit timestamps).

Resumable: if visit_out_path already exists, any patient with a row in it is skipped, and new rows
are appended. If a previous run died partway through a patient, that patient's partial gap_summary
rows must be removed by hand before resuming (visit_summary.csv is the completion marker).

Usage:
    python scripts/data_coverage.py [--patient EMU-ID] [--exclude EMU-ID]
        [--gap-out gap_summary.csv] [--visit-out visit_summary.csv]
"""

import argparse
import csv
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

import datajoint as dj
from brpylib import NsxFile
from pyNsXStitch.helpers import brk_toc_ticks_to_utc, get_nsx_start_timestamp
from pyNsXStitch.streamers import iter_nsx_timestamps
from tqdm import tqdm

NSP_ID = 2

# The datalake is read-only, so skip DataJoint's per-fetch content checksum on filepath attributes
# (ns5_file) - without this, fetching a chunk's filepath reads and hashes the entire file.
dj.config['filepath_checksum_size_limit'] = 0
logging.getLogger('datajoint').setLevel(logging.ERROR)


@dataclass
class ChunkFile:
    """One NS5 chunk file, identified by its place in the DataJoint schema."""
    toc_id: int
    chunk_id: int
    filepath: str
    toc_base_file: str  # TOCInstance.base_file - human-readable session identifier


TOC_BASE_FILE_DATE_RE = re.compile(r'\d{8}-\d{6}')
TOC_BASE_FILE_DATE_FMT = '%Y%m%d-%H%M%S'


def parse_toc_base_file_dt(base_file):
    """Extract the YYYYMMDD-HHMMSS timestamp embedded in a TOCInstance.base_file label, if any."""
    match = TOC_BASE_FILE_DATE_RE.search(base_file) if base_file else None
    return datetime.strptime(match.group(), TOC_BASE_FILE_DATE_FMT) if match else None


def determine_toc_order(chunks):
    """
    Chronological order of the TOCs in one admission, by each TOC's own base_file label (not
    toc_id - unreliable for some early admissions). A TOC with a missing/unparseable base_file
    sorts earliest, tiebroken by toc_id.

    :return: (toc_order, mismatch - True if this differs from plain toc_id order, base_file_by_toc)
    """
    base_file_by_toc = {}
    for chunk in chunks:
        base_file_by_toc.setdefault(chunk.toc_id, chunk.toc_base_file)

    def label_sort_key(toc_id):
        return (parse_toc_base_file_dt(base_file_by_toc[toc_id]) or datetime.min, toc_id)

    toc_order = sorted(base_file_by_toc, key=label_sort_key)
    mismatch = toc_order != sorted(base_file_by_toc)
    return toc_order, mismatch, base_file_by_toc


@dataclass
class Packet:
    """One data packet read out of an NS5 file, with its source chunk attached."""
    chunk: ChunkFile
    brk_start: int
    brk_end: int
    ts_resolution: int
    utc_start: datetime
    utc_end: datetime


# A raw BRK tick this far from a TOC's own origin can't be real - the packet header was misread
# (e.g. byte-misaligned after an earlier corrupted packet). Also the insane-gap threshold below.
MAX_SANE_OFFSET_SECONDS = 100 * 365 * 24 * 3600  # 100 years
ROUNDING_TOLERANCE_SECONDS = 1e-6  # sub-microsecond float noise, not a real negative gap
FILE_EDGE_TICK_TOLERANCE = 1  # a 0-1 tick gap at a file cut is a normal artifact, not a real gap


def _safe_utc(tick, origin_utc, first_tick, ts_resolution):
    """brk_toc_ticks_to_utc(tick), clamped to datetime.min/max instead of raising when the offset
    is too large to represent as a datetime at all (see MAX_SANE_OFFSET_SECONDS)."""
    offset_seconds = (tick - first_tick) / ts_resolution
    if abs(offset_seconds) > MAX_SANE_OFFSET_SECONDS:
        return datetime.max if offset_seconds > 0 else datetime.min
    return brk_toc_ticks_to_utc(tick, toc_start_utc=origin_utc, toc_start_brk=first_tick, toc_ts_freq=ts_resolution)


def toc_origin(chunks_for_toc):
    """The shared UTC anchor for every packet in one TOC: (origin_utc, ts_resolution, first_tick),
    read from the TOC's own chronologically-first chunk file (lowest chunk_id) only."""
    first_chunk = min(chunks_for_toc, key=lambda c: c.chunk_id)
    nsx_file = NsxFile(first_chunk.filepath, verbose=False, interactive=False)
    try:
        origin_utc = nsx_file.basic_header['TimeOrigin']
        ts_resolution = nsx_file.basic_header['TimeStampResolution']
    finally:
        nsx_file.datafile.close()
    first_tick = get_nsx_start_timestamp(first_chunk.filepath)
    return origin_utc, ts_resolution, first_tick


def iter_file_packets(chunk: ChunkFile, origin_utc, ts_resolution, first_tick):
    """Yield one Packet per data packet in chunk's file, anchored to the TOC-shared
    (origin_utc, ts_resolution, first_tick) from toc_origin - not this file's own header."""
    nsx_file = NsxFile(chunk.filepath, verbose=False, interactive=False)
    try:
        sample_freq = nsx_file.basic_header['SampleResolution'] / nsx_file.basic_header['Period']
        ticks_per_sample = ts_resolution / sample_freq
        for brk_start, n_points in iter_nsx_timestamps(nsx_file):
            brk_end = brk_start + int(round(n_points * ticks_per_sample))
            yield Packet(
                chunk=chunk,
                brk_start=brk_start,
                brk_end=brk_end,
                ts_resolution=ts_resolution,
                utc_start=_safe_utc(brk_start, origin_utc, first_tick, ts_resolution),
                utc_end=_safe_utc(brk_end, origin_utc, first_tick, ts_resolution),
            )
    finally:
        nsx_file.datafile.close()


def toc_packets(chunks_for_toc):
    """Every packet in one TOC, sorted by (chunk_id, brk_start), all anchored to that TOC's own
    shared origin (see toc_origin)."""
    origin_utc, ts_resolution, first_tick = toc_origin(chunks_for_toc)
    packets = []
    for chunk in sorted(chunks_for_toc, key=lambda c: c.chunk_id):
        packets.extend(iter_file_packets(chunk, origin_utc, ts_resolution, first_tick))
    packets.sort(key=lambda p: (p.chunk.chunk_id, p.brk_start))
    return packets


def classify_cause(before: Packet, after: Packet) -> str:
    if before.chunk.toc_id != after.chunk.toc_id:
        return 'TOC reset'
    elif before.chunk.chunk_id != after.chunk.chunk_id:
        return 'file edge'
    else:
        return 'other'


def is_insane(duration_seconds: float) -> bool:
    return duration_seconds < -ROUNDING_TOLERANCE_SECONDS or duration_seconds > MAX_SANE_OFFSET_SECONDS


def find_gap(before: Packet, after: Packet, cause: str):
    """
    Determine whether there's a gap between two time-adjacent packets: BRK ticks (exact) within
    one TOC, UTC otherwise (ticks aren't comparable across a TOC boundary). A negative-beyond-
    rounding or implausibly large duration is flagged `insane` and reported as-is, uncorrected -
    see module docstring.

    :return: None if no gap, else a dict of the fields to attach to a Gap
    """
    same_toc = before.chunk.toc_id == after.chunk.toc_id
    if same_toc:
        tick_diff = after.brk_start - before.brk_end
        if cause == 'file edge' and 0 <= tick_diff <= FILE_EDGE_TICK_TOLERANCE:
            return None
        duration_seconds = tick_diff / before.ts_resolution
        method = 'brk'
    else:
        duration_seconds = (after.utc_start - before.utc_end).total_seconds()
        method = 'utc'

    insane = is_insane(duration_seconds)
    if not insane and duration_seconds <= 0:
        return None

    return {'duration_hours': duration_seconds / 3600, 'detection_method': method, 'insane': insane}


@dataclass
class Gap:
    patient_id: int
    emu_id: str
    admission_id: int
    before: Packet
    after: Packet
    cause: str
    duration_hours: float
    detection_method: str  # 'brk' or 'utc'
    insane: bool


def process_admission(patient_id, emu_id, admission_id, chunks):
    """
    Build the sorted NS5/NSP_ID packet timeline for one admission, TOC by TOC (each on its own
    shared UTC origin - see module docstring), find gaps in it, and compute summary stats.

    :param chunks: every NS5 ChunkFile belonging to this admission (all toc_id, NSP_ID only)
    :return: (gaps, visit_row), or (None, None) if there were no packets at all
    """
    toc_order, toc_order_mismatch, base_file_by_toc = determine_toc_order(chunks)
    if toc_order_mismatch:
        ordered_pairs = list(zip(toc_order, (base_file_by_toc[t] for t in toc_order)))
        logging.warning(
            f'Patient {emu_id} admission {admission_id}: TOC label (base_file) order disagrees '
            f'with toc_id numeric order. Using label order (toc_id, base_file) = {ordered_pairs}.'
        )

    chunks_by_toc = {}
    for chunk in chunks:
        chunks_by_toc.setdefault(chunk.toc_id, []).append(chunk)

    all_packets = []
    for toc_id in tqdm(toc_order, desc='Scanning TOCs'):
        all_packets.extend(toc_packets(chunks_by_toc[toc_id]))

    if not all_packets:
        return None, None

    gaps = []
    for before, after in zip(all_packets, all_packets[1:]):
        cause = classify_cause(before, after)
        gap_info = find_gap(before, after, cause)
        if gap_info is not None:
            gaps.append(Gap(patient_id, emu_id, admission_id, before, after, cause, **gap_info))

    span_start = all_packets[0].utc_start
    span_end = all_packets[-1].utc_end
    total_span_hours = (span_end - span_start).total_seconds() / 3600
    total_gap_hours = sum(g.duration_hours for g in gaps)
    visit_row = {
        'patient_id': patient_id,
        'emu_id': emu_id,
        'admission_id': admission_id,
        'nsp_id': NSP_ID,
        'recording_start_utc': span_start.isoformat(),
        'recording_end_utc': span_end.isoformat(),
        'total_span_hours': total_span_hours,
        'n_gaps': len(gaps),
        'total_gap_hours': total_gap_hours,
        'total_continuous_hours': total_span_hours - total_gap_hours,
        'pct_coverage': 100 * (total_span_hours - total_gap_hours) / total_span_hours if total_span_hours else None,
        'n_insane_gaps': sum(1 for g in gaps if g.insane),
        'toc_order_mismatch': toc_order_mismatch,
    }

    return gaps, visit_row


GAP_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'gap_start_utc', 'gap_end_utc', 'duration_hours', 'detection_method', 'insane',
    'toc_id_before', 'toc_base_file_before', 'chunk_id_before', 'file_before',
    'brk_before_ts', 'brk_before_resolution_hz',
    'toc_id_after', 'toc_base_file_after', 'chunk_id_after', 'file_after',
    'brk_after_ts', 'brk_after_resolution_hz',
    'cause',
]

VISIT_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'recording_start_utc', 'recording_end_utc', 'total_span_hours',
    'n_gaps', 'total_gap_hours', 'total_continuous_hours', 'pct_coverage',
    'n_insane_gaps',
    'toc_order_mismatch',
]


def gap_to_row(gap: Gap) -> dict:
    return {
        'patient_id': gap.patient_id,
        'emu_id': gap.emu_id,
        'admission_id': gap.admission_id,
        'nsp_id': NSP_ID,
        'gap_start_utc': gap.before.utc_end.isoformat(),
        'gap_end_utc': gap.after.utc_start.isoformat(),
        'duration_hours': gap.duration_hours,
        'detection_method': gap.detection_method,
        'insane': gap.insane,
        'toc_id_before': gap.before.chunk.toc_id,
        'toc_base_file_before': gap.before.chunk.toc_base_file,
        'chunk_id_before': gap.before.chunk.chunk_id,
        'file_before': gap.before.chunk.filepath,
        'brk_before_ts': gap.before.brk_end,
        'brk_before_resolution_hz': gap.before.ts_resolution,
        'toc_id_after': gap.after.chunk.toc_id,
        'toc_base_file_after': gap.after.chunk.toc_base_file,
        'chunk_id_after': gap.after.chunk.chunk_id,
        'file_after': gap.after.chunk.filepath,
        'brk_after_ts': gap.after.brk_start,
        'brk_after_resolution_hz': gap.after.ts_resolution,
        'cause': gap.cause,
    }


def fetch_admissions(patient_emu_id=None, exclude_emu_ids=None):
    from emu24.schema import Patient, Admission
    query = Patient * Admission
    if patient_emu_id:
        query = query & f'emu_id="{patient_emu_id}"'
    for excluded in exclude_emu_ids or []:
        query = query & f'emu_id!="{excluded}"'
    rows = query.fetch('patient_id', 'emu_id', 'admission_id', as_dict=True)
    # DataJoint returns numpy scalar types (e.g. numpy.int64) for int attributes - normalize at
    # the fetch boundary, since those repr() ugly (np.int64(43)) once logged inside a tuple/list.
    return [{**row, 'patient_id': int(row['patient_id']), 'admission_id': int(row['admission_id'])} for row in rows]


def fetch_chunks_for_admission(patient_id, admission_id):
    from emu24.schema import TOCInstance, NS5Chunks
    tocs = TOCInstance & f'patient_id={patient_id}' & f'admission_id={admission_id}'

    print(f'  Retrieving data chunks...')
    relevant = (tocs * NS5Chunks & f'nsp_id={NSP_ID}')
    rows = relevant.fetch('toc_id', 'chunk_id', 'ns5_file', 'base_file', as_dict=True)
    return [
        ChunkFile(
            toc_id=int(row['toc_id']),
            chunk_id=int(row['chunk_id']),
            filepath=row['ns5_file'],
            toc_base_file=row['base_file'],
        )
        for row in rows
    ]


def load_completed_patients(visit_out_path):
    """A visit_summary row is only written once an admission finishes without error, so patients
    already in it are a safe completion marker for resuming."""
    if not os.path.exists(visit_out_path):
        return set()
    with open(visit_out_path, newline='') as f:
        return {int(row['patient_id']) for row in csv.DictReader(f)}


def main(patient_emu_id, gap_out_path, visit_out_path, exclude_emu_ids=None):
    admissions = fetch_admissions(patient_emu_id, exclude_emu_ids)
    total_insane = 0
    toc_order_mismatch_patients = set()

    completed_patients = load_completed_patients(visit_out_path)
    if completed_patients:
        print(f'Resuming: {len(completed_patients)} patient(s) already have rows in {visit_out_path}, skipping them')

    for admission in admissions:
        if admission['patient_id'] in completed_patients:
            print(f"Skipping patient {admission['emu_id']} admission {admission['admission_id']} (already complete)")
            continue

        print(f"Processing patient {admission['emu_id']} admission {admission['admission_id']}...")
        chunks = fetch_chunks_for_admission(admission['patient_id'], admission['admission_id'])
        if not chunks:
            print(f'  No NS5 chunks found for NSP_ID={NSP_ID}, skipping')
            continue

        gaps, visit_row = process_admission(
            admission['patient_id'], admission['emu_id'], admission['admission_id'], chunks
        )
        if visit_row is None:
            print('  No data packets found in any NS5 chunk, skipping')
            continue
        print(f"  {visit_row['n_gaps']} gap(s) found ({visit_row['n_insane_gaps']} insane)")
        total_insane += visit_row['n_insane_gaps']
        if visit_row['toc_order_mismatch']:
            toc_order_mismatch_patients.add(admission['emu_id'])

        # Reopened per admission (rather than held open for the whole run) so gap_out_path/
        # visit_out_path are flushed and readable on disk after every admission, not just at exit.
        gap_exists = os.path.exists(gap_out_path)
        visit_exists = os.path.exists(visit_out_path)
        with open(gap_out_path, 'a' if gap_exists else 'w', newline='') as gap_f, \
                open(visit_out_path, 'a' if visit_exists else 'w', newline='') as visit_f:
            gap_writer = csv.DictWriter(gap_f, fieldnames=GAP_FIELDS)
            if not gap_exists:
                gap_writer.writeheader()
            visit_writer = csv.DictWriter(visit_f, fieldnames=VISIT_FIELDS)
            if not visit_exists:
                visit_writer.writeheader()

            for gap in gaps:
                gap_writer.writerow(gap_to_row(gap))
            visit_writer.writerow(visit_row)

    print(f'\nTotal insane gaps across all processed admissions: {total_insane}')
    if toc_order_mismatch_patients:
        print(
            f'TOC label order disagreed with toc_id order for {len(toc_order_mismatch_patients)} '
            f'patient(s) this run: {sorted(toc_order_mismatch_patients)} - see warnings above.'
        )


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description=f'Measure NS5/NSP_ID={NSP_ID} recording coverage and gaps for one or all patients.',
        add_help=False,
    )
    arg_parser.add_argument('--patient', type=str, help='Restrict to the patient with this EMU identifier')
    arg_parser.add_argument(
        '--exclude', type=str, action='append', default=[], metavar='EMU-ID',
        help='Exclude the patient with this EMU identifier from the analysis (repeatable)'
    )
    arg_parser.add_argument('--gap-out', type=str, default='gap_summary.csv')
    arg_parser.add_argument('--visit-out', type=str, default='visit_summary.csv')
    args = arg_parser.parse_args()

    connect(args)

    main(args.patient, args.gap_out, args.visit_out, args.exclude)
