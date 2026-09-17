"""
Standalone analysis script: measure recording coverage/gaps for patients in the emu24 schema.

For each admission, every NS5 data packet (across all TOCInstances/chunks) on NSP_ID is listed
out, converted to UTC, and sorted in time. Walking that sorted list, any point where one packet
ends before the next one starts is a gap.

Only NS5 on a single NSP is used: in practice this is sufficient to characterize coverage for
the large majority of admissions. NS3 and other NSPs are not considered here.

Assumes all NS5 files are FileSpec 3.0 (PTP, 64-bit per-packet timestamps).

Resumable: if the output files already exist, any patient with a row already in visit_out_path is
treated as fully processed and skipped, and new rows are appended rather than overwriting. If a
previous run died partway through a patient, that patient's partial rows across all three output
files must be removed by hand before resuming, or they'll be left in place alongside whatever a
fresh reprocessing (if any) adds.

Usage:
    python scripts/data_coverage.py [--patient EMU-ID] [--gap-out gap_summary.csv]
        [--visit-out visit_summary.csv] [--imputed-out imputed_gaps.csv]
"""

import argparse
import csv
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import datajoint as dj
from brpylib import NsxFile
from pyNsXStitch.streamers import iter_nsx_timestamps
from tqdm import tqdm

NSP_ID = 2

# The datalake is read-only and files aren't expected to change, so skip DataJoint's per-fetch
# content checksum on filepath attributes (ns5_file) - without this, fetching a chunk's filepath
# reads and hashes the entire file, which is very slow for large NS5 recordings. This is
# intentional here, so silence the "Skipped checksum" warning it logs for every file.
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
    Determine the chronological order of the TOCs in one admission.

    Each TOC's own base_file label (assigned by the recording software) is used as the
    authoritative order, not toc_id - earlier admissions (from when this ingestion pipeline was
    still maturing) have shown toc_id doesn't always track real chronology. A TOC whose base_file
    is missing/unparseable sorts as if it were earliest, then falls back to toc_id as a tiebreak;
    this is rare and only a deterministic fallback, not a claim that it's chronologically correct.

    :return: (toc_order: list of toc_id in chronological order, mismatch: bool - True if this
        differs from plain toc_id numeric order, i.e. toc_id could not be trusted here,
        base_file_by_toc: dict of toc_id -> its base_file label, for reporting the mismatch)
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


# BRK ticks are exact but only comparable within one TOCInstance (each TOC has its own clock
# origin). Above this size, a BRK-detected gap is cross-checked against UTC in case the BRK
# clock itself reset/glitched, which would make the BRK-derived duration meaningless.
LARGE_GAP_SECONDS = 5
CLOCK_MISMATCH_TOLERANCE_SECONDS = 0.1


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
    utc_cross_check_hours: float = None  # only set when detection_method == 'brk' and gap is large
    clock_discrepancy: bool = None  # True if the UTC cross-check disagreed with the BRK duration


@dataclass
class ImputedGap:
    """
    A best-estimate substitute for one or more corrupted (clock-glitch) packets.

    Since a corrupted BRK tick/TimeOrigin makes the real gap boundaries unknowable, this
    conservatively bridges from the last known-good packet before the corrupted run to the next
    known-good packet after it, rather than trusting anything computed from the bad packet(s).
    """
    patient_id: int
    emu_id: str
    admission_id: int
    from_packet: Packet  # last known-good packet before the corrupted run
    to_packet: Packet  # first known-good packet after the corrupted run
    corrupted_packets: list  # the packet(s) skipped over between from_packet and to_packet

    @property
    def duration_hours(self):
        return (self.to_packet.utc_start - self.from_packet.utc_end).total_seconds() / 3600


# A raw BRK tick this far from a file's own origin can't be real (no admission runs anywhere
# close to 100 years) - it means the packet header was misread (e.g. byte-misaligned after an
# earlier corrupted packet), producing a near-arbitrary 64-bit value. datetime/timedelta can't
# represent an offset that large without raising OverflowError, so it's clamped to datetime.min/
# max instead: still a real Python int (no precision is lost - struct.unpack('<Q', ...) already
# returns a full-width, arbitrary-precision int, so this isn't a bigint/dtype issue), just too
# large to express as a calendar date. Clamping guarantees the packet fails the per-TOC validity
# window check in process_admission, same as any other corrupted packet.
MAX_SANE_OFFSET_SECONDS = 100 * 365 * 24 * 3600


