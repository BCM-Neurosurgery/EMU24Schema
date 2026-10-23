"""
Standalone script: draw one patient's whole recording time course - neural availability as a filled
blue band, video as a filled orange band above it, gaps marked in red/dark red - from a neural
gap-summary + visit-summary CSV pair and a video gap-summary + visit-summary CSV pair (neural as
produced by data_coverage.py/discontinuity_analysis.py's --gaps/--visits, e.g. gaps.csv +
visit_summary_corrected.csv; video from whatever equivalent tool produced its own pair with the same
column names). This is the single-patient version of coverage_timeline_base.ipynb's
plot_patient_timeline cell, pulled out as its own file so it has no dependency on this repo
(plotting_utils.py, etc.) or on a live database - only the CSVs and widely-available packages
(pandas, numpy, matplotlib) - so it's easy to hand to someone else as one file alongside the
(already-shared) data.

Both sources are drawn on one shared time origin - same convention as the notebook: the "visit
start" both are measured from is the EARLIER of the two sources' own recording_start_utc for this
patient, not either source's own start, so a system that started recording later doesn't look like
it has a head start.

Usage:
    python scripts/plot_patient_timeline.py PATIENT_ID [--out patient_timeline.png]
"""

import argparse

import matplotlib.pyplot as plt
import pandas as pd

NEURAL_VISIT_SUMMARY_CSV = r"Z:\EMU-18112\24-7-Paper\Data Availability\neural-data\visit_summary_corrected.csv"
NEURAL_GAP_SUMMARY_CSV = r"Z:\EMU-18112\24-7-Paper\Data Availability\neural-data\real_data_gaps.csv"

VIDEO_VISIT_SUMMARY_CSV = r"Z:\EMU-18112\24-7-Paper\Data Availability\video-data\video_visit_summary.csv"
VIDEO_GAP_SUMMARY_CSV = r"Z:\EMU-18112\24-7-Paper\Data Availability\video-data\video_gaps.csv"

VIDEO_TIME_OFFSET = -12

SHORT_GAP_THRESHOLD_MINUTES = 10
NEURAL_COLOR = 'tab:blue'
VIDEO_COLOR = 'tab:orange'


def load_coverage(gap_csv, visit_csv, patient_id, emu_col='emu_id', gap_start_col='gap_start_utc', t_offset=0,
                   gap_end_col='gap_end_utc', visit_start_col='recording_start_utc', visit_end_col='recording_end_utc'):
    """(gaps, visits) for one patient only, both UTC columns parsed as tz-aware datetimes.
    format='ISO8601' parses whichever valid ISO8601 variant each row actually is (some have
    fractional seconds and/or an explicit UTC offset, others don't), and utc=True still forces
    tz-aware UTC regardless."""
    gaps = pd.read_csv(gap_csv)
    gaps = gaps[gaps[emu_col] == patient_id].copy()
    t_offset = pd.Timedelta(t_offset, unit='h')
    for col in (gap_start_col, gap_end_col):
        gaps[col] = pd.to_datetime(gaps[col], format='ISO8601', utc=True) + t_offset

    visits = pd.read_csv(visit_csv)
    visits = visits[visits[emu_col] == patient_id].copy()
    for col in (visit_start_col, visit_end_col):
        visits[col] = pd.to_datetime(visits[col], format='ISO8601', utc=True) + t_offset

    return gaps, visits


def good_intervals_for_admission(recording_start, recording_end, admission_gaps, gap_start_col, gap_end_col):
    """Complement of admission_gaps (sorted by start) within [recording_start, recording_end]."""
    admission_gaps = admission_gaps.sort_values(gap_start_col)
    intervals = []
    cursor = recording_start
    for _, gap in admission_gaps.iterrows():
        if gap[gap_start_col] > cursor:
            intervals.append((cursor, gap[gap_start_col]))
        cursor = max(cursor, gap[gap_end_col])
    if recording_end > cursor:
        intervals.append((cursor, recording_end))
    return intervals


def build_good_intervals(gaps, visits, patient_col='emu_id', visit_start_col='recording_start_utc',
                          visit_end_col='recording_end_utc', gap_start_col='gap_start_utc', gap_end_col='gap_end_utc'):
    """One row per continuous good-data interval, across every admission in visits. Output columns
    are always named patient_col plus the fixed 'good_start_utc'/'good_end_utc', regardless of the
    input gap/visit column names."""
    rows = []
    for _, visit in visits.iterrows():
        admission_gaps = gaps[gaps[patient_col] == visit[patient_col]]
        for good_start, good_end in good_intervals_for_admission(
            visit[visit_start_col], visit[visit_end_col], admission_gaps, gap_start_col, gap_end_col
        ):
            rows.append({patient_col: visit[patient_col], 'good_start_utc': good_start, 'good_end_utc': good_end})
    return pd.DataFrame(rows, columns=[patient_col, 'good_start_utc', 'good_end_utc'])


