"""
TEMPORARY, standalone diagnostic script - not part of the regular pipeline. Delete once the
reset+recovery investigation in gap_types.md is done.

For a given patient/admission/TOC and a range of chunk_ids, walks every raw packet across those
chunk files (in chunk_id, then brk_start order) and prints, per packet:
    - the raw brk_start tick and n_points (sample count)
    - the packet's implied brk_end tick
    - UTC start/end computed two ways: anchored to the whole TOC's shared origin (its
      chronologically-first chunk, matching data_coverage.py's convention), and anchored to this
      packet's own file's TimeOrigin
and between each consecutive pair of packets (including across a chunk boundary):
    - missing ticks, missing UTC per the TOC anchor, missing UTC per the file anchor

Each chunk's own file TimeOrigin/TimeStampResolution/first tick is printed once, before its
packets.

Usage:
    python scripts/investigate_toc_packets.py --patient EMU-ID --toc-id N
        --chunk-start N --chunk-end N [--admission N]
"""

import argparse
from dataclasses import dataclass
from datetime import datetime

import datajoint as dj
from brpylib import NsxFile
from pyNsXStitch.helpers import brk_toc_ticks_to_utc
from pyNsXStitch.streamers import iter_nsx_timestamps

from scripts.diagnostics.gaps.data_coverage import ChunkFile, MAX_SANE_OFFSET_SECONDS, NSP_ID, toc_origin

dj.config['filepath_checksum_size_limit'] = 0


def safe_utc(tick, origin_utc, first_tick, ts_resolution):
    """Same overflow guard as data_coverage.py's _safe_utc - clamps to datetime.min/max instead
    of raising when the offset is too large to represent as a datetime."""
    offset_seconds = (tick - first_tick) / ts_resolution
    if abs(offset_seconds) > MAX_SANE_OFFSET_SECONDS:
        return datetime.max if offset_seconds > 0 else datetime.min
    return brk_toc_ticks_to_utc(tick, toc_start_utc=origin_utc, toc_start_brk=first_tick, toc_ts_freq=ts_resolution)


@dataclass
class PacketInfo:
    chunk_id: int
    brk_start: int
    n_points: int
    brk_end: int
    utc_start_toc: datetime
    utc_end_toc: datetime
    utc_start_file: datetime
    utc_end_file: datetime


def read_chunk_packets(chunk: ChunkFile, toc_origin_utc, toc_ts_resolution, toc_first_tick):
    """Every packet in chunk, with UTC computed both against the TOC-shared origin and against
    this chunk's own file's independent TimeOrigin."""
    file_origin_utc, file_ts_resolution, file_first_tick = toc_origin([chunk])
    print(
        f'  Chunk {chunk.chunk_id} file TimeOrigin: {file_origin_utc.isoformat()} '
        f'(ts_resolution={file_ts_resolution}, first_tick={file_first_tick})'
    )

    packets = []
    nsx_file = NsxFile(chunk.filepath, verbose=False, interactive=False)
    try:
        sample_freq = nsx_file.basic_header['SampleResolution'] / nsx_file.basic_header['Period']
        ticks_per_sample = toc_ts_resolution / sample_freq
        for brk_start, n_points in iter_nsx_timestamps(nsx_file):
            brk_end = brk_start + int(round(n_points * ticks_per_sample))
            packets.append(PacketInfo(
                chunk_id=chunk.chunk_id,
                brk_start=brk_start,
                n_points=n_points,
                brk_end=brk_end,
                utc_start_toc=safe_utc(brk_start, toc_origin_utc, toc_first_tick, toc_ts_resolution),
                utc_end_toc=safe_utc(brk_end, toc_origin_utc, toc_first_tick, toc_ts_resolution),
                utc_start_file=safe_utc(brk_start, file_origin_utc, file_first_tick, file_ts_resolution),
                utc_end_file=safe_utc(brk_end, file_origin_utc, file_first_tick, file_ts_resolution),
            ))
    finally:
        nsx_file.datafile.close()
    return packets


def print_packet(p: PacketInfo):
    print(f'    packet: brk_start={p.brk_start}  n_points={p.n_points}  brk_end={p.brk_end}')
    print(f'      utc_start  toc_anchor={p.utc_start_toc.isoformat()}  file_anchor={p.utc_start_file.isoformat()}')
    print(f'      utc_end    toc_anchor={p.utc_end_toc.isoformat()}  file_anchor={p.utc_end_file.isoformat()}')


