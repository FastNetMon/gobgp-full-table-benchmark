#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate a completed comparison and generate tables and static PNG/SVG graphs.

Also reads the original 2026-10-16 comparison and bounded-stream run bundles.
Never modifies the input measurements. Plotting requires matplotlib.
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics

from compare import MODES, check_counts

LABELS = {"structured": "Structured", "mixed": "Structured +\nbinary attributes", "binary": "Fully binary"}
METRICS = ("total_ms", "ttfb_ms", "in_recv_ms", "in_proc_ms", "server_cpu_seconds")


def read(path):
    return json.loads(path.read_text())


def load_run(root):
    rows = read(root / "results.json")
    meta = read(root / "run-metadata.json")
    memory = read(root / "memory.json") if (root / "memory.json").exists() else []
    if "config" in meta:
        if meta.get("status") != "complete":
            raise ValueError("Run is incomplete or failed")
        cfg = meta["config"]
        before, after = (meta["correctness"][k] for k in ("before", "after"))
    else:
        # Compatibility with the two original, already completed run formats.
        before = meta.get("validation_before", meta.get("binary_data_validation_before"))
        after = meta.get("validation_after", meta.get("binary_data_validation"))
        if not before or not after:
            raise ValueError("Missing before/after correctness validation")
        revisions = meta.get("versions", {"before": meta.get("base_commit"), "after": meta.get("fix_commit")})
        cfg = {"variants": [{"name": name, "label": name, "revision": revisions.get(name, "unknown")}
                            for name in before],
               "expected": next(iter(before.values())), "warmup_rounds": meta.get("warmup_rounds", 1),
               "measured_rounds": meta.get("measured_rounds", 5), "memory": bool(memory)}
    names = [v["name"] for v in cfg["variants"]]
    cells = {(name, mode) for name in names for mode in MODES}
    if set(before) != set(names) or set(after) != set(names) or before != after:
        raise ValueError("Missing/changed before/after route data")
    reference = next(iter(before.values()))
    check_counts(reference, cfg["expected"])
    if any(value != reference for value in before.values()):
        raise ValueError("Variants have different route data")
    if not isinstance(reference.get("sha256"), str) or len(reference["sha256"]) != 64:
        raise ValueError("Missing canonical route-data hash")
    for row in rows:
        if (row["daemon"], row["mode"]) not in cells:
            raise ValueError("Unexpected variant/mode")
        check_counts({"destinations": row["dests"], "paths": row["paths"]}, cfg["expected"])
        for key in METRICS:
            if not isinstance(row[key], (float, int)) or not math.isfinite(row[key]) or row[key] < 0:
                raise ValueError("Invalid measurement: " + key)
        if row["mode"] != "structured" and row["attr_bytes"] != reference["attribute_bytes"]:
            raise ValueError("Attribute byte count mismatch")
        if row["mode"] == "binary" and row["nlri_bytes"] != reference["nlri_bytes"]:
            raise ValueError("NLRI byte count mismatch")
    summary = []
    for variant in cfg["variants"]:
        for mode in MODES:
            selected = [r for r in rows if r["daemon"] == variant["name"] and r["mode"] == mode]
            rounds = cfg["warmup_rounds"] + cfg["measured_rounds"]
            if len(selected) != rounds or {r["round"] for r in selected} != set(range(rounds)):
                raise ValueError("Missing/duplicate benchmark round")
            if any(r["warmup"] != (r["round"] < cfg["warmup_rounds"]) for r in selected):
                raise ValueError("Incorrect warm-up labels")
            selected = [r for r in selected if not r["warmup"]]
            entry = {"daemon": variant["name"], "mode": mode, "iterations": len(selected)}
            for key in METRICS:
                values = [r[key] for r in selected]
                entry.update({key: statistics.median(values), key + "_min": min(values), key + "_max": max(values)})
            summary.append(entry)
    seen = set()
    for item in memory:
        cell = (item["daemon"], item["mode"])
        if cell not in cells or cell in seen or item["validation"] != reference:
            raise ValueError("Missing/duplicate/invalid fresh-process memory case")
        seen.add(cell)
        check_counts({"destinations": item["request"]["dests"], "paths": item["request"]["paths"]}, cfg["expected"])
        if item["peak_rss_kib"] < item["baseline"]["rss_kib"]:
            raise ValueError("Peak RSS below baseline")
    if cfg["memory"] and seen != cells:
        raise ValueError("Missing fresh-process memory measurements")
    return cfg, meta, rows, summary, memory, reference


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render(root, out):
    cfg, meta, rows, summary, memory, reference = load_run(root)
    out.mkdir(parents=True, exist_ok=True)
    names = [v["name"] for v in cfg["variants"]]
    labels = {v["name"]: v.get("label", v["name"]) for v in cfg["variants"]}
    by = {(r["daemon"], r["mode"]): r for r in summary}
    mem = {(r["daemon"], r["mode"]): r for r in memory}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_csv(out / "summary.csv", summary)
    memory_rows = []
    for item in sorted(memory, key=lambda item: (names.index(item["daemon"]), list(MODES).index(item["mode"]))):
        idle = item.get("idle", item.get("idle_after_15s"))
        memory_rows.append({"daemon": item["daemon"], "mode": item["mode"],
                            "baseline_rss_gib": item["baseline"]["rss_kib"] / 1048576,
                            "loaded_hwm_gib": item["baseline"]["peak_rss_kib"] / 1048576,
                            "peak_rss_gib": item["peak_rss_kib"] / 1048576,
                            "sampled_peak_rss_gib": item["sampled_peak_rss_kib"] / 1048576,
                            "immediate_rss_gib": item["immediate"]["rss_kib"] / 1048576,
                            "idle_rss_gib": idle["rss_kib"] / 1048576})
    write_csv(out / "memory.csv", memory_rows)
    changes = []
    for index, baseline_name in enumerate(names):
        for name in names[index+1:]:
            for mode in MODES:
                row = {"baseline": baseline_name, "comparison": name, "mode": mode}
                for key in METRICS:
                    baseline = by[baseline_name, mode][key]
                    row[key + "_change_percent"] = 100 * (by[name, mode][key] / baseline - 1) if baseline else None
                if mem:
                    row["peak_rss_change_percent"] = 100 * (mem[name, mode]["peak_rss_kib"] / mem[baseline_name, mode]["peak_rss_kib"] - 1)
                changes.append(row)
    write_csv(out / "comparison.csv", changes)
    (out / "comparison.json").write_text(json.dumps(changes, indent=2) + "\n")
    links = os.path.relpath(root, out)
    text = ["# Full-table API retrieval comparison", "",
            f"IPv4 Adj-RIB-In: **{reference['destinations']:,} destinations, {reference['paths']:,} paths**.", "",
            "This measures API retrieval, not convergence, route propagation, update throughput or policy evaluation.", "",
            f"Timing: {cfg['measured_rounds']} measured requests per cell after {cfg['warmup_rounds']} warm-up round(s). "
            "Requests are sequential and shuffled across variants/modes. Values below are medians.", "",
            "| Version | Mode | Total ms | First response ms | Read ms | Processing ms | Server CPU s |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in summary:
        text.append(f"| {labels[row['daemon']]} | {LABELS[row['mode']].replace(chr(10), ' ')} | " +
                    " | ".join(f"{row[k]:.1f}" if k != "server_cpu_seconds" else f"{row[k]:.3f}" for k in METRICS) + " |")
    text += ["", "Read time includes server work, transport and client protobuf parsing. Processing time measures "
             "the client loop between reads. `decode=true` scans binary buffers; it does not fully decode BGP attributes. "
             "Structured output has no binary buffers to scan. CPU deltas cover the host process around the client command.", ""]
    if memory:
        text += ["## Fresh-process memory", "",
                 "One new daemon and one request per cell, without an earlier ListPath call or warm-up. "
                 "These are single observations, not medians. VmHWM is the process lifetime high-water mark, "
                 "so it includes route loading. It is captured before the later correctness pass. "
                 "Baseline, sampled peak, immediate and idle RSS are also retained in memory.csv.", "",
                 "| Version | Mode | Loaded RSS GiB | Peak RSS GiB | Idle RSS GiB |", "| --- | --- | ---: | ---: | ---: |"]
        for row in memory_rows:
            text.append(f"| {labels[row['daemon']]} | {LABELS[row['mode']].replace(chr(10), ' ')} | "
                        f"{row['baseline_rss_gib']:.2f} | {row['peak_rss_gib']:.2f} | {row['idle_rss_gib']:.2f} |")
    else:
        text += ["Fresh-process memory was not measured. No per-mode memory claim can be made from this run."]
    steady_before = meta.get("steady_memory_before", meta.get("memory_before", {}))
    steady_after = meta.get("steady_memory_after", meta.get("memory_after", {}))
    text += ["", "## Combined workload memory", "",
             "These lifetime peaks include route loading, validation, warm-ups and all API modes. "
             "Do not attribute them to a binary-only request.", "",
             "| Version | Loaded RSS GiB | After workload RSS GiB | Lifetime peak GiB |", "| --- | ---: | ---: | ---: |"]
    for name in names:
        text.append(f"| {labels[name]} | {steady_before[name]['rss_kib']/1048576:.2f} | "
                    f"{steady_after[name]['rss_kib']/1048576:.2f} | {steady_after[name]['peak_rss_kib']/1048576:.2f} |")
    text += ["", "## Correctness and provenance", "",
             f"All {len(rows)} timed/warm-up requests and {len(memory)} memory cases returned the expected counts. "
             "Separate binary validation passes matched before/after and across versions. "
             "The hash covers binary NLRI and sorted top-level binary attributes, retaining duplicate paths. "
             "Timestamps, peer metadata and structured response equivalence are outside this hash.", "",
             f"Route-data SHA-256: `{reference['sha256']}`.", "",
             f"NLRI bytes: {reference['nlri_bytes']:,}; attribute bytes: {reference['attribute_bytes']:,}.", "",
             "| Version | Declared source revision | Binary version output |", "| --- | --- | --- |"]
    for variant in cfg["variants"]:
        version = meta.get("version_output", {}).get(variant["name"])
        if version is None:
            version = meta.get("binaries", {}).get(variant["name"], {}).get("version", "See run metadata")
        text.append(f"| {labels[variant['name']]} | `{variant['revision']}` | {version} |")
    text += ["", f"Exact input/configuration, runtime image ID, binary hashes and host details: [run metadata]({links}/run-metadata.json). "
             "Source revisions/build details are caller-supplied; file hashes identify the measured binaries.", "",
             f"[Raw requests]({links}/raw/) · [Raw timing JSON]({links}/results.json) · "
             "[Summary CSV](summary.csv) · [Pairwise percentage changes](comparison.csv)", "",
             "![Overview](overview.png)", "", "![Timing and CPU](timing-cpu.png)"]
    if meta.get("cleanup_errors"):
        text += ["", "Cleanup reported errors; inspect run-metadata.json for remaining resources."]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    bg, ink, muted, grid = "#F5F3ED", "#172D3C", "#637583", "#D9DED9"
    palette = ["#CC643A", "#237D9F", "#677B42", "#8A639A", "#A87820", "#356C63"]
    colors = {name: palette[i % len(palette)] for i, name in enumerate(names)}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "text.color": ink,
                         "axes.labelcolor": muted, "xtick.color": ink, "ytick.color": ink,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.spines.left": False, "axes.spines.bottom": False,
                         "axes.facecolor": bg, "figure.facecolor": bg, "svg.fonttype": "none"})

    def canvas(title, subtitle, count=2):
        fig, axes = plt.subplots(1, count, figsize=(max(14, len(names)*3.5), 7), squeeze=False)
        fig.subplots_adjust(left=.07, right=.97, bottom=.23, top=.68, wspace=.30)
        fig.text(.07, .92, title, fontsize=25, weight="bold")
        fig.text(.07, .86, subtitle, fontsize=12, color=muted)
        fig.legend(handles=[Line2D([0], [0], color=colors[n], lw=7, label=labels[n]) for n in names],
                   loc="upper left", bbox_to_anchor=(.065, .81), frameon=False, ncol=min(4, len(names)))
        return fig, list(axes[0])

    def bars(ax, key, title, unit, scale=1):
        width = .8 / len(names)
        maximum = 0
        for index, name in enumerate(names):
            positions = [i - .4 + width*(index+.5) for i in range(3)]
            entries = [by[name, m] for m in MODES]
            values = [r[key]/scale for r in entries]
            lower = [values[i]-r[key+"_min"]/scale for i, r in enumerate(entries)]
            upper = [r[key+"_max"]/scale-values[i] for i, r in enumerate(entries)]
            ax.bar(positions, values, width*.88, color=colors[name], yerr=[lower, upper],
                   capsize=3, error_kw={"ecolor": ink, "elinewidth": .8})
            maximum = max(maximum, max(r[key+"_max"]/scale for r in entries))
            for x, value, high in zip(positions, values, upper):
                ax.annotate(f"{value:.2f}", (x, value+high), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=10)
        ax.set_ylim(0, max(maximum*1.27, .01))
        ax.set_xticks(range(3), list(LABELS.values()))
        style(ax, title, unit)

    def style(ax, title, unit):
        ax.set_title(title, loc="left", pad=18, fontsize=13, weight="bold")
        ax.set_ylabel(unit)
        ax.set_axisbelow(True)
        ax.grid(axis="y", color=grid, linewidth=.7)
        ax.tick_params(length=0, pad=9)

    def finish(fig, name):
        for suffix in ("png", "svg"):
            fig.savefig(out / (name + "." + suffix), dpi=180)
        plt.close(fig)

    subtitle = f"IPv4 Adj-RIB-In · {reference['paths']:,} paths · matching binary route-data hashes"
    fig, axes = canvas("Full-table API retrieval", subtitle)
    bars(axes[0], "total_ms", "Median full-stream latency", "Seconds", 1000)
    if memory:
        width = .8 / len(names)
        maximum = max(r["peak_rss_kib"] / 1048576 for r in memory)
        for index, name in enumerate(names):
            positions = [i-.4+width*(index+.5) for i in range(3)]
            values = [mem[name, m]["peak_rss_kib"]/1048576 for m in MODES]
            axes[1].bar(positions, values, width*.88, color=colors[name])
            for x, value in zip(positions, values):
                axes[1].annotate(f"{value:.2f}", (x, value), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=10)
        axes[1].set_xticks(range(3), list(LABELS.values()))
        axes[1].set_ylim(0, maximum*1.27)
        style(axes[1], "Fresh-process peak RSS · one observation per cell", "GiB RSS")
    else:
        values = [steady_after[n]["peak_rss_kib"]/1048576 for n in names]
        axes[1].bar(range(len(names)), values, color=[colors[n] for n in names])
        for x, value in enumerate(values):
            axes[1].annotate(f"{value:.2f}", (x, value), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=10)
        axes[1].set_ylim(0, max(values)*1.25)
        axes[1].set_xticks(range(len(names)), [labels[n] for n in names])
        style(axes[1], "Combined workload lifetime peak · all modes", "GiB RSS")
    fig.text(.07, .10, f"Latency: median of {cfg['measured_rounds']} measured runs; whiskers show min–max. Memory peaks include route loading.", fontsize=11, color=muted)
    fig.text(.07, .055, "API retrieval only. These measurements do not establish convergence or update-processing performance.", fontsize=11, color=muted)
    finish(fig, "overview")
    # Two separate two-panel figures keep labels readable in exported images.
    for filename, panels in [("timing-cpu", [("ttfb_ms", "Time to first response", "Seconds", 1000),
                                            ("server_cpu_seconds", "Server CPU around each request", "CPU seconds", 1)]),
                             ("client-timing", [("in_recv_ms", "Time inside Read()", "Seconds", 1000),
                                                ("in_proc_ms", "Client processing between reads", "Milliseconds", 1)])]:
        fig, axes = canvas("Full-table retrieval / timing breakdown", subtitle)
        for ax, (key, title, unit, scale) in zip(axes, panels):
            bars(ax, key, title, unit, scale)
        fig.text(.07, .10, "Medians with min–max whiskers. Read() includes transport and protobuf parsing; decode=true scans binary buffers.", fontsize=11, color=muted)
        finish(fig, filename)
    text += ["", "![Client timing](client-timing.png)"]
    if memory:
        fig, axes = canvas("Full-table retrieval / memory over time", subtitle, 3)
        for ax, mode in zip(axes, MODES):
            for name in names:
                item = mem[name, mode]
                samples = read(root / "raw" / f"cold-{name}-{mode}-rss.json")
                ax.plot([s["time"]-item["start"] for s in samples], [s["rss_kib"]/1048576 for s in samples],
                        color=colors[name], linewidth=1.4)
                ax.axvline(item["end"]-item["start"], color=colors[name], linestyle=":", linewidth=.8)
            style(ax, LABELS[mode].replace("\n", " "), "GiB RSS")
            ax.set_xlabel("Seconds since client command started")
            ax.set_ylim(bottom=0)
        # Shared scale makes differences across response modes comparable.
        maximum = max(ax.get_ylim()[1] for ax in axes)
        for ax in axes:
            ax.set_ylim(0, maximum)
        fig.text(.07, .10, "Each line uses a fresh daemon. Dotted lines mark client command completion; validation runs after this window.", fontsize=11, color=muted)
        finish(fig, "memory-timeline")
        text += ["", "![Memory timeline](memory-timeline.png)"]
    (out / "REPORT.md").write_text("\n".join(text) + "\n")
    print(out / "REPORT.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, help="Default: RUN/report")
    args = parser.parse_args()
    render(args.run.resolve(), (args.output or args.run / "report").resolve())


if __name__ == "__main__":
    main()
