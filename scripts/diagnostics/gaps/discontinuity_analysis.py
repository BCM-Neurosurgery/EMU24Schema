"""
Library of functions for resolving `insane=True` discontinuities from data_coverage.py's
discontinuity_summary.csv into `gap`s (real missing-data intervals) and untrustworthy-clock time
ranges, or into a plan for fixing time alignment later.

Convention: each discontinuity category gets up to three functions:
    find_<category>_events(disc, pkt) -> which discontinuity_summary.csv rows belong to that
        category, grouped into events (see identify_all). Every row between the first sign of the
        problem and wherever the event ends is included, even if some of those rows would look
        like a different/no category in isolation - they all get handled together as one event.
    calculate_<category>_gap(disc_indices, disc, pkt) -> the real missing-data interval for one
        such event (a `gap`), or None if the category is known not to represent any actual missing
        data; later, a time-alignment fix could be added alongside it.
    calculate_<category>_untrustworthy_range(disc_indices, disc, pkt) -> the time range within
        which the BRK clock can't be trusted for one such event - only for categories that actually
        involve a corrupted/unreliable clock (not every category has one - simple_gap doesn't; see
        UNTRUSTWORTHY_RANGE_CALCULATORS). NOT the same interval as
        calculate_<category>_gap in general: for clock_reset in particular, the untrustworthy range
        is the WHOLE broken span, while the gap is only the shortfall within it that datapoint
        counting can't account for (see calculate_clock_reset_gap/_untrustworthy_range). Reuses the
        gap calculator's own internal computation via a shared _<category>_span helper wherever the
        two overlap, noted in each function's docstring.

`insane` is read directly from discontinuity_summary.csv's own column wherever a *logged
discontinuity* is being classified - never recomputed. The one exception is the packet-to-packet
walk in _walk_back_to_anchor: packet_summary.csv logs every raw packet regardless of whether a
discontinuity was flagged for it, so there's no `insane` column to read for an arbitrary
consecutive pair - that's the one place this module reuses data_coverage.py's own is_insane
predicate directly, on the same tick-difference calculation data_coverage.py itself does.

Currently implemented:
    - simple_gap (find + calculate_gap): a non-insane discontinuity - data_coverage.py's first pass
      already got these right, so they're passed forward unchanged, not re-derived. "Simple" because
      it needs no further interpretation, not because the underlying gap is small or unimportant.
      Not an untrustworthy-clock range - the clock is fine on both sides of it.
    - packet_overlap (find + calculate_gap + calculate_untrustworthy_range): the tolerated-band-
      adjacent -1 tick at a file boundary - not a gap (calculate_gap returns None), but IS an
      untrustworthy instant: the overlap means a stitch across that boundary can't be sure which
      packet's copy of the shared sample is authoritative.
    - packet_corruption (find + calculate_gap + calculate_untrustworthy_range): a corrupted terminal
      tick propagating across a TOC boundary. Its gap and untrustworthy_range intervals coincide.
    - clock_reset (find + calculate_gap + calculate_untrustworthy_range): the tick resetting mid-TOC
      without a corrupted handoff into the next TOC, and never recovering before it ends. Its
      untrustworthy_range is the whole broken span; calculate_gap is only the datapoint-implied
      shortfall within it (see above). One patient (YFE) shows a periodic, repeating flavor of this
      (a fixed ~-3.985s tick offset recurring continuously within one file) - not a separate
      category, just excluded from seeding a fresh event of its own (see CATEGORY_1_TICK_OFFSETS).
See scripts/discontinuity_types.md for the full category reference.
"""

import pandas as pd

from data_coverage import is_insane, BRK_RESOLUTION_HZ

SIMPLE_GAP = 'simple_gap'
PACKET_OVERLAP = 'packet_overlap'
PACKET_CORRUPTION = 'packet_corruption'
CLOCK_RESET = 'clock_reset'

# Columns every gap has (or should, once its category's calculate_gap fills them in), in the order
# they should appear - a category's own extra fields (e.g. packet_corruption's anchor_* columns)
# are often empty for other categories, so they're sorted to the end instead of interleaved here.
COMMON_GAP_FIELDS = [
    'category', 'patient_id', 'emu_id', 'admission_id', 'toc_id_before', 'toc_id_after',
    'gap_start_utc', 'gap_end_utc', 'duration_seconds',
]


# Columns every untrustworthy range has, in the order they should appear
# uncertainty_seconds_utc: the candidate missing-data duration (same quantity as the category's own
# gap, where one exists) - how much real time is unaccounted for, in seconds. brk_tick_uncertainty:
# a completely different unit (raw BRK ticks, not seconds) and NOT capped at anything - it's how
# wrong a process aligning on raw BRK ticks alone could be, which can dwarf the real duration.
COMMON_UNTRUSTWORTHY_FIELDS = [
    'patient_id', 'emu_id', 'admission_id', 'category', 'toc_id_before', 'toc_id_after',
    'first_chunk', 'last_chunk',
    'untrustworthy_start_utc', 'untrustworthy_end_utc', 'duration_seconds',
    'uncertainty_seconds_utc', 'brk_tick_uncertainty',
]

