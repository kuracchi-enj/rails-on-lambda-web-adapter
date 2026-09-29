#!/usr/bin/env python3
"""bench/results/raw/*.jsonl（bench/cold_start.py の出力）を集計する。

出力:
    bench/results/coldstart.csv  — 全行（コールド/ウォーム問わず）をそのまま並べたもの
    bench/results/summary.md     — Markdown 表
        - コールドスタート時間（Init。SnapStart は Restore）: 行=app×package、列=メモリ
        - クライアント合計時間: 行=app×package、列=メモリ
        - チューニング: variant ごとに baseline（memory=1024, variant=baseline のラウンド、
          container/zip の4関数に絞る）との中央値の差

集計対象からコールドでない行（cold=false）は除外し、除外件数を標準出力に表示する。

使い方:
    python3 bench/aggregate.py                          # 既定パス
    python3 bench/aggregate.py --raw-dir DIR --out-dir DIR  # テスト用に入出力先を差し替え
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict

# bench/aggregate.py と同じディレクトリに cold_start.py がある前提で、そこから
# stats() / _percentile() を再利用する（percentile の定義をここで重複させない）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cold_start import stats  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RESULTS_DIR = os.path.join(REPO_ROOT, "bench", "results")
DEFAULT_RAW_DIR = os.path.join(DEFAULT_RESULTS_DIR, "raw")

MEMORY_LEVELS = [512, 1024, 1769, 3008]
APP_PACKAGE_ORDER = [
    ("web", "container"),
    ("api", "container"),
    ("web", "snapstart"),
    ("api", "snapstart"),
    ("web", "zip"),
    ("api", "zip"),
]
TUNING_APP_PACKAGE_ORDER = [
    ("web", "container"),
    ("api", "container"),
    ("web", "zip"),
    ("api", "zip"),
]
# 2026-09-28 の memory / tuning 計測は infra/cdk.json の context が asyncInit=true のまま
# だったため、baseline と asyncinit-on は同じ設定（ASYNC_INIT=true）になっている。
# ASYNC_INIT の比較は asyncinit-off（明示的に false）で行う。
TUNING_VARIANTS = ["bootsnap-off", "eagerload-off", "yjit-off", "asyncinit-on", "asyncinit-off"]


def cold_metric(rec: dict):
    """SnapStart は Restore Duration、それ以外は Init Duration を「コールド時間」として使う。"""
    if rec.get("package") == "snapstart":
        return rec.get("restore_duration_ms")
    return rec.get("init_duration_ms")


def _fmt_ms(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def cell_text(values: list) -> str:
    s = stats(values)
    if s["n"] == 0:
        return "n/a"
    return f"median={_fmt_ms(s['median'])} p90={_fmt_ms(s['p90'])} (n={s['n']})"


def load_records(raw_dir: str) -> list:
    records = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.jsonl"))):
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"WARNING: {path}:{lineno}: JSON パースエラー: {e}", file=sys.stderr)
    return records


def write_csv(records: list, csv_out: str) -> None:
    if not records:
        print(f"WARNING: 入力レコードが0件のため {csv_out} は書き込みません")
        return
    fieldnames: list = []
    seen: set = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    os.makedirs(os.path.dirname(csv_out) or ".", exist_ok=True)
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    print(f"{len(records)} 行 -> {csv_out}")


def build_memory_tables(cold_records: list) -> str:
    # variant=="baseline" だけを対象にする。tuning フェーズも memory=1024 で計測するため、
    # ここで絞らないと 1024MB 列に bootsnap-off 等のチューニング行が混入してしまう。
    baseline_records = [r for r in cold_records if r.get("variant") == "baseline"]

    groups: dict = defaultdict(list)
    memories_present: set = set()
    for rec in baseline_records:
        memory = rec.get("memory")
        if memory is None:
            continue
        groups[(rec.get("app"), rec.get("package"), memory)].append(rec)
        memories_present.add(memory)
    memories = [m for m in MEMORY_LEVELS if m in memories_present]
    memories += sorted(m for m in memories_present if m not in MEMORY_LEVELS)

    if not memories:
        return "## メモリ別コールドスタート時間\n\n(データなし)\n"

    header = "| app-package | " + " | ".join(f"{m}MB" for m in memories) + " |"
    sep = "|---|" + "---|" * len(memories)

    lines = ["## メモリ別コールドスタート時間\n"]
    lines.append("### Init（SnapStart は Restore）\n")
    lines.append(header)
    lines.append(sep)
    for app, package in APP_PACKAGE_ORDER:
        row = [f"{app}-{package}"]
        for m in memories:
            recs = groups.get((app, package, m), [])
            vals = [v for v in (cold_metric(r) for r in recs) if v is not None]
            row.append(cell_text(vals))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("### クライアント合計時間（コールドのみ）\n")
    lines.append(header)
    lines.append(sep)
    for app, package in APP_PACKAGE_ORDER:
        row = [f"{app}-{package}"]
        for m in memories:
            recs = groups.get((app, package, m), [])
            vals = [v for v in (r.get("client_total_ms") for r in recs) if v is not None]
            row.append(cell_text(vals))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def build_tuning_tables(cold_records: list) -> str:
    # baseline: memory=1024 の baseline ラウンド（mem1024-r1 / mem1024-r2 をまとめる）。
    # チューニングは container/zip の4関数だけが対象なので、baseline 側もそこに絞って比較する。
    baseline = [
        r
        for r in cold_records
        if r.get("memory") == 1024
        and r.get("variant") == "baseline"
        and r.get("package") in ("container", "zip")
    ]
    baseline_groups: dict = defaultdict(list)
    for r in baseline:
        baseline_groups[(r.get("app"), r.get("package"))].append(r)

    lines = ["## チューニング（メモリ1024固定、対象: container/zip の4関数）\n"]
    lines.append(
        "baseline は memory=1024 かつ variant=baseline の全ラウンド（mem1024-r1 / mem1024-r2）を"
        "まとめたもの。diff は variant の中央値 - baseline の中央値（ms、負の値は速くなったことを示す）。\n"
    )

    for variant in TUNING_VARIANTS:
        variant_groups: dict = defaultdict(list)
        for r in cold_records:
            if r.get("variant") == variant:
                variant_groups[(r.get("app"), r.get("package"))].append(r)

        lines.append(f"### variant={variant}\n")
        lines.append("| app-package | metric | baseline | variant | diff (median, ms) |")
        lines.append("|---|---|---|---|---|")
        for app, package in TUNING_APP_PACKAGE_ORDER:
            b_recs = baseline_groups.get((app, package), [])
            v_recs = variant_groups.get((app, package), [])

            for metric_name, extractor in (
                ("Init", cold_metric),
                ("client_total", lambda r: r.get("client_total_ms")),
            ):
                b_vals = [v for v in (extractor(r) for r in b_recs) if v is not None]
                v_vals = [v for v in (extractor(r) for r in v_recs) if v is not None]
                s_b = stats(b_vals)
                s_v = stats(v_vals)
                diff = (
                    s_v["median"] - s_b["median"]
                    if s_b["median"] is not None and s_v["median"] is not None
                    else None
                )
                lines.append(
                    f"| {app}-{package} | {metric_name} | {cell_text(b_vals)} | "
                    f"{cell_text(v_vals)} | {_fmt_ms(diff)} |"
                )
        lines.append("")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw-dir", default=DEFAULT_RAW_DIR, help="読み込む *.jsonl のディレクトリ")
    p.add_argument("--out-dir", default=DEFAULT_RESULTS_DIR, help="coldstart.csv / summary.md の出力先")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    records = load_records(args.raw_dir)
    csv_out = os.path.join(args.out_dir, "coldstart.csv")
    summary_out = os.path.join(args.out_dir, "summary.md")

    write_csv(records, csv_out)

    cold_records = [r for r in records if r.get("cold")]
    excluded = len(records) - len(cold_records)
    print(f"total={len(records)} cold={len(cold_records)} excluded(non-cold)={excluded}")

    parts = [
        "# コールドスタート計測 集計結果\n",
        f"全 {len(records)} 行のうち、コールドでない {excluded} 行を除外して以下を集計。\n",
        build_memory_tables(cold_records),
        build_tuning_tables(cold_records),
    ]

    os.makedirs(args.out_dir, exist_ok=True)
    with open(summary_out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"summary -> {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