def plot_patient_timeline(
    ax, patient_id, good_df, gap_df, visit_start,
    id_col='emu_id', good_start_col='good_start_utc', good_end_col='good_end_utc',
    gap_start_col='gap_start_utc', gap_end_col='gap_end_utc',
    good_color='tab:blue', gap_color='red', y_offset=0.0, label=None,
):
    """
    Draw one patient's whole recording time course onto `ax`, x-axis as hours elapsed since
    visit_start. Available (good) data is a filled band in good_color, gaps are gap_color markers
    above it. A gap shorter than short_gap_threshold_minutes is a single point at its midpoint; a
    longer gap also gets a thin line spanning its full duration. y_offset shifts this source's band
    up (in the same [0, 1]-per-source vertical scale) so both sources can be overlaid on one ax
    without occluding each other.
    """
    patient_good = good_df[good_df[id_col] == patient_id]
    patient_gaps = gap_df[gap_df[id_col] == patient_id]

    def elapsed_hours(timestamp):
        return (timestamp - visit_start).total_seconds() / 3600

    bars = [
        (elapsed_hours(row[good_start_col]), elapsed_hours(row[good_end_col]) - elapsed_hours(row[good_start_col]))
        for _, row in patient_good.iterrows()
    ]
    ax.broken_barh(bars, (y_offset, 1), facecolors=good_color, label=label)

    threshold_hours = SHORT_GAP_THRESHOLD_MINUTES / 60
    marker_y = y_offset + 1.2
    for _, gap in patient_gaps.iterrows():
        start, end = elapsed_hours(gap[gap_start_col]), elapsed_hours(gap[gap_end_col])
        if (end - start) >= threshold_hours:
            ax.plot([start, end], [marker_y, marker_y], color=gap_color, linewidth=1)
        ax.plot(start, marker_y, marker='x', color=gap_color, linestyle='None')

    ax.set_yticks([])
    ax.set_ylabel(str(patient_id), rotation=0, ha='right', va='center')


def main(patient_id, out_path=None):
    # load the visit sumamry and gap data for neural
    neural_gaps, neural_visits = load_coverage(NEURAL_GAP_SUMMARY_CSV, NEURAL_VISIT_SUMMARY_CSV, patient_id)
    if neural_visits.empty:
        raise ValueError(f'No admission found for patient {patient_id!r} in {NEURAL_VISIT_SUMMARY_CSV}')
    neural_good = build_good_intervals(neural_gaps, neural_visits)

    # load the visit summary and gap data for video
    video_gaps, video_visits = load_coverage(
        VIDEO_GAP_SUMMARY_CSV, VIDEO_VISIT_SUMMARY_CSV, patient_id, t_offset=VIDEO_TIME_OFFSET)
    if video_visits.empty:
        raise ValueError(f'No admission found for patient {patient_id!r} in {VIDEO_VISIT_SUMMARY_CSV}')
    video_good = build_good_intervals(video_gaps, video_visits)

    # Set up unified start/end for both data types
    visit_start = min(neural_visits['recording_start_utc'].min(), video_visits['recording_start_utc'].min())
    max_elapsed = max(
        (neural_visits['recording_end_utc'].max() - visit_start).total_seconds() / 3600,
        (video_visits['recording_end_utc'].max() - visit_start).total_seconds() / 3600,
    )

    print(f'Visit start (t=0): {visit_start}')

    fig, ax = plt.subplots(figsize=(14, 2.2))
    ax.set_ylim(-0.3, 3.7)
    plot_patient_timeline(
        ax, patient_id, neural_good, neural_gaps, visit_start,
        good_color=NEURAL_COLOR, gap_color='red', y_offset=0.0, label='neural'
    )
    plot_patient_timeline(
        ax, patient_id, video_good, video_gaps, visit_start,
        good_color=VIDEO_COLOR, gap_color='darkred', y_offset=2.0, label='video'
    )
    ax.legend(loc='upper right', fontsize=8)

    ax.set_xlim(0, max_elapsed)
    ax.set_xlabel('Time since visit start (hours)')
    ax.set_title(f'Recording timeline - {patient_id}')
    plt.tight_layout()

    if out_path:
        plt.savefig(out_path, dpi=300)
        print(f'Wrote {out_path}')
    else:
        plt.show()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Plot one patient's neural + video recording timeline (good-data bands + gap markers)."
    )
    parser.add_argument('patient_id', help='emu_id of the patient to plot')
    parser.add_argument('--out', default=None, help='Save the figure here instead of showing it interactively')
    args = parser.parse_args()

    main(args.patient_id, args.out)
