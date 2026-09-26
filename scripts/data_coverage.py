"""
Standalone analysis script: measure recording coverage/discontinuities for patients in the emu24
schema.

For each admission, every NS5 data packet (across all TOCInstances/chunks) on NSP_ID is listed
out and sorted in time; a point where one packet ends before the next one starts is a
discontinuity. "Discontinuity" rather than "gap" is deliberate: not every discontinuity found here
represents real missing data (some are clock/tick artifacts) - "gap" is reserved for the eventual,
confirmed list of real missing data once these have been resolved one way or the other.

UTC anchoring: BRK ticks are only comparable within one TOCInstance. Each TOC is anchored ONCE,
from its own chronologically-first chunk file's TimeOrigin - every other file in that TOC converts
ticks using that single shared origin (via pyNsXStitch.helpers.brk_toc_ticks_to_utc), never its own
TimeOrigin, since a file's own TimeOrigin can carry its own small clock latency/inaccuracy.

Insane discontinuities: one that's negative (beyond float rounding) or bigger than
MAX_SANE_OFFSET_SECONDS means the underlying tick/TimeOrigin was corrupted, not real. These are
flagged `insane=True` in discontinuity_summary.csv with the raw (nonsensical) value left in
`duration_hours` - correcting them is a deferred second pass, not done here. A discontinuity of 0-1
ticks at a file cut is a normal artifact, not reported at all.

Only NS5 on a single NSP is used. Assumes all NS5 files are FileSpec 3.0 (PTP, 64-bit timestamps).

Resumable: if visit_out_path already exists, any patient with a row in it is skipped, and new rows
are appended. If a previous run died partway through a patient, that patient's partial
discontinuity_summary/packet_summary rows must be removed by hand before resuming
(visit_summary.csv is the completion marker).

Also writes a packet-level CSV (packet_out_path) - one row per raw data packet (patient_id/emu_id,
admission_id, toc_id, chunk_id, packet_number within its chunk, brk_start, n_points) - as a minimal
resource for investigating discontinuities directly against the raw packet stream, without needing
to re-read the source files each time.

Usage:
    python scripts/data_coverage.py [--patient EMU-ID] [--exclude EMU-ID]
        [--discontinuity-out discontinuity_summary.csv] [--visit-out visit_summary.csv]
        [--packet-out packet_summary.csv]
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
    n_points: int
    brk_end: int
    ts_resolution: int
    utc_start: datetime
    utc_end: datetime


# A raw BRK tick this far from a TOC's own origin can't be real - the packet header was misread
# (e.g. byte-misaligned after an earlier corrupted packet). Also the insane-discontinuity threshold
# below.
MAX_SANE_OFFSET_SECONDS = 100 * 365 * 24 * 3600  # 100 years
ROUNDING_TOLERANCE_SECONDS = 1e-6  # sub-microsecond float noise, not a real negative discontinuity
FILE_EDGE_TICK_TOLERANCE = 1  # a 0-1 tick discontinuity at a file cut is a normal artifact


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
                n_points=n_points,
                brk_end=brk_end,
                ts_resolution=ts_resolution,
                utc_start=_safe_utc(brk_start, origin_utc, first_tick, ts_resolution),
                utc_end=_safe_utc(brk_end, origin_utc, first_tick, ts_resolution),
            )
    finally:
        nsx_file.datafile.close()


def toc_packets(chunks_for_toc):
    """Every packet in one TOC, in true chronological order, all anchored to that TOC's own
    shared origin (see toc_origin).

    Chunk files are ordered by chunk_id (assumed chronological, same convention used elsewhere).
    Within one file, packets are kept in iter_file_packets' own order - iter_nsx_timestamps walks
    the file by byte offset, so that order is already the true physical/chronological read order,
    regardless of what a packet's own (possibly corrupted) brk_start says. Do NOT re-sort by
    brk_start: when a corrupted tick drops to a small value, sorting ascending by brk_start yanks
    that packet - and only that packet - earlier in the sequence than packets that were actually
    read before it, fabricating the appearance of a tick "resetting then recovering" when the true
    story (from physical read order) is that the stream just kept glitching with no recovery.
    """
    origin_utc, ts_resolution, first_tick = toc_origin(chunks_for_toc)
    packets = []
    for chunk in sorted(chunks_for_toc, key=lambda c: c.chunk_id):
        packets.extend(iter_file_packets(chunk, origin_utc, ts_resolution, first_tick))
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


def find_discontinuity(before: Packet, after: Packet, cause: str):
    """
    Determine whether there's a discontinuity between two time-adjacent packets: BRK ticks (exact)
    within one TOC, UTC otherwise (ticks aren't comparable across a TOC boundary). A negative-
    beyond-rounding or implausibly large duration is flagged `insane` and reported as-is,
    uncorrected - see module docstring.

    :return: None if no discontinuity, else a dict of the fields to attach to a Discontinuity
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
class Discontinuity:
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
    Build the packet timeline for one admission, TOC by TOC (each on its own shared UTC origin -
    see module docstring), find discontinuities in it, and compute summary stats.

    :param chunks: every NS5 ChunkFile belonging to this admission (all toc_id, NSP_ID only)
    :return: (discontinuities, packet_rows, visit_row), or (None, None, None) if there were no
        packets at all
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
        return None, None, None

    discontinuities = []
    for before, after in zip(all_packets, all_packets[1:]):
        cause = classify_cause(before, after)
        disc_info = find_discontinuity(before, after, cause)
        if disc_info is not None:
            discontinuities.append(
                Discontinuity(patient_id, emu_id, admission_id, before, after, cause, **disc_info)
            )

    # packet_number resets to 0 at the start of each chunk file, so it's a stable, minimal
    # per-chunk index for cross-referencing a row here against the same packet in the raw file.
    packet_rows = []
    packet_number = 0
    current_chunk_key = None
    for p in all_packets:
        chunk_key = (p.chunk.toc_id, p.chunk.chunk_id)
        if chunk_key != current_chunk_key:
            current_chunk_key = chunk_key
            packet_number = 0
        packet_rows.append({
            'patient_id': patient_id,
            'emu_id': emu_id,
            'admission_id': admission_id,
            'toc_id': p.chunk.toc_id,
            'chunk_id': p.chunk.chunk_id,
            'packet_number': packet_number,
            'brk_start': p.brk_start,
            'n_points': p.n_points,
        })
        packet_number += 1

    span_start = all_packets[0].utc_start
    span_end = all_packets[-1].utc_end
    total_span_hours = (span_end - span_start).total_seconds() / 3600
    total_discontinuity_hours = sum(d.duration_hours for d in discontinuities)
    visit_row = {
        'patient_id': patient_id,
        'emu_id': emu_id,
        'admission_id': admission_id,
        'nsp_id': NSP_ID,
        'recording_start_utc': span_start.isoformat(),
        'recording_end_utc': span_end.isoformat(),
        'total_span_hours': total_span_hours,
        'n_discontinuities': len(discontinuities),
        'total_discontinuity_hours': total_discontinuity_hours,
        'total_continuous_hours': total_span_hours - total_discontinuity_hours,
        'pct_coverage': 100 * (total_span_hours - total_discontinuity_hours) / total_span_hours if total_span_hours else None,
        'n_insane_discontinuities': sum(1 for d in discontinuities if d.insane),
        'toc_order_mismatch': toc_order_mismatch,
    }

    return discontinuities, packet_rows, visit_row


DISCONTINUITY_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'discontinuity_start_utc', 'discontinuity_end_utc', 'duration_hours', 'detection_method', 'insane',
    'toc_id_before', 'toc_base_file_before', 'chunk_id_before', 'file_before',
    'brk_before_ts', 'brk_before_resolution_hz',
    'toc_id_after', 'toc_base_file_after', 'chunk_id_after', 'file_after',
    'brk_after_ts', 'brk_after_resolution_hz',
    'cause',
]