def _safe_utc(origin_utc: datetime, tick_diff: int, ts_resolution: int) -> datetime:
    offset_seconds = tick_diff / ts_resolution
    if offset_seconds > MAX_SANE_OFFSET_SECONDS:
        return datetime.max
    if offset_seconds < -MAX_SANE_OFFSET_SECONDS:
        return datetime.min
    return origin_utc + timedelta(seconds=offset_seconds)


def iter_file_packets(chunk: ChunkFile):
    """
    Yield one Packet per data packet found in this NS5 file.

    PTP timestamps (FileSpec >=3) are an absolute, continuously-running tick counter - they do
    NOT reset to ~0 at the start of each chunk file. brpylib's own getdata() treats the first
    timestamp found in a file as that file's zero-reference (see ts_0 in brpylib.NsxFile.getdata),
    so UTC is anchored the same way here: TimeOrigin corresponds to this file's first packet, not
    to tick 0. brk_start/brk_end are kept as the raw absolute ticks (unadjusted), since those stay
    valid for direct comparison across chunk files within one TOCInstance.
    """
    nsx_file = NsxFile(chunk.filepath, verbose=False, interactive=False)
    try:
        ts_resolution = nsx_file.basic_header['TimeStampResolution']
        sample_freq = nsx_file.basic_header['SampleResolution'] / nsx_file.basic_header['Period']
        ticks_per_sample = ts_resolution / sample_freq
        origin_utc = nsx_file.basic_header['TimeOrigin']  # UTC, per brpylib format_timeorigin

        file_origin_ticks = None
        for brk_start, n_points in iter_nsx_timestamps(nsx_file):
            if file_origin_ticks is None:
                file_origin_ticks = brk_start
            brk_end = brk_start + int(round(n_points * ticks_per_sample))
            yield Packet(
                chunk=chunk,
                brk_start=brk_start,
                brk_end=brk_end,
                ts_resolution=ts_resolution,
                utc_start=_safe_utc(origin_utc, brk_start - file_origin_ticks, ts_resolution),
                utc_end=_safe_utc(origin_utc, brk_end - file_origin_ticks, ts_resolution),
            )
    finally:
        nsx_file.datafile.close()


def classify_cause(before: Packet, after: Packet) -> str:
    if before.chunk.toc_id != after.chunk.toc_id:
        return 'TOC reset'
    elif before.chunk.chunk_id != after.chunk.chunk_id:
        return 'file edge'
    else:
        return 'other'


def _utc_gap_seconds(before: Packet, after: Packet) -> float:
    return (after.utc_start - before.utc_end).total_seconds()


def find_gap(before: Packet, after: Packet):
    """
    Determine whether there's a gap between two time-adjacent packets, preferring BRK ticks
    (the NSP's own clock, exact) over UTC (derived from TimeOrigin, ~1ms precision) whenever
    possible. BRK ticks only mean the same thing within one TOCInstance, so a TOC boundary must
    fall back to UTC instead.

    Note: the utc_cross_check here can't catch a corrupted BRK tick within one file, since our
    UTC is itself derived from that same raw tick (see iter_file_packets) - it isn't independent.
    Packets with a corrupted BRK tick or TimeOrigin are instead filtered out entirely in
    process_admission (see is_valid there) before this function ever sees them, based on each
    packet's own TOC's independently-measured start/end window.

    :return: None if no gap, else a dict of the fields to attach to a Gap
    """
    same_toc = before.chunk.toc_id == after.chunk.toc_id

    if same_toc:
        brk_gap_ticks = after.brk_start - before.brk_end
        gap_seconds = brk_gap_ticks / before.ts_resolution
        method = 'brk'
    else:
        gap_seconds = _utc_gap_seconds(before, after)
        method = 'utc'

    if gap_seconds <= 0:
        return None

    result = {'duration_hours': gap_seconds / 3600, 'detection_method': method}

    if method == 'brk' and gap_seconds > LARGE_GAP_SECONDS:
        utc_gap_seconds = _utc_gap_seconds(before, after)
        result['utc_cross_check_hours'] = utc_gap_seconds / 3600
        result['clock_discrepancy'] = abs(utc_gap_seconds - gap_seconds) > CLOCK_MISMATCH_TOLERANCE_SECONDS

    return result