def print_gap(prev: PacketInfo, curr: PacketInfo):
    missing_ticks = curr.brk_start - prev.brk_end
    missing_utc_toc = (curr.utc_start_toc - prev.utc_end_toc).total_seconds()
    missing_utc_file = (curr.utc_start_file - prev.utc_end_file).total_seconds()
    print(
        f'    -- gap: chunk {prev.chunk_id} (brk_end={prev.brk_end}) -> '
        f'chunk {curr.chunk_id} (brk_start={curr.brk_start}) --'
    )
    print(
        f'       missing_ticks={missing_ticks}  '
        f'missing_utc_toc_anchor={missing_utc_toc:.6f}s  '
        f'missing_utc_file_anchor={missing_utc_file:.6f}s'
    )


def fetch_toc_chunks(patient_id, admission_id, toc_id):
    from emu24.schema import NS5Chunks
    key = {'patient_id': patient_id, 'toc_id': toc_id, 'nsp_id': NSP_ID}
    if admission_id is not None:
        key['admission_id'] = admission_id
    rows = (NS5Chunks() & key).fetch('chunk_id', 'admission_id', 'ns5_file', as_dict=True)
    admission_ids = {int(r['admission_id']) for r in rows}
    if len(admission_ids) > 1:
        raise ValueError(
            f'toc_id={toc_id} exists under multiple admissions {sorted(admission_ids)} for this '
            f'patient - pass --admission to disambiguate'
        )
    return [
        ChunkFile(toc_id=toc_id, chunk_id=int(r['chunk_id']), filepath=r['ns5_file'], toc_base_file='')
        for r in rows
    ]


def main(patient_emu_id, admission_id, toc_id, chunk_start, chunk_end):
    from emu24.schema import Patient
    patient_id = (Patient() & {'emu_id': patient_emu_id}).fetch1('patient_id')

    all_toc_chunks = fetch_toc_chunks(patient_id, admission_id, toc_id)
    if not all_toc_chunks:
        print('No chunks found for this patient/TOC (check --admission if toc_id is ambiguous)')
        return

    toc_origin_utc, toc_ts_resolution, toc_first_tick = toc_origin(all_toc_chunks)
    first_chunk_id = min(c.chunk_id for c in all_toc_chunks)
    print(
        f'TOC {toc_id} shared origin: {toc_origin_utc.isoformat()} '
        f'(ts_resolution={toc_ts_resolution}, first_tick={toc_first_tick}, from chunk {first_chunk_id})'
    )
    print()

    last_toc_chunk_id = max(c.chunk_id for c in all_toc_chunks)

    chunks_in_range = sorted(
        (c for c in all_toc_chunks if chunk_start <= c.chunk_id <= chunk_end),
        key=lambda c: c.chunk_id,
    )
    if not chunks_in_range:
        print(f'No chunks found in range [{chunk_start}, {chunk_end}] for this TOC')
        return

    prev_packet = None
    for chunk in chunks_in_range:
        packets = read_chunk_packets(chunk, toc_origin_utc, toc_ts_resolution, toc_first_tick)
        for p in packets:
            if prev_packet is not None:
                print_gap(prev_packet, p)
            print_packet(p)
            prev_packet = p
        print()

    if last_toc_chunk_id < chunk_end:
        print(f'=== END OF TOC {toc_id} reached at chunk {last_toc_chunk_id} (requested chunks up to {chunk_end}) ===')


if __name__ == '__main__':
    from emu24.helper import make_login_parser, connect

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
        description='TEMPORARY diagnostic: walk raw packets across a chunk range for one TOC, comparing TOC-anchor vs file-anchor UTC.',
        add_help=False,
    )
    arg_parser.add_argument('--patient', type=str, required=True, help='EMU-ID')
    arg_parser.add_argument('--admission', type=int, default=None, help='Admission ID, only needed if toc_id is ambiguous for this patient')
    arg_parser.add_argument('--toc-id', type=int, required=True)
    arg_parser.add_argument('--chunk-start', type=int, required=True)
    arg_parser.add_argument('--chunk-end', type=int, required=True)
    args = arg_parser.parse_args()

    connect(args)
    main(args.patient, args.admission, args.toc_id, args.chunk_start, args.chunk_end)