VISIT_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'recording_start_utc', 'recording_end_utc', 'total_span_hours',
    'n_discontinuities', 'total_discontinuity_hours', 'total_continuous_hours', 'pct_coverage',
    'n_insane_discontinuities',
    'toc_order_mismatch',
]

PACKET_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'toc_id', 'chunk_id', 'packet_number',
    'brk_start', 'n_points',
]


def discontinuity_to_row(discontinuity: Discontinuity) -> dict:
    return {
        'patient_id': discontinuity.patient_id,
        'emu_id': discontinuity.emu_id,
        'admission_id': discontinuity.admission_id,
        'nsp_id': NSP_ID,
        'discontinuity_start_utc': discontinuity.before.utc_end.isoformat(),
        'discontinuity_end_utc': discontinuity.after.utc_start.isoformat(),
        'duration_hours': discontinuity.duration_hours,
        'detection_method': discontinuity.detection_method,
        'insane': discontinuity.insane,
        'toc_id_before': discontinuity.before.chunk.toc_id,
        'toc_base_file_before': discontinuity.before.chunk.toc_base_file,
        'chunk_id_before': discontinuity.before.chunk.chunk_id,
        'file_before': discontinuity.before.chunk.filepath,
        'brk_before_ts': discontinuity.before.brk_end,
        'brk_before_resolution_hz': discontinuity.before.ts_resolution,
        'toc_id_after': discontinuity.after.chunk.toc_id,
        'toc_base_file_after': discontinuity.after.chunk.toc_base_file,
        'chunk_id_after': discontinuity.after.chunk.chunk_id,
        'file_after': discontinuity.after.chunk.filepath,
        'brk_after_ts': discontinuity.after.brk_start,
        'brk_after_resolution_hz': discontinuity.after.ts_resolution,
        'cause': discontinuity.cause,
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


def main(patient_emu_id, discontinuity_out_path, visit_out_path, packet_out_path, exclude_emu_ids=None):
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

        discontinuities, packet_rows, visit_row = process_admission(
            admission['patient_id'], admission['emu_id'], admission['admission_id'], chunks
        )
        if visit_row is None:
            print('  No data packets found in any NS5 chunk, skipping')
            continue
        print(f"  {visit_row['n_discontinuities']} discontinuity(-ies) found ({visit_row['n_insane_discontinuities']} insane)")
        total_insane += visit_row['n_insane_discontinuities']
        if visit_row['toc_order_mismatch']:
            toc_order_mismatch_patients.add(admission['emu_id'])

        # Reopened per admission (rather than held open for the whole run) so every output file is
        # flushed and readable on disk after every admission, not just at exit.
        discontinuity_exists = os.path.exists(discontinuity_out_path)
        visit_exists = os.path.exists(visit_out_path)
        packet_exists = os.path.exists(packet_out_path)
        with open(discontinuity_out_path, 'a' if discontinuity_exists else 'w', newline='') as disc_f, \
                open(visit_out_path, 'a' if visit_exists else 'w', newline='') as visit_f, \
                open(packet_out_path, 'a' if packet_exists else 'w', newline='') as packet_f:
            disc_writer = csv.DictWriter(disc_f, fieldnames=DISCONTINUITY_FIELDS)
            if not discontinuity_exists:
                disc_writer.writeheader()
            visit_writer = csv.DictWriter(visit_f, fieldnames=VISIT_FIELDS)
            if not visit_exists:
                visit_writer.writeheader()
            packet_writer = csv.DictWriter(packet_f, fieldnames=PACKET_FIELDS)
            if not packet_exists:
                packet_writer.writeheader()

            for discontinuity in discontinuities:
                disc_writer.writerow(discontinuity_to_row(discontinuity))
            packet_writer.writerows(packet_rows)
            visit_writer.writerow(visit_row)

    print(f'\nTotal insane discontinuities across all processed admissions: {total_insane}')
    if toc_order_mismatch_patients:
        print(
            f'TOC label order disagreed with toc_id order for {len(toc_order_mismatch_patients)} '
            f'patient(s) this run: {sorted(toc_order_mismatch_patients)} - see warnings above.'
        )


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description=f'Measure NS5/NSP_ID={NSP_ID} recording coverage and discontinuities for one or all patients.',
        add_help=False,
    )
    arg_parser.add_argument('--patient', type=str, help='Restrict to the patient with this EMU identifier')
    arg_parser.add_argument(
        '--exclude', type=str, action='append', default=[], metavar='EMU-ID',
        help='Exclude the patient with this EMU identifier from the analysis (repeatable)'
    )
    arg_parser.add_argument('--discontinuity-out', type=str, default='discontinuity_summary.csv')
    arg_parser.add_argument('--visit-out', type=str, default='visit_summary.csv')
    arg_parser.add_argument('--packet-out', type=str, default='packet_summary.csv')
    args = arg_parser.parse_args()

    connect(args)

    main(args.patient, args.discontinuity_out, args.visit_out, args.packet_out, args.exclude)