def process_admission(patient_id, emu_id, admission_id, chunks):
    """
    Build the sorted NS5/NSP_ID packet timeline for one admission, find gaps in it, and compute
    summary stats.

    :param chunks: every NS5 ChunkFile belonging to this admission (all toc_id, NSP_ID only)
    :return: (gaps, imputed_gaps, visit_row) or (None, None, None) if there were no packets at all
    """
    # TOC label (base_file) order is authoritative, not toc_id numeric order - see
    # determine_toc_order. When they disagree, that's logged here (to both console and disk, via
    # the root logger emu24.schema configures) so it's visible how often this happens and whether
    # it's really confined to earlier admissions, as suspected.
    toc_order, toc_order_mismatch, base_file_by_toc = determine_toc_order(chunks)
    if toc_order_mismatch:
        ordered_pairs = list(zip(toc_order, (base_file_by_toc[t] for t in toc_order)))
        logging.warning(
            f'Patient {emu_id} admission {admission_id}: TOC label (base_file) order disagrees '
            f'with toc_id numeric order. Using label order (toc_id, base_file) = {ordered_pairs} '
            f'instead of toc_id order - treating toc_id as unreliable for this admission.'
        )
    toc_rank = {toc_id: rank for rank, toc_id in enumerate(toc_order)}

    packets = []
    for chunk in tqdm(chunks, desc='Scanning NS5 chunks'):
        packets.extend(iter_file_packets(chunk))

    # Sort by each TOC's chronological rank (from its label, above), then by the DB's own
    # chunk_id ordering within a TOC, then brk_start as a tiebreak for multiple segments within
    # the same chunk file. Recordings are strictly sequential (one NSP can't record two TOCs at
    # once), so trusting our own derived utc_start for *ordering* is what let unrelated TOCs sort
    # in between two segments of the same chunk file when one file's TimeOrigin was slightly off.
    packets.sort(key=lambda p: (toc_rank[p.chunk.toc_id], p.chunk.chunk_id, p.brk_start))

    if not packets:
        return None, None, None

    # First-seen packet per toc_id, in the chronological order established above - an independent
    # measurement (from a different file's TimeOrigin) of when each TOC actually began. Together
    # with the next TOC's own start, this defines a valid time window per TOC: a packet computed
    # to fall outside its own TOC's window (in EITHER direction) has a corrupted BRK tick or
    # TimeOrigin, not a real recording gap. Checking only the "after" side of a same-TOC pair
    # (whether it overran the next TOC) missed cases where it was actually the "before" side that
    # was corrupted - its bogus early timestamp can still coincidentally land just under the next
    # TOC's start, e.g. if it happens to be off by only a few hours.
    toc_first_packet = {}
    for p in packets:
        toc_first_packet.setdefault(p.chunk.toc_id, p)
    # A TOC with chunks but no successfully-decoded packets would still be in toc_order (which
    # comes from chunks, not packets) - drop it here rather than KeyError below.
    toc_order = [t for t in toc_order if t in toc_first_packet]

    toc_window = {}
    for i, toc_id in enumerate(toc_order):
        window_start = toc_first_packet[toc_id].utc_start
        window_end = toc_first_packet[toc_order[i + 1]].utc_start if i + 1 < len(toc_order) else None
        toc_window[toc_id] = (window_start, window_end)

    def is_valid(p):
        window_start, window_end = toc_window[p.chunk.toc_id]
        if p.utc_start < window_start:
            return False
        if window_end is not None and p.utc_end > window_end:
            return False
        return True

    # Walk the sorted packets, only forming a Gap between two adjacent VALID packets (so
    # find_gap/classify_cause never sees corrupted data). A run of one or more invalid packets
    # in between gets bridged by a single ImputedGap instead of trusting anything computed from
    # them. NOTE: this assumes each TOC's own first packet (which defines its window) is itself
    # valid - true for every corrupted file seen so far (corruption has hit later chunks, not the
    # TOC-opening one), but not something this check can independently verify.
    gaps = []
    imputed_gaps = []
    last_good_idx = None
    corrupted_run = []
    leading_untethered = []
    for idx, p in enumerate(packets):
        if not is_valid(p):
            corrupted_run.append(p)
            continue

        if last_good_idx is None:
            # No known-good packet exists yet to bridge from - these leading corrupted packets
            # can't be imputed either, same as a trailing run at the end of the admission.
            leading_untethered = corrupted_run
        elif not corrupted_run:
            prev = packets[last_good_idx]
            gap_info = find_gap(prev, p)
            if gap_info is not None:
                cause = classify_cause(prev, p)
                gaps.append(Gap(patient_id, emu_id, admission_id, prev, p, cause, **gap_info))
        else:
            prev = packets[last_good_idx]
            imputed_gaps.append(ImputedGap(patient_id, emu_id, admission_id, prev, p, corrupted_run))

        corrupted_run = []
        last_good_idx = idx

    # Corrupted packets at the very start (before any known-good packet) or end (after the last
    # one) of the admission have no known-good packet on one side to bridge to/from, so they
    # can't be imputed - still count them rather than silently dropping them.
    n_untethered_corrupted_packets = len(leading_untethered) + len(corrupted_run)

    good_packets = [p for p in packets if is_valid(p)]
    span_start = good_packets[0].utc_start
    span_end = good_packets[-1].utc_end
    total_span_hours = (span_end - span_start).total_seconds() / 3600
    total_gap_hours = sum(g.duration_hours for g in gaps) + sum(g.duration_hours for g in imputed_gaps)
    visit_row = {
        'patient_id': patient_id,
        'emu_id': emu_id,
        'admission_id': admission_id,
        'nsp_id': NSP_ID,
        'recording_start_utc': span_start.isoformat(),
        'recording_end_utc': span_end.isoformat(),
        'total_span_hours': total_span_hours,
        'n_gaps': len(gaps) + len(imputed_gaps),
        'total_gap_hours': total_gap_hours,
        'total_continuous_hours': total_span_hours - total_gap_hours,
        'pct_coverage': 100 * (total_span_hours - total_gap_hours) / total_span_hours if total_span_hours else None,
        'n_imputed_gaps': len(imputed_gaps),
        'imputed_gap_hours': sum(g.duration_hours for g in imputed_gaps),
        'n_corrupted_packets': sum(len(g.corrupted_packets) for g in imputed_gaps) + n_untethered_corrupted_packets,
        'n_untethered_corrupted_packets': n_untethered_corrupted_packets,
        'toc_order_mismatch': toc_order_mismatch,
    }

    return gaps, imputed_gaps, visit_row