def find_simple_gap_events(disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """
    A plain, already-trustworthy discontinuity - data_coverage.py's first pass got the timing right,
    so there's nothing left here to reinterpret.

    Identification: any discontinuity row with insane=False - the reported duration came out of BRK
    tick math (or UTC math, across a TOC boundary) non-negative and within a sane range, with no sign
    of a corrupted tick or clock on either side.

    Nuance: "simple" describes how much we trust the timing, not the size or importance of the gap - a
    multi-hour outage with a clean tick on both sides is just as much a simple_gap as a sub-second one.
    Always a single-row event, since there's no ambiguity here to resolve.

    :param disc: discontinuity_summary.csv, containing every detected discontinuity, in on-disk row order
    :param pkt: packet_summary.csv (unused - accepted for interface consistency with the other
        find_<category>_events functions)
    :return: DataFrame with columns [disc_index, category, event_id], see find_packet_corruption_events
    """
    matches = disc.index[~disc['insane']]
    return pd.DataFrame({
        'disc_index': matches,
        'category': SIMPLE_GAP,
        'event_id': range(len(matches)),
    })


def calc_simple_gap(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """simple_gap's endpoints are already correct - read straight off the row, nothing recomputed."""
    row = disc.loc[disc_indices[0]]
    return {
        'category': SIMPLE_GAP,
        'patient_id': row['patient_id'],
        'emu_id': row['emu_id'],
        'admission_id': row['admission_id'],
        'toc_id_before': row['toc_id_before'],
        'toc_id_after': row['toc_id_after'],
        'gap_start_utc': pd.Timestamp(row['start_utc']).isoformat(),
        'gap_end_utc': pd.Timestamp(row['end_utc']).isoformat(),
        'duration_seconds': row['duration_hours'] * 3600,
    }


def find_packet_overlap_events(disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """
    The same sample written to the end of one file and the start of the next - a file join that
    overlaps by one tick more than usual.

    Identification: an insane discontinuity at a file boundary (cause='file edge') whose BRK tick
    difference is exactly -1 - one tick past the 0/+1 tick band data_coverage.py already tolerates
    as a normal artifact of how files get cut (see FILE_EDGE_TICK_TOLERANCE).

    Nuance: this isn't corruption - it's a duplicated sample, not a lost one - but a naive stitch
    across that file boundary would double-count one tick's worth of data if the overlap isn't
    deduplicated. Always a single-row event.

    :param disc: discontinuity_summary.csv, containing every detected discontinuity, in on-disk row order
    :param pkt: packet_summary.csv (unused - accepted for interface consistency with the other
        find_<category>_events functions)
    :return: DataFrame with columns [disc_index, category, event_id], see find_packet_corruption_events
    """
    # brk_ts_before/brk_ts_after load as uint64 (some corrupted rows exceed int64 range), so
    # brk_ts_after - brk_ts_before would silently wrap instead of going negative for a real -1 tick
    # drop. Comparing the other direction (before - after == 1) stays a small positive value, safe
    # in uint64.
    is_dedup = disc['insane'] & (disc['cause'] == 'file edge') & (disc['brk_ts_before'] - disc['brk_ts_after'] == 1)
    matches = disc.index[is_dedup]
    return pd.DataFrame({
        'disc_index': matches,
        'category': PACKET_OVERLAP,
        'event_id': range(len(matches)),
    })


def calculate_packet_overlap_gap(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame):
    """packet_overlap never represents missing data - the -1 tick implies the same datapoint was
    written to both the end of one packet and the start of the next, not a lost sample - so there's
    no gap to report.

    :return: always None
    """
    return None


def calculate_packet_overlap_untrustworthy(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """packet_overlap isn't a gap, but it IS an untrustworthy instant: the overlapping sample means
    a stitch across this file boundary can't be sure which packet's copy of that datapoint is the
    "real" one, or exactly where the true sample boundary falls. The disc row's own start_utc/end_utc
    are already the two packets' (end, start) UTCs, but in reversed order for a negative-duration row
    like this one - swapped here into a normal forward interval spanning just the 1-tick overlap.

    uncertainty_seconds_utc is that same 1-tick span - there's no missing data here, but this is the
    UTC-time size of the ambiguous overlap. brk_tick_uncertainty is, by definition of this category,
    always exactly 1 tick - a single -1 tick overlap's uncertainty doesn't compound on its own, but
    note this is single-event, not the whole-run kind of accumulation the tick-tolerance handling
    elsewhere is only a caveat about (see data_coverage.py's FILE_EDGE_TICK_TOLERANCE).

    :return: dict with the event's location, the untrustworthy interval (the 1-tick overlap), its
        duration, both uncertainty bounds, and the two joined chunks
    """
    row = disc.loc[disc_indices[0]]
    start_utc = pd.Timestamp(row['end_utc'])
    end_utc = pd.Timestamp(row['start_utc'])
    duration_seconds = (end_utc - start_utc).total_seconds()

    return {
        'category': PACKET_OVERLAP,
        'patient_id': row['patient_id'],
        'emu_id': row['emu_id'],
        'admission_id': row['admission_id'],
        'toc_id_before': row['toc_id_before'],
        'toc_id_after': row['toc_id_after'],
        'untrustworthy_start_utc': start_utc.isoformat(),
        'untrustworthy_end_utc': end_utc.isoformat(),
        'duration_seconds': duration_seconds,
        'uncertainty_seconds_utc': duration_seconds,
        'brk_tick_uncertainty': 1,
        'first_chunk': row['chunk_before'],
        'last_chunk': row['chunk_after'],
    }


def find_packet_corruption_events(disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """
    A corrupted BRK tick on a TOC's last packet, propagating forward into the boundary with the next TOC.

    Identification: an insane discontinuity within a single chunk file (cause='other'), typically a
    nonsensical jump of hundreds of years, followed a reset back to a reasonable UTC time on TOC reset.

    Nuance: a single write error can garble more than one packet and each garbled packet's own tick is unreliable,
    so the discontinuity log alone can't tell us where the corruption actually started. Instead, we walk backward
    through the raw packets (see _walk_back_to_anchor) to find the last one that has a plausible sample count and
    clock tick. We fold in every discontinuity from there through the TOC reset (including the TOC reset) as one
    event.

    :param disc: discontinuity_summary.csv, containing every detected discontinuity, in on-disk row order
    :param pkt: packet_summary.csv, containing every individual packet and its size
    :return: DataFrame with columns [disc_index, category, event_id] - one row per discontinuity_summary.csv
        row belonging to this category. disc_index is a label into disc.index; event_id groups the rows
        of one real-world event together
    """
    records = []
    event_id = 0
    for i in range(len(disc) - 1):
        a, b = disc.iloc[i], disc.iloc[i + 1]
        if (a['toc_id_after'] == b['toc_id_before'] and a['chunk_after'] == b['chunk_before']         # No missing data
                and a['patient_id'] == b['patient_id'] and a['admission_id'] == b['admission_id']        # same patient
                and a['insane'] and b['insane']                                          # Two nonsense discontinuities
                and a['cause'] == 'other' and b['cause'] == 'TOC reset'              # Crazy jump followed by TOC reset
        ):
            patient, admission, toc_id = a['patient_id'], a['admission_id'], a['toc_id_after']
            pkt_slice = _toc_packet_slice(pkt, patient, admission, toc_id)
            start_pos = _packet_position_by_number(pkt_slice, a['chunk_after'], a['packet_after'])
            anchor_pos, _ = _walk_back_to_anchor(pkt_slice, start_pos)
            first_corrupt = pkt_slice.loc[anchor_pos + 1]

            match = disc.index[
                (disc['patient_id'] == patient) & (disc['admission_id'] == admission) & (disc['toc_id_before'] == toc_id)
                & (disc['chunk_after'] == first_corrupt['chunk_id']) & (disc['packet_after'] == first_corrupt['packet_number'])
            ]
            start = disc.index.get_loc(match[0]) if len(match) else i
            while start < i and not disc.iloc[start]['insane']:
                # The transition into the first corrupted packet can itself be sane (e.g. the anchor
                # was pushed one packet further back by a zero-point rejection, not an insane tick) -
                # only the insane rows after it are evidence of corruption.
                start += 1

            for pos in range(start, i + 2):
                records.append({'disc_index': disc.index[pos], 'category': PACKET_CORRUPTION, 'event_id': event_id})
            event_id += 1
    return pd.DataFrame(records, columns=['disc_index', 'category', 'event_id'])


def find_clock_reset_events(disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """
    The BRK clock resetting back to zero (or near zero) mid-recording, once or repeatedly

    Identification: an insane discontinuity within a single chunk file (cause='other') whose BRK tick
    difference is a large negative drop - the clock suddenly reading far behind where it should be. Usually
    followed by a large positive discontinuity as the time is fixed on the next TOC reset

    Nuance: once the clock has dropped like this, every discontinuity from there through the TOC reset
    is folded into the same event, as the clock never recovers. A single event can also log the exact
    same (before, after) tick pair over and over while the clock is stuck oscillating between a small set of values;
    repeats are flagged via `is_repeat` rather than counted as new sub-events.

    :param disc: discontinuity_summary.csv, containing every detected discontinuity, in on-disk row order
    :param pkt: packet_summary.csv (unused, accepted for interface consistency)
    :return: DataFrame with columns [disc_index, category, event_id, is_repeat], see
        find_packet_corruption_events
    """
    records = []
    event_id = 0
    n = len(disc)
    i = 0
    while i < n:
        row = disc.iloc[i]
        tick_diff = int(row['brk_ts_after']) - int(row['brk_ts_before'])
        is_seed = row['insane'] and row['cause'] == 'other' and tick_diff < 0 and tick_diff
        if not is_seed:
            i += 1
            continue

        patient_id, admission_id = row['patient_id'], row['admission_id']
        j = i
        while j < n:
            r = disc.iloc[j]
            if r['patient_id'] != patient_id or r['admission_id'] != admission_id or r['cause'] == 'TOC reset':
                break
            j += 1
        event_end = j if j < n and disc.iloc[j]['patient_id'] == patient_id and disc.iloc[j]['admission_id'] == admission_id else j - 1

        seen_pairs = set()
        for pos in range(i, event_end + 1):
            r = disc.iloc[pos]
            pair = (r['brk_ts_before'], r['brk_ts_after'])
            records.append({
                'disc_index': disc.index[pos], 'category': CLOCK_RESET, 'event_id': event_id,
                'is_repeat': pair in seen_pairs,
            })
            seen_pairs.add(pair)
        event_id += 1
        i = event_end + 1
    return pd.DataFrame(records, columns=['disc_index', 'category', 'event_id', 'is_repeat'])


def _max_tick_delta(disc: pd.DataFrame, disc_indices: list) -> int:
    """The single largest raw |brk_ts_after - brk_ts_before|, in ticks, among an event's disc rows,
    excluding `TOC reset` rows (those compare ticks across a TOC boundary via UTC, not a same-clock
    tick delta) - shared by clock_reset and packet_corruption's brk_tick_uncertainty. Reported in raw
    BRK ticks, not seconds: a process aligning on raw ticks alone is off by this many ticks, and
    converting to seconds would just be re-deriving a number nobody aligning on ticks actually uses."""
    event_rows = disc.loc[disc_indices]
    tick_rows = event_rows[event_rows['cause'] != 'TOC reset']
    return max(abs(int(r.brk_ts_after) - int(r.brk_ts_before)) for r in tick_rows.itertuples())


def _clock_reset_span(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """Shared computation behind calculate_clock_reset_gap and calculate_clock_reset_untrustworthy_range
    - both need the same anchor/next-TOC endpoints, just report different parts of them.

    :return: dict with row_i/row_j/toc_id/patient_id/admission_id, last_faithful_utc/next_toc_utc/
        real_seconds/datapoint_seconds (see calculate_clock_reset_gap for what these mean),
        gap_seconds (= real_seconds - datapoint_seconds, the candidate missing-data duration - also
        used as the untrustworthy range's UTC-based uncertainty: we know exactly how much data is
        missing from this span, just not exactly where within it, so that amount IS the UTC
        uncertainty, a much tighter bound than the raw tick delta), first_chunk/last_chunk (the
        chunk right after the last faithful packet, through the TOC's own last chunk - every chunk
        in between has an untrustworthy clock), and max_delta_ticks (the single largest raw tick jump
        logged for this event, in raw BRK ticks - not seconds - excluding the terminating `TOC reset`
        row itself since that's a cross-TOC UTC comparison, not a same-clock tick delta. A process
        that aligns on raw BRK ticks alone can genuinely be off by this many ticks, a completely
        separate concern from gap_seconds' UTC-based bound - see calculate_clock_reset_untrustworthy,
        which reports both)
    """
    i, j = min(disc_indices), max(disc_indices)
    row_i, row_j = disc.loc[i], disc.loc[j]
    toc_id = row_i['toc_id_before']
    patient_id, admission_id = row_i['patient_id'], row_i['admission_id']

    # row_i's own "before" packet is trustworthy by construction (see find_clock_reset_events), so
    # its identity (chunk_before, packet_before) - already in disc - locates it directly, no need to
    # search packet_summary.csv by tick value.
    pkt_slice = _toc_packet_slice(pkt, patient_id, admission_id, toc_id)
    anchor_pos = _packet_position_by_number(pkt_slice, row_i['chunk_before'], row_i['packet_before'])
    total_points = pkt_slice.loc[anchor_pos + 1:, 'n_points'].sum()
    datapoint_seconds = total_points / BRK_RESOLUTION_HZ

    last_faithful_utc = pd.Timestamp(row_i['start_utc'])
    next_toc_utc = pd.Timestamp(row_j['end_utc'])
    real_seconds = (next_toc_utc - last_faithful_utc).total_seconds()

    return {
        'row_i': row_i, 'row_j': row_j, 'toc_id': toc_id, 'patient_id': patient_id, 'admission_id': admission_id,
        'last_faithful_utc': last_faithful_utc, 'next_toc_utc': next_toc_utc,
        'real_seconds': real_seconds, 'datapoint_seconds': datapoint_seconds,
        'gap_seconds': real_seconds - datapoint_seconds,
        'first_chunk': int(pkt_slice.loc[anchor_pos + 1, 'chunk_id']),
        'last_chunk': int(pkt_slice.iloc[-1]['chunk_id']),
        'max_delta_ticks': _max_tick_delta(disc, disc_indices),
    }


def calculate_clock_reset_gap(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """The candidate missing-data interval for one clock_reset event: compare the UTC time really
    elapsed against the time implied by the datapoints actually recorded while the clock was broken,
    and treat any shortfall as the gap, ending exactly at the next TOC's first trustworthy moment
    (i.e. anchored to the known-good side, working backward) - since the recording only resumes
    reliably once the next TOC starts:
      - `real_seconds`: UTC gap between the last faithful timestamp before the drop (the seed row's
        own "before" endpoint - trustworthy by construction, see find_clock_reset_events) and the
        first faithful timestamp of the next TOC (the terminating `TOC reset` row's "after" endpoint).
      - `datapoint_seconds`: every packet in packet_summary.csv strictly after the last faithful
        packet through the end of this TOC - still real recorded samples, just with an untrustworthy
        timestamp - converted to seconds via the TOC's own sample resolution (assumes 1 tick/sample,
        standard for NS5).
      - `duration_seconds = real_seconds - datapoint_seconds`: the shortfall, i.e. the candidate
        missing-data duration.

    Note this is a NARROWER interval than calculate_clock_reset_untrustworthy_range's - the gap is
    only the shortfall, not the whole untrustworthy span (real recorded samples with bad timestamps
    fill most of that span - see that function).

    :param disc_indices: every disc.index label for this event, as produced by
        find_clock_reset_events (any order) - the earliest is the seed row, the latest is the
        terminating `TOC reset` row
    :param disc: discontinuity_summary.csv
    :param pkt: packet_summary.csv
    :return: dict with the event's location, both real/datapoint-implied durations, both gap
        endpoint UTCs, and the resulting gap duration
    """
    span = _clock_reset_span(disc_indices, disc, pkt)
    row_i, row_j = span['row_i'], span['row_j']
    duration_seconds = span['gap_seconds']

    return {
        'category': CLOCK_RESET,
        'patient_id': span['patient_id'],
        'emu_id': row_i['emu_id'],
        'admission_id': span['admission_id'],
        'toc_id_before': span['toc_id'],
        'toc_id_after': row_j['toc_id_after'],
        'gap_start_utc': (span['next_toc_utc'] - pd.Timedelta(seconds=duration_seconds)).isoformat(),
        'gap_end_utc': span['next_toc_utc'].isoformat(),
        'duration_seconds': duration_seconds,
        'real_seconds': span['real_seconds'],
        'datapoint_seconds': span['datapoint_seconds'],
    }


def calculate_clock_reset_untrustworthy(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """The full span during which clock_reset makes the BRK clock untrustworthy: from the last
    faithful timestamp through the next TOC's first trustworthy moment (span['real_seconds'] long) -
    wider than calculate_clock_reset_gap's own interval, since most of this span is real recorded
    data, just with individually untrustworthy timestamps. Reuses _clock_reset_span, the same
    computation calculate_clock_reset_gap itself is built on.

    uncertainty_seconds_utc is span['gap_seconds'] - the same shortfall calculate_clock_reset_gap
    reports. We know exactly how many datapoints were recorded in this span and in what order, so we
    know exactly how much time is unaccounted for; we just don't know WHERE in the span that missing
    time falls. brk_tick_uncertainty is a different unit entirely (raw ticks, not seconds) and NOT
    capped - see _max_tick_delta.

    :return: dict with the event's location, the untrustworthy interval, its duration, the missing-
        data-duration uncertainty (uncertainty_seconds_utc) and the raw-tick uncertainty
        (brk_tick_uncertainty), and the first/last chunk affected
    """
    span = _clock_reset_span(disc_indices, disc, pkt)
    row_i, row_j = span['row_i'], span['row_j']

    return {
        'category': CLOCK_RESET,
        'patient_id': span['patient_id'],
        'emu_id': row_i['emu_id'],
        'admission_id': span['admission_id'],
        'toc_id_before': span['toc_id'],
        'toc_id_after': row_j['toc_id_after'],
        'untrustworthy_start_utc': span['last_faithful_utc'].isoformat(),
        'untrustworthy_end_utc': span['next_toc_utc'].isoformat(),
        'duration_seconds': span['real_seconds'],
        'uncertainty_seconds_utc': span['gap_seconds'],
        'brk_tick_uncertainty': span['max_delta_ticks'],
        'first_chunk': span['first_chunk'],
        'last_chunk': span['last_chunk'],
    }


def _toc_packet_slice(pkt: pd.DataFrame, patient_id, admission_id, toc_id) -> pd.DataFrame:
    """packet_summary.csv rows for one admission's one TOC, in true chronological order,
    reindexed 0..n-1 - the scope _walk_back_to_anchor walks within."""
    mask = (pkt['patient_id'] == patient_id) & (pkt['admission_id'] == admission_id) & (pkt['toc_id'] == toc_id)
    return pkt[mask].reset_index(drop=True)


def _packet_position_by_number(pkt_slice: pd.DataFrame, chunk_id, packet_number) -> int:
    """Position of the packet matching (chunk_id, packet_number) within pkt_slice - an exact,
    unambiguous identity now that discontinuity_summary.csv carries packet_before/packet_after
    directly. Replaces the old brk-tick-value-based lookups: a corrupted row's own tick is exactly
    what's untrustworthy, and a chunk-only lookup is ambiguous whenever a chunk holds more than one
    packet - packet_number has neither problem."""
    matches = (pkt_slice['chunk_id'] == chunk_id) & (pkt_slice['packet_number'] == packet_number)
    return pkt_slice.index[matches][0]


def _walk_back_to_anchor(pkt_slice: pd.DataFrame, start_pos: int):
    """Step backward through pkt_slice (one admission's one TOC, in true chronological order) from
    just before start_pos, to find the most recent packet that is BOTH:
      - a sane continuation of its own predecessor - via is_insane on the raw tick difference, the
        same test data_coverage.py itself uses to decide whether to log a discontinuity, and
      - non-degenerate (n_points > 0) - a 0-point packet is itself a suspicious reading, not a
        trustworthy endpoint.
    Stops at the start of pkt_slice.

    :return: (position, the anchor packet) - the position is returned too since callers typically
        need the very next packet (position + 1), the first actually-untrustworthy one
    """
    pos = start_pos - 1
    while pos > 0:
        prev, curr = pkt_slice.loc[pos - 1], pkt_slice.loc[pos]
        tick_diff = curr['brk_start'] - (prev['brk_start'] + prev['n_points'])
        if is_insane(tick_diff / BRK_RESOLUTION_HZ) or curr['n_points'] == 0:
            pos -= 1
            continue
        break
    return pos, pkt_slice.loc[pos]


def _packet_corruption_span(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """Shared computation behind calculate_packet_corruption_gap and
    calculate_packet_corruption_untrustworthy_range - here the two intervals coincide exactly (every
    packet from the anchor to the TOC's end has a corrupted tick, so the whole span is both the
    untrustworthy range AND the candidate missing-data gap), so both functions build their dict from
    this one computation.

    :return: dict with row_i/row_j/toc_id/patient_id/admission_id, anchor (the last faithful packet)
        and its end UTC, next_toc_utc, duration_seconds, first_chunk/
        last_chunk (the chunk right after the anchor, through the TOC's own last chunk),
        and max_delta_ticks (the corrupted tick's own raw magnitude vs. its predecessor, in raw BRK
        ticks - not seconds - see _max_tick_delta)
    """
    i, j = min(disc_indices), max(disc_indices)
    row_i, row_j = disc.loc[i], disc.loc[j]
    toc_id = row_i['toc_id_after']
    patient_id, admission_id = row_i['patient_id'], row_i['admission_id']

    pkt_slice = _toc_packet_slice(pkt, patient_id, admission_id, toc_id)
    start_pos = _packet_position_by_number(pkt_slice, row_i['chunk_after'], row_i['packet_after'])
    anchor_pos, anchor = _walk_back_to_anchor(pkt_slice, start_pos)

    # UTC of the anchor's END tick, via the linear tick<->UTC mapping already established by
    # row_i's own (brk_ts_before, start_utc) pair - valid for the whole TOC, since a TOC has one
    # shared origin (see data_coverage.py's toc_origin/toc_packets).
    anchor_end_tick = int(anchor['brk_start']) + int(anchor['n_points'])
    origin_tick = int(row_i['brk_ts_before'])
    origin_utc = pd.Timestamp(row_i['start_utc'])
    anchor_end_utc = origin_utc + pd.Timedelta(seconds=(anchor_end_tick - origin_tick) / BRK_RESOLUTION_HZ)

    next_toc_utc = pd.Timestamp(row_j['end_utc'])

    return {
        'row_i': row_i, 'row_j': row_j, 'toc_id': toc_id, 'patient_id': patient_id, 'admission_id': admission_id,
        'anchor': anchor, 'anchor_end_utc': anchor_end_utc, 'next_toc_utc': next_toc_utc,
        'duration_seconds': (next_toc_utc - anchor_end_utc).total_seconds(),
        'first_chunk': int(pkt_slice.loc[anchor_pos + 1, 'chunk_id']),
        'last_chunk': int(pkt_slice.iloc[-1]['chunk_id']),
        'max_delta_ticks': _max_tick_delta(disc, disc_indices),
    }


def calc_packet_corruption_gap(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """The candidate missing-data interval for one packet_corruption event: from the end of the
    last faithful packet before the corruption (found by walking packet_summary.csv directly - the
    discontinuity log alone is too sparse, since a long clean run can span many packets with zero
    logged rows) to the start of the next TOC's first trustworthy packet.

    :param disc_indices: every disc.index label for this event, as produced by
        find_packet_corruption_events (any order) - the earliest is the first corrupted-packet row,
        the latest is the TOC-reset row
    :param disc: discontinuity_summary.csv
    :param pkt: packet_summary.csv
    :return: dict with the event's location, anchor packet identity, both gap endpoint UTCs, and
        the resulting gap duration
    """
    span = _packet_corruption_span(disc_indices, disc, pkt)
    row_i, row_j, anchor = span['row_i'], span['row_j'], span['anchor']

    return {
        'category': PACKET_CORRUPTION,
        'patient_id': span['patient_id'],
        'emu_id': row_i['emu_id'],
        'admission_id': span['admission_id'],
        'toc_id_before': span['toc_id'],
        'toc_id_after': row_j['toc_id_after'],
        'gap_start_utc': span['anchor_end_utc'].isoformat(),
        'gap_end_utc': span['next_toc_utc'].isoformat(),
        'duration_seconds': span['duration_seconds'],
        'anchor_chunk': int(anchor['chunk_id']),
        'anchor_packet': int(anchor['packet_number']),
        'anchor_n_points': int(anchor['n_points']),
    }


def calc_packet_corruption_untrustworthy(disc_indices: list, disc: pd.DataFrame, pkt: pd.DataFrame) -> dict:
    """The span during which packet_corruption makes the BRK clock untrustworthy - identical to
    calculate_packet_corruption_gap's own interval here (every packet in it has a corrupted tick, so
    unlike clock_reset there's no narrower "shortfall" to separate out). Reuses _packet_corruption_span,
    the same computation calculate_packet_corruption_gap itself is built on.

    uncertainty_seconds_utc is just the span's own duration here: unlike clock_reset, we can't cross-
    check against a real datapoint count to narrow it down - the corrupted packet's own n_points is
    itself garbled (see scripts/discontinuity_types.md), so the entire span is candidate missing
    data, not just part of it. brk_tick_uncertainty is a different unit entirely (raw ticks, not
    seconds) and NOT capped - it's the corrupted tick's own raw magnitude vs. its predecessor (see
    _max_tick_delta), which can dwarf the real span by many orders of magnitude: a process that
    aligns on raw BRK ticks alone could be off by that many ticks.

    :return: dict with the event's location, the untrustworthy interval, its duration, the missing-
        data-duration uncertainty (uncertainty_seconds_utc) and the raw-tick uncertainty
        (brk_tick_uncertainty), and the first/last chunk affected
    """
    span = _packet_corruption_span(disc_indices, disc, pkt)
    row_i, row_j = span['row_i'], span['row_j']

    return {
        'category': PACKET_CORRUPTION,
        'patient_id': span['patient_id'],
        'emu_id': row_i['emu_id'],
        'admission_id': span['admission_id'],
        'toc_id_before': span['toc_id'],
        'toc_id_after': row_j['toc_id_after'],
        'untrustworthy_start_utc': span['anchor_end_utc'].isoformat(),
        'untrustworthy_end_utc': span['next_toc_utc'].isoformat(),
        'duration_seconds': span['duration_seconds'],
        'uncertainty_seconds_utc': span['duration_seconds'],
        'brk_tick_uncertainty': span['max_delta_ticks'],
        'first_chunk': span['first_chunk'],
        'last_chunk': span['last_chunk'],
    }


# Registries wiring each category's identify/calculate pair together - new categories (e.g. the
# two clock-reset/recovery variants) are added by writing their find_<category>_events/
# calculate_<category>_gap functions above and registering them here. Dict order is the tiebreak
# resolve_overlaps uses when two events start on the same row.
EVENT_FINDERS = {
    SIMPLE_GAP: find_simple_gap_events,
    PACKET_OVERLAP: find_packet_overlap_events,
    PACKET_CORRUPTION: find_packet_corruption_events,
    CLOCK_RESET: find_clock_reset_events,
}

GAP_CALCULATORS = {
    SIMPLE_GAP: calc_simple_gap,
    PACKET_OVERLAP: calculate_packet_overlap_gap,
    PACKET_CORRUPTION: calc_packet_corruption_gap,
    CLOCK_RESET: calculate_clock_reset_gap,
}

UNTRUSTWORTHY_CALCULATORS = {
    PACKET_OVERLAP: calculate_packet_overlap_untrustworthy,
    PACKET_CORRUPTION: calc_packet_corruption_untrustworthy,
    CLOCK_RESET: calculate_clock_reset_untrustworthy,
}


def identify_all(disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """Run every registered find_<category>_events function, combine into one events table sharing
    disc's row labels (columns: disc_index, category, event_id, ...), and resolve any rows two
    different events both claimed (see resolve_overlaps)."""
    combined = pd.concat([finder(disc, pkt) for finder in EVENT_FINDERS.values()], ignore_index=True)
    return resolve_overlaps(combined)


def resolve_overlaps(events: pd.DataFrame) -> pd.DataFrame:
    """When more than one event (regardless of category) claims the same disc_index, only the
    event that starts earliest keeps that row - ties broken by EVENT_FINDERS registration order
    (earlier-registered category wins). The losing event's other, non-contested rows are unaffected;
    only the specific rows it lost are dropped from it. Every row that had more than one claimant
    gets an `overlap_note` recording what else matched it, so the conflict is visible in the final
    output rather than silently resolved.

    :param events: concatenated output of the find_<category>_events functions (may contain
        overlapping disc_index values across different (category, event_id) groups)
    :return: events with at most one (category, event_id) per disc_index, plus an `overlap_note`
        column (None where there was no conflict)
    """
    if events.empty:
        return events.assign(overlap_note=None)

    category_rank = {cat: rank for rank, cat in enumerate(EVENT_FINDERS)}
    event_starts = events.groupby(['category', 'event_id'])['disc_index'].min()
    event_order = sorted(event_starts.index, key=lambda key: (event_starts[key], category_rank[key[0]]))

    claimed_by = {}  # disc_index -> winning (category, event_id), first event in event_order wins
    for key in event_order:
        for disc_index in events.loc[(events['category'] == key[0]) & (events['event_id'] == key[1]), 'disc_index']:
            claimed_by.setdefault(disc_index, key)

    claimants = events.groupby('disc_index').apply(lambda d: list(zip(d['category'], d['event_id'])), include_groups=False)

    def overlap_note(row):
        others = [c for c in claimants[row['disc_index']] if c != (row['category'], row['event_id'])]
        return f'also matched {", ".join(f"{cat} event {eid}" for cat, eid in others)}' if others else None

    is_winner = events.apply(lambda r: (r['category'], r['event_id']) == claimed_by[r['disc_index']], axis=1)
    resolved = events[is_winner].copy()
    resolved['overlap_note'] = resolved.apply(overlap_note, axis=1)
    return resolved.reset_index(drop=True)


def label_discontinuities(disc: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """disc with `category`/`event_id` columns attached from `events` (see identify_all) - every
    row is kept, including ones no find_<category>_events function claimed (category/event_id left
    null for those), so the labeling can be inspected/spot-checked against the full discontinuity
    log before any gap is calculated from it. Sorted by (patient_id, admission_id, toc_id_before)."""
    labeled = disc.merge(events, how='left', left_index=True, right_on='disc_index').drop(columns='disc_index')
    return labeled.sort_values(['patient_id', 'admission_id', 'toc_id_before'], kind='stable').reset_index(drop=True)


def _calc_all(events: pd.DataFrame, disc: pd.DataFrame, pkt: pd.DataFrame, calculators: dict, common_fields: list) -> pd.DataFrame:
    """Shared by calculate_all_gaps and calculate_all_untrustworthy_ranges: run `calculators[category]`
    for every event in `events`, as one table with `common_fields` first and any category-specific
    columns (often empty outside their own category) pushed to the end, sorted by
    (patient_id, admission_id, toc_id_before). Two kinds of event contribute nothing to the result:
      - a category with no entry in `calculators` (identification exists, this calculation doesn't
        yet) - skipped, not an error, since the pipeline should still run end-to-end on whatever's
        ready;
      - a calculator that returns None for a given event (the category is known not to apply, e.g.
        packet_overlap has no gap).
    """
    rows = []
    for (category, event_id), group in events.groupby(['category', 'event_id']):
        calculator = calculators.get(category)
        if calculator is None:
            continue
        row = calculator(group['disc_index'].tolist(), disc, pkt)
        if row is not None:
            rows.append(row)

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    extra_fields = [c for c in result.columns if c not in common_fields]
    result = result[[c for c in common_fields if c in result.columns] + extra_fields]
    return result.sort_values(['patient_id', 'admission_id', 'toc_id_before'], kind='stable').reset_index(drop=True)


def calc_all_gaps(events: pd.DataFrame, disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """calculate_<category>_gap for every event in `events` (as returned by identify_all or one of
    the find_<category>_events functions), as one table - the reduced/corrected list of real data
    gaps. See _calculate_all for the shared mechanics."""
    return _calc_all(events, disc, pkt, GAP_CALCULATORS, COMMON_GAP_FIELDS)


def calc_all_untrustworthy(events: pd.DataFrame, disc: pd.DataFrame, pkt: pd.DataFrame) -> pd.DataFrame:
    """calculate_<category>_untrustworthy_range for every event in `events`, as one table - every
    time range where the BRK clock can't be trusted, which is NOT the same set of categories or (for
    clock_reset) the same interval as calculate_all_gaps: see UNTRUSTWORTHY_RANGE_CALCULATORS and
    each calculate_<category>_untrustworthy_range's own docstring. See _calculate_all for the shared
    mechanics."""
    return _calc_all(events, disc, pkt, UNTRUSTWORTHY_CALCULATORS, COMMON_UNTRUSTWORTHY_FIELDS)


def _category_event_counts(events: pd.DataFrame, disc: pd.DataFrame) -> pd.DataFrame:
    """One row per admission, one `n_<category>` column per category in EVENT_FINDERS (plus
    `n_unlabeled` for disc rows no finder claimed) - counts EVENTS (unique (category, event_id)
    pairs), not raw disc rows, so a category like clock_reset that can span many rows per event
    isn't inflated relative to a single-row category like simple_gap."""
    labeled = disc[['patient_id', 'admission_id']].merge(events, how='left', left_index=True, right_on='disc_index')
    labeled['category'] = labeled['category'].fillna('unlabeled')
    counts = labeled.drop_duplicates(['patient_id', 'admission_id', 'category', 'event_id']) \
        .groupby(['patient_id', 'admission_id', 'category']).size().unstack(fill_value=0)
    counts.columns = [f'n_{c}' for c in counts.columns]
    return counts.reset_index()


def calc_visit_summary(visit: pd.DataFrame, gaps: pd.DataFrame, events: pd.DataFrame, disc: pd.DataFrame) -> pd.DataFrame:
    """Recompute each admission's coverage stats from the reduced/corrected gap list (see
    calculate_all_gaps), replacing data_coverage.py's own n_discontinuities/total_discontinuity_hours/
    total_continuous_hours/pct_coverage/n_insane_discontinuities - those were derived from the raw,
    uncategorized discontinuity_summary.csv, so an admission with a lot of insane rows (e.g. a
    packet_corruption or clock_reset event logged as many rows) could show a wildly wrong duration.
    recording_start_utc/recording_end_utc/total_span_hours aren't affected by categorization, so
    they're carried over from `visit` unchanged. Also adds one `n_<category>` column per category
    (event counts, not raw row counts - see _category_event_counts), plus `n_unlabeled` for rows no
    finder claimed, so it's visible at a glance what's driving each admission's total.

    :param visit: visit_summary.csv, data_coverage.py's own per-admission output
    :param gaps: calculate_all_gaps' output
    :param events: identify_all's output, for the per-category breakdown
    :param disc: discontinuity_summary.csv, to map events' disc_index back to patient/admission
    :return: one row per admission in `visit`, sorted by (patient_id, admission_id), with
        n_gaps/total_gap_hours/total_continuous_hours/pct_coverage recomputed from `gaps`
        (an admission with no gaps gets n_gaps=0, total_gap_hours=0.0, pct_coverage=100), plus the
        per-category event-count columns
    """
    per_admission = gaps.groupby(['patient_id', 'admission_id']).agg(
        n_gaps=('duration_seconds', 'size'), total_gap_hours=('duration_seconds', lambda s: s.sum() / 3600),
    ).reset_index() if not gaps.empty else pd.DataFrame(columns=['patient_id', 'admission_id', 'n_gaps', 'total_gap_hours'])

    result = visit.merge(per_admission, on=['patient_id', 'admission_id'], how='left')
    result['n_gaps'] = result['n_gaps'].fillna(0).astype(int)
    result['total_gap_hours'] = result['total_gap_hours'].fillna(0.0)
    result['total_continuous_hours'] = result['total_span_hours'] - result['total_gap_hours']
    result['pct_coverage'] = 100 * result['total_continuous_hours'] / result['total_span_hours']

    category_counts = _category_event_counts(events, disc)
    result = result.merge(category_counts, on=['patient_id', 'admission_id'], how='left')
    for col in category_counts.columns.difference(['patient_id', 'admission_id']):
        result[col] = result[col].fillna(0).astype(int)

    drop_fields = ['n_discontinuities', 'total_discontinuity_hours', 'n_insane_discontinuities']
    result = result.drop(columns=[c for c in drop_fields if c in result.columns])
    return result.sort_values(['patient_id', 'admission_id'], kind='stable').reset_index(drop=True)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Identify discontinuity categories, and optionally label discontinuity_summary.csv with them and/or calculate the resulting gaps.'
    )
    parser.add_argument('data_dir', help='Directory containing discontinuity_summary.csv, packet_summary.csv, and (for --visits) visit_summary.csv')
    parser.add_argument('--labels', action='store_true', help='Write the labeled discontinuities CSV (<data_dir>/discontinuity_labeled.csv)')
    parser.add_argument('--gaps', action='store_true', help='Write the calculated gaps CSV (<data_dir>/gaps.csv)')
    parser.add_argument('--visits', action='store_true', help='Write the corrected visit summary CSV (<data_dir>/visit_summary_corrected.csv)')
    parser.add_argument('--untrustworthy', action='store_true', help='Write the untrustworthy-clock time ranges CSV (<data_dir>/untrustworthy_ranges.csv)')
    args = parser.parse_args()

    disc_df = pd.read_csv(f'{args.data_dir}/discontinuity_summary.csv')
    pkt_df = pd.read_csv(f'{args.data_dir}/packet_summary.csv')
    events_df = identify_all(disc_df, pkt_df)

    print(f'{events_df["disc_index"].nunique()} of {len(disc_df)} rows labeled')
    print(events_df['category'].value_counts(dropna=False).to_string())

    if args.labels:
        labels_path = f'{args.data_dir}/discontinuity_labeled.csv'
        label_discontinuities(disc_df, events_df).to_csv(labels_path, index=False)
        print(f'Wrote {labels_path}')

    if args.gaps or args.visits:
        not_ready = sorted(set(events_df['category']) - set(GAP_CALCULATORS))
        if not_ready:
            print(f'No calculate_gap implemented yet for: {not_ready} - their events are excluded from the gaps output')
        gaps_df = calc_all_gaps(events_df, disc_df, pkt_df)

    if args.gaps:
        gaps_path = f'{args.data_dir}/gaps.csv'
        gaps_df.to_csv(gaps_path, index=False)
        print(f'Wrote {gaps_path}')

    if args.visits:
        visit_df = pd.read_csv(f'{args.data_dir}/visit_summary.csv')
        visits_path = f'{args.data_dir}/visit_summary_corrected.csv'
        calc_visit_summary(visit_df, gaps_df, events_df, disc_df).to_csv(visits_path, index=False)
        print(f'Wrote {visits_path}')

    if args.untrustworthy:
        not_ready = sorted(set(events_df['category']) - set(UNTRUSTWORTHY_CALCULATORS) - {SIMPLE_GAP})
        if not_ready:
            print(f'No calculate_untrustworthy_range implemented yet for: {not_ready} - their events are excluded from this output')
        untrustworthy_path = f'{args.data_dir}/untrustworthy_ranges.csv'
        calc_all_untrustworthy(events_df, disc_df, pkt_df).to_csv(untrustworthy_path, index=False)
        print(f'Wrote {untrustworthy_path}')
