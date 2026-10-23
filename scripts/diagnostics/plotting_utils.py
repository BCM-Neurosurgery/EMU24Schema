"""
Generic plotting helpers shared across this project's coverage/timeline notebooks
(coverage_visualizations.ipynb, coverage_timeline_base.ipynb, collection_delay.ipynb). Nothing
here is specific to any one CSV's columns - notebook cells pass in the data and column names.
"""

import math

import matplotlib.pyplot as plt
import numpy as np


def log_bins(values, n=30):
    """Log-spaced bin edges spanning the positive values in `values`."""
    values = values[values > 0]
    if len(values) == 0 or values.min() == values.max():
        return n
    return np.logspace(np.log10(values.min()), np.log10(values.max()), n)


def hist_ax(ax, values, title, xlabel='Duration (hours)', use_log=False, bins=30):
    """
    Histogram on log-spaced bins: durations/delays here can span microseconds to multi-day, so a
    linear-scale histogram would just show one tall bar near zero.

    `bins` may be a count (edges computed from `values` alone) or a precomputed array of edges -
    small_multiples passes the latter so every subplot in a grid shares the same x-axis.
    """
    values = values[values > 0]
    if len(values) == 0:
        ax.set_title(f'{title} (no data)')
        return

    if use_log and isinstance(bins, int):
        bins = log_bins(values, n=bins)

    ax.hist(values, bins=bins)
    if use_log:
        ax.set_xscale('log')
    ax.set_xlabel(xlabel)
    ax.set_ylabel('Count')
    ax.set_title(title)


def small_multiples(df, value_col, group_col, suptitle, ncols=4, bins=20, use_log=True, xlabel='Duration (hours)'):
    """
    One log-scale histogram per group (e.g. per patient), laid out in a grid.

    Bin edges are computed once from the whole column and every panel is forced to the same
    x-limits afterward - relying on matplotlib's sharex alone left panels with no data (which
    never call set_xscale/autoscale) or a narrower data range visually out of sync with the rest.
    """
    groups = sorted(df[group_col].unique())
    nrows = math.ceil(len(groups) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4 * ncols, 3 * nrows), squeeze=False)

    if use_log:
        shared_bins = log_bins(df[value_col], n=bins)
    else:
        shared_bins = np.linspace(df[value_col].min(), df[value_col].max(), num=bins)

    for ax, group in zip(axes.flat, groups):
        hist_ax(ax, df.loc[df[group_col] == group, value_col], str(group), xlabel=xlabel, use_log=use_log, bins=shared_bins)
    for ax in axes.flat[len(groups):]:
        ax.axis('off')

    if not isinstance(shared_bins, int):
        xlim = (shared_bins[0], shared_bins[-1])
        for ax in axes.flat[:len(groups)]:
            if use_log:
                ax.set_xscale('log')
            ax.set_xlim(xlim)

    fig.suptitle(suptitle)
    plt.tight_layout()
    plt.show()


def stacked_bars(ax, bin_centers, width, group_values):
    """
    Draw one bar layer per group, stacked, using matplotlib's own default color cycle -
    no color is passed to bar(), so each successive group's layer gets the next color in
    the axes' cycle, same as any other unstyled series.
    """
    groups_in_order = sorted(group_values.keys())
    bottom = np.zeros(len(bin_centers))
    for group in groups_in_order:
        values = group_values[group]
        ax.bar(bin_centers, values, width=width, bottom=bottom, label=str(group))
        bottom += values
    return groups_in_order


def raw_values_by_group(subset, value_col, group_col, groups_in_order):
    """List of value arrays, one per group in groups_in_order (empty if that group has
    no rows in subset) - the shape ax.hist(..., stacked=True) expects, per the matplotlib
    stacked-histogram example, so it handles both the stacking and the default color
    cycle."""
    return [subset.loc[subset[group_col] == group, value_col].to_numpy() for group in groups_in_order]


def style_hist_axes(ax):
    """Strip the top/right spines and bump up the remaining spines/ticks/labels - the "AV Sync
    style" minimal-chrome look (see "AV Sync style example.png")."""
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_linewidth(1.5)
    ax.spines['bottom'].set_linewidth(1.5)
    ax.tick_params(width=1.5, labelsize=12)
    ax.xaxis.label.set_size(14)
    ax.yaxis.label.set_size(14)


def stepfilled_hist(ax, values, color, edgecolor=None, bins=50, alpha=0.4, linewidth=1.5, label=None):
    """One semi-transparent filled-step histogram layer (AV Sync style) - stack several
    (different colors) on the same ax and their overlap blends, same as the AV Sync example's
    two distributions."""
    edgecolor = color if edgecolor is None else edgecolor
    ax.hist(
        values, bins=bins, histtype='stepfilled',
        color=color, edgecolor=edgecolor, alpha=alpha, linewidth=linewidth, label=label,
    )


def stepfilled_bars(ax, heights, bin_edges, color, edgecolor=None, alpha=0.55, linewidth=1.5, label=None):
    """Pre-binned equivalent of stepfilled_hist - fills a step plot from precomputed per-bin
    heights (e.g. summed duration), same AV Sync look, for data that isn't a raw-sample
    histogram."""
    edgecolor = color if edgecolor is None else edgecolor
    ax.stairs(heights, bin_edges, fill=True, color=color, edgecolor=edgecolor, alpha=alpha, linewidth=linewidth, label=label)