GAP_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'gap_start_utc', 'gap_end_utc', 'duration_hours', 'detection_method',
    'utc_cross_check_hours', 'clock_discrepancy',
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
    'n_imputed_gaps', 'imputed_gap_hours',
    'n_corrupted_packets', 'n_untethered_corrupted_packets',
    'toc_order_mismatch',
]

IMPUTED_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'nsp_id',
    'gap_start_utc', 'gap_end_utc', 'duration_hours',
    'toc_id_before', 'toc_base_file_before', 'chunk_id_before', 'file_before',
    'brk_before_ts', 'brk_before_resolution_hz',
    'toc_id_after', 'toc_base_file_after', 'chunk_id_after', 'file_after',
    'brk_after_ts', 'brk_after_resolution_hz',
    'n_corrupted_packets', 'source_brk_corrupted_ts',
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
        'utc_cross_check_hours': gap.utc_cross_check_hours,
        'clock_discrepancy': gap.clock_discrepancy,
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


def imputed_gap_to_row(imputed: ImputedGap) -> dict:
    return {
        'patient_id': imputed.patient_id,
        'emu_id': imputed.emu_id,
        'admission_id': imputed.admission_id,
        'nsp_id': NSP_ID,
        'gap_start_utc': imputed.from_packet.utc_end.isoformat(),
        'gap_end_utc': imputed.to_packet.utc_start.isoformat(),
        'duration_hours': imputed.duration_hours,
        'toc_id_before': imputed.from_packet.chunk.toc_id,
        'toc_base_file_before': imputed.from_packet.chunk.toc_base_file,
        'chunk_id_before': imputed.from_packet.chunk.chunk_id,
        'file_before': imputed.from_packet.chunk.filepath,
        'brk_before_ts': imputed.from_packet.brk_end,
        'brk_before_resolution_hz': imputed.from_packet.ts_resolution,
        'toc_id_after': imputed.to_packet.chunk.toc_id,
        'toc_base_file_after': imputed.to_packet.chunk.toc_base_file,
        'chunk_id_after': imputed.to_packet.chunk.chunk_id,
        'file_after': imputed.to_packet.chunk.filepath,
        'brk_after_ts': imputed.to_packet.brk_start,
        'brk_after_resolution_hz': imputed.to_packet.ts_resolution,
        'n_corrupted_packets': len(imputed.corrupted_packets),
        'source_brk_corrupted_ts': imputed.corrupted_packets[0].brk_start,
    }


def fetch_admissions(patient_emu_id=None):
    from emu24.schema import Patient, Admission
    query = Patient * Admission
    if patient_emu_id:
        query = query & f'emu_id="{patient_emu_id}"'
    rows = query.fetch('patient_id', 'emu_id', 'admission_id', as_dict=True)
    # DataJoint returns numpy scalar types (e.g. numpy.int64) for int attributes, not plain
    # Python ints - harmless individually (str()s the same), but repr()s ugly (np.int64(43)) the
    # moment one ends up inside a list/tuple that gets logged, so normalize at the fetch boundary.
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
    """
    Patients that already have a row in visit_summary.csv are treated as fully done.

    A visit_summary row is only ever written once, after an admission finishes processing
    without error, so it's a cleaner completion marker than gap_summary.csv (which could in
    principle end up with a partial admission's rows if a crash landed mid-write).
    """
    if not os.path.exists(visit_out_path):
        return set()
    with open(visit_out_path, newline='') as f:
        return {int(row['patient_id']) for row in csv.DictReader(f)}


def main(patient_emu_id, gap_out_path, visit_out_path, imputed_out_path):
    admissions = fetch_admissions(patient_emu_id)
    total_clock_corruptions = 0
    toc_order_mismatch_patients = set()

    completed_patients = load_completed_patients(visit_out_path)
    if completed_patients:
        print(f'Resuming: {len(completed_patients)} patient(s) already have rows in {visit_out_path}, skipping them')

    gap_exists = os.path.exists(gap_out_path)
    visit_exists = os.path.exists(visit_out_path)
    imputed_exists = os.path.exists(imputed_out_path)

    with open(gap_out_path, 'a' if gap_exists else 'w', newline='') as gap_f, \
            open(visit_out_path, 'a' if visit_exists else 'w', newline='') as visit_f, \
            open(imputed_out_path, 'a' if imputed_exists else 'w', newline='') as imputed_f:
        gap_writer = csv.DictWriter(gap_f, fieldnames=GAP_FIELDS)
        if not gap_exists:
            gap_writer.writeheader()
        visit_writer = csv.DictWriter(visit_f, fieldnames=VISIT_FIELDS)
        if not visit_exists:
            visit_writer.writeheader()
        imputed_writer = csv.DictWriter(imputed_f, fieldnames=IMPUTED_FIELDS)
        if not imputed_exists:
            imputed_writer.writeheader()

        for admission in admissions:
            if admission['patient_id'] in completed_patients:
                print(f"Skipping patient {admission['emu_id']} admission {admission['admission_id']} (already complete)")
                continue

            print(f"Processing patient {admission['emu_id']} admission {admission['admission_id']}...")
            chunks = fetch_chunks_for_admission(admission['patient_id'], admission['admission_id'])
            if not chunks:
                print(f'  No NS5 chunks found for NSP_ID={NSP_ID}, skipping')
                continue

            gaps, imputed_gaps, visit_row = process_admission(
                admission['patient_id'], admission['emu_id'], admission['admission_id'], chunks
            )
            if visit_row is None:
                print('  No data packets found in any NS5 chunk, skipping')
                continue
            print(f"  {visit_row['n_gaps']} gaps found ({visit_row['n_corrupted_packets']} corrupted packets "
                  f"across {visit_row['n_imputed_gaps']} imputed gap(s)"
                  + (f", {visit_row['n_untethered_corrupted_packets']} untethered" if visit_row['n_untethered_corrupted_packets'] else '')
                  + ')')
            total_clock_corruptions += visit_row['n_corrupted_packets']
            if visit_row['toc_order_mismatch']:
                toc_order_mismatch_patients.add(admission['emu_id'])

            for gap in gaps:
                gap_writer.writerow(gap_to_row(gap))
            for imputed in imputed_gaps:
                imputed_writer.writerow(imputed_gap_to_row(imputed))
            visit_writer.writerow(visit_row)

    print(f'\nTotal suspected clock corruptions across all processed admissions: {total_clock_corruptions}')
    if toc_order_mismatch_patients:
        print(
            f'TOC label order disagreed with toc_id order for {len(toc_order_mismatch_patients)} '
            f'patient(s) this run: {sorted(toc_order_mismatch_patients)} - see warnings above '
            f'(and the log file) for which admissions/TOCs specifically.'
        )


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description=f'Measure NS5/NSP_ID={NSP_ID} recording coverage and gaps for one or all patients.',
        add_help=False,
    )
    arg_parser.add_argument('--patient', type=str, help='Restrict to the patient with this EMU identifier')
    arg_parser.add_argument('--gap-out', type=str, default='gap_summary.csv')
    arg_parser.add_argument('--visit-out', type=str, default='visit_summary.csv')
    arg_parser.add_argument('--imputed-out', type=str, default='imputed_gaps.csv')
    args = arg_parser.parse_args()

    connect(args)

    main(args.patient, args.gap_out, args.visit_out, args.imputed_out)