#!/usr/bin/env python3
"""Aurora Serverless v2 の auto-pause からの DB 復帰時間を計測する: 1 シナリオ分（cold/warm のいずれか）
を1 対象キーに対して実行する。

背景: このリポジトリの DB は Aurora PostgreSQL Serverless v2（min 0 ACU、auto-pause 300 秒、
DB 側 idle_session_timeout=60 秒）。Lambda 実行環境がウォームでも DB が pause していれば、
リクエストは DB の再起動待ちで遅くなる。これを cold（実行環境もコールド）と warm（実行環境は
ウォームのまま DB だけ pause）の両シナリオで切り分けて計測する。

pause の検知は CloudWatch の `AWS/RDS` `ServerlessDatabaseCapacity`（Maximum, period=60）を
ポーリングして行う。pause 中は 0.0、稼働中は 0.5 以上になることを親が実機で確認済み。
メトリクスは数分遅れて出てくる前提で、「最後に DB を使うリクエストを送った時刻」より **後**の
タイムスタンプを持つデータポイントでしか 0.0 を検知しない（pause 前の古い 0.0 データポイントを
誤検知しないようにするため）。

再利用: HTTP バッチ送信・CloudWatch Logs との突き合わせ・JSONL 出力・urls.env 読み込み・
キー→関数名変換は bench/cold_start.py から import する（bench/aggregate.py が
`sys.path.insert` + `from cold_start import stats` している方式に倣う）。

使い方:
    python3 -B bench/db_paused.py --urls bench/results/raw/urls.env \\
        --target API_CONTAINER --scenario cold --concurrency 3 \\
        --label dbpaused-cold --memory 1024 \\
        --out bench/results/raw/db-paused-20260928.jsonl

    python3 -B bench/db_paused.py --self-test    # オフラインでメトリクスのパース・pause検知を検証
    python3 -B bench/db_paused.py --dry-run ...  # 実行予定の HTTP / aws コマンドを表示するだけ

このスクリプトは自分では AWS を一切呼び出さない --self-test / --dry-run 以外の経路でのみ
`aws cloudwatch get-metric-statistics`（cold_start.py 経由で `aws logs filter-log-events` も）を
subprocess で呼ぶ。本エージェント（実装担当）は AWS アクセス禁止のため、--self-test と
--dry-run でのみ動作確認済み。実機での挙動（実際に auto-pause が検知できるか、pause 待ちに
かかる実時間、warm シナリオで実行環境が本当に生き続けるか等）は未検証。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

# bench/db_paused.py と同じディレクトリに cold_start.py がある前提で、そこから
# HTTP バッチ送信・ログ突き合わせ・JSONL 出力などを再利用する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cold_start import (  # noqa: E402
    RequestResult,
    append_jsonl,
    app_and_package_for_key,
    build_record,
    build_stream_index,
    function_name_for_key,
    load_urls_env,
    poll_logs_until_matched,
    run_batch,
    stats,
)


# ── 時刻ヘルパー ──────────────────────────────────────────────────────────


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso8601(s: str) -> datetime:
    """AWS CLI が返す "2026-09-28T09:10:00+00:00" 形式（まれに "Z" 終端）をパースする。"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── CloudWatch メトリクス ─────────────────────────────────────────────────


def cloudwatch_get_metric_statistics(cluster_id: str, start_iso: str, end_iso: str, region: str) -> dict:
    cmd = [
        "aws", "cloudwatch", "get-metric-statistics",
        "--namespace", "AWS/RDS",
        "--metric-name", "ServerlessDatabaseCapacity",
        "--dimensions", f"Name=DBClusterIdentifier,Value={cluster_id}",
        "--start-time", start_iso,
        "--end-time", end_iso,
        "--period", "60",
        "--statistics", "Maximum",
        "--region", region,
        "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def parse_datapoints(data: dict) -> list:
    """get-metric-statistics の生 JSON を (timestamp: datetime, maximum: float) のリストへ変換し、
    タイムスタンプ昇順にソートして返す。AWS CLI は Datapoints の順序を保証しないため、
    ここで必ずソートする。
    """
    points: list = []
    for dp in data.get("Datapoints", []):
        ts_raw = dp.get("Timestamp")
        max_raw = dp.get("Maximum")
        if ts_raw is None or max_raw is None:
            continue
        points.append((_parse_iso8601(str(ts_raw)), float(max_raw)))
    points.sort(key=lambda p: p[0])
    return points


def detect_pause(points: list, after_dt) -> Optional[tuple]:
    """after_dt **より後**のタイムスタンプを持つ、値が 0.0 のデータポイントを探す（昇順の先頭を採用）。

    after_dt と同時刻のデータポイントは対象外（「より後」であって「以降」ではない）。
    pause 前の古い 0.0 データポイントを誤検知しないための境界。
    """
    for ts, val in points:
        if ts > after_dt and abs(val) < 1e-9:
            return ts, val
    return None


def fetch_datapoints(cluster_id: str, start_dt: datetime, end_dt: datetime, region: str) -> list:
    try:
        data = cloudwatch_get_metric_statistics(cluster_id, iso(start_dt), iso(end_dt), region)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        print(f"  WARNING: aws cloudwatch get-metric-statistics failed: {stderr}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"  WARNING: aws cloudwatch get-metric-statistics returned unparsable JSON: {e}", file=sys.stderr)
        return []
    return parse_datapoints(data)


def wait_for_pause(cluster_id: str, region: str, after_dt: datetime, max_wait_s: float,
                    keepwarm_fn, keepwarm_interval: float) -> Optional[tuple]:
    """after_dt より後の ServerlessDatabaseCapacity Maximum が 0.0 になるまで 30 秒おきにポーリングする。

    keepwarm_fn が指定されていれば（scenario=warm）、keepwarm_interval 秒おきに呼び出して
    実行環境を生かし続ける。max_wait_s を超えたら None を返す（呼び出し側で非0終了させる）。
    """
    poll_interval = 30.0
    wait_start_mono = time.monotonic()
    last_keepwarm_mono = wait_start_mono
    while True:
        now_dt = datetime.now(timezone.utc)
        # メトリクスは数分遅れて出てくる前提のため、開始時刻より少し前から・現在より少し先まで
        # 問い合わせ窓を広げておく（開始時刻より前の 0.0 は detect_pause 側の境界で除外される）。
        points = fetch_datapoints(cluster_id, after_dt - timedelta(minutes=1), now_dt + timedelta(minutes=1), region)
        detected = detect_pause(points, after_dt)
        elapsed = time.monotonic() - wait_start_mono
        if points:
            latest_ts, latest_val = points[-1]
            latest_str = f"{iso(latest_ts)}={latest_val}"
        else:
            latest_str = "no datapoints yet"
        print(f"  [pause-wait] elapsed={elapsed:.0f}s latest={latest_str}")

        if detected is not None:
            return detected
        if elapsed >= max_wait_s:
            return None

        if keepwarm_fn is not None and (time.monotonic() - last_keepwarm_mono) >= keepwarm_interval:
            keepwarm_fn()
            last_keepwarm_mono = time.monotonic()

        time.sleep(poll_interval)


# ── 集計・表示 ────────────────────────────────────────────────────────────


def _fmt_ms(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def summarize_and_print(records: list, scenario: str) -> None:
    n = len(records)
    cold_n = sum(1 for r in records if r["cold"])
    s_total = stats([r["client_total_ms"] for r in records])
    s_dur = stats([r["duration_ms"] for r in records])
    status_counts: dict = defaultdict(int)
    for r in records:
        status_counts[r["http_status"]] += 1
    status_str = ", ".join(
        f"{k}:{v}" for k, v in sorted(status_counts.items(), key=lambda kv: (kv[0] is None, kv[0]))
    )
    print(
        f"  summary: n={n} cold={cold_n} "
        f"client_total_ms(median/min/max)={_fmt_ms(s_total['median'])}/{_fmt_ms(s_total['min'])}/{_fmt_ms(s_total['max'])} "
        f"duration_ms(median/min/max)={_fmt_ms(s_dur['median'])}/{_fmt_ms(s_dur['min'])}/{_fmt_ms(s_dur['max'])} "
        f"status=[{status_str}]"
    )
    if scenario == "warm" and cold_n != 0:
        print(
            f"  WARNING: scenario=warm なのに cold={cold_n} 件検出されました"
            "（keepwarm 中に実行環境が回収された可能性があります）",
            file=sys.stderr,
        )


# ── dry-run ───────────────────────────────────────────────────────────────


def dry_run_plan(urls: dict, args) -> None:
    url = urls[args.target]
    function_name = function_name_for_key(args.target)
    app, package = app_and_package_for_key(args.target)
    accept = "text/html" if app == "web" else "application/json"

    print(f"[dry-run] target={args.target} function={function_name} app={app} package={package} scenario={args.scenario}")
    if args.scenario == "warm":
        print(
            f"    Warm-up: {args.concurrency} 本を同時に GET {url.rstrip('/')}/posts "
            f"(Accept: {accept}, timeout=60s) — この結果は記録しない（ステータスのみ表示）"
        )
        print(
            f"    Keepwarm: 待機中 {args.keepwarm_interval}s おきに {args.concurrency} 本の同時 GET "
            f"{url.rstrip('/')}/up (Accept: */*, timeout=30s) を送信し続ける"
        )
    else:
        print("    Cold シナリオのため、待機中は何も送信しない")
    print(
        f"    Pause 待ち: aws cloudwatch get-metric-statistics --namespace AWS/RDS "
        f"--metric-name ServerlessDatabaseCapacity "
        f"--dimensions Name=DBClusterIdentifier,Value={args.cluster_id} "
        f"--start-time <基準時刻-1min> --end-time <now+1min> --period 60 --statistics Maximum "
        f"--region {args.region} --output json "
        f"（30秒おきにポーリング、基準時刻より後のデータポイントで Maximum が 0.0 になるまで、"
        f"上限 {args.max_wait}s。超えたら非0終了）"
    )
    print(
        f"    Measure: pause 検知後に {args.concurrency} 本を同時に GET {url.rstrip('/')}/posts "
        f"(Accept: {accept}, timeout=60s) し、"
        f"aws logs filter-log-events --log-group-name /aws/lambda/{function_name} --region {args.region} "
        f"でログ突き合わせ（log-wait-timeout={args.log_wait_timeout}s）。"
        f"各行に variant=db-paused-{args.scenario} / db_paused_detected_at / scenario を付与"
    )
    print(
        f"    Out: {args.concurrency} 行を追記 -> {args.out} "
        f"(label={args.label} memory={args.memory})"
    )


# ── self-test（オフライン） ────────────────────────────────────────────────


def self_test() -> int:
    """オフラインで CloudWatch メトリクスのパース・pause 検知境界・追加フィールド付与を検証する。

    以下のフィクスチャは全て本エージェントが作成した SYNTHETIC なデータであり、実際の
    CloudWatch / Lambda ログではない。get-metric-statistics の実際の応答は未確認。
    """
    failures: list = []

    def check(name: str, cond: bool) -> None:
        status = "OK" if cond else "FAIL"
        print(f"  [{status}] {name}")
        if not cond:
            failures.append(name)

    # -- 1) parse_datapoints / detect_pause: 基準時刻より前の 0.0 を無視し、後の 0.0 を検知する --
    print("== self-test 1: parse_datapoints / detect_pause (SYNTHETIC) ==")
    after_dt = _parse_iso8601("2026-09-28T09:00:00+00:00")
    # SYNTHETIC: AWS CLI は Datapoints の順序を保証しないため、あえて時系列を前後させて渡す。
    fake_metric_response = {
        "Label": "ServerlessDatabaseCapacity",
        "Datapoints": [
            {"Timestamp": "2026-09-28T09:05:00+00:00", "Maximum": 0.5, "Unit": "None"},
            # 基準時刻より前の 0.0（過去の pause の名残。誤検知してはいけない）
            {"Timestamp": "2026-09-28T08:50:00+00:00", "Maximum": 0.0, "Unit": "None"},
            # 基準時刻と同時刻の 0.0（「より後」ではないので対象外にすべき境界ケース）
            {"Timestamp": "2026-09-28T09:00:00+00:00", "Maximum": 0.0, "Unit": "None"},
            # 基準時刻より後の 0.0（これを検知すべき）
            {"Timestamp": "2026-09-28T09:10:00+00:00", "Maximum": 0.0, "Unit": "None"},
        ],
    }
    points = parse_datapoints(fake_metric_response)
    check("4件パースできる", len(points) == 4)
    check("時系列昇順にソートされている", points == sorted(points, key=lambda p: p[0]))

    detected = detect_pause(points, after_dt)
    check("pause を検知できる", detected is not None)
    if detected is not None:
        check("検知したタイムスタンプは 09:10 のデータポイント", detected[0] == _parse_iso8601("2026-09-28T09:10:00+00:00"))
        check("検知した値は 0.0", detected[1] == 0.0)

    print("== self-test 2: 基準時刻より前の 0.0 しか無ければ検知しない (SYNTHETIC) ==")
    points_no_pause = [
        (_parse_iso8601("2026-09-28T08:50:00+00:00"), 0.0),
        (_parse_iso8601("2026-09-28T09:00:00+00:00"), 0.0),  # 境界（同時刻）も対象外
        (_parse_iso8601("2026-09-28T09:05:00+00:00"), 0.5),
        (_parse_iso8601("2026-09-28T09:10:00+00:00"), 1.0),
    ]
    check("検知されない（None）", detect_pause(points_no_pause, after_dt) is None)

    print("== self-test 3: データポイントが空 ==")
    check("空リストでは検知しない", detect_pause([], after_dt) is None)
    check("空応答の parse_datapoints は空リスト", parse_datapoints({}) == [])
    check("Datapoints キー無しの応答も空リスト", parse_datapoints({"Label": "x"}) == [])

    # -- 4) build_record（cold_start.py 由来）への db_paused 追加フィールド付与 --
    print("== self-test 4: build_record 再利用 + db_paused 追加フィールド (SYNTHETIC) ==")
    stream_name = "2026/09/28/[$LATEST]dbpaused01"
    events = [
        {
            "logStreamName": stream_name,
            "timestamp": 1000,
            "message": (
                "REPORT RequestId: 33333333-3333-3333-3333-333333333333\t"
                "Duration: 15000.00 ms\tBilled Duration: 15000 ms\tMemory Size: 1024 MB\t"
                "Max Memory Used: 210 MB"
            ),
        },
    ]
    streams = build_stream_index(events)
    fake_args = argparse.Namespace(label="dbpaused-selftest", memory=1024, variant="db-paused-cold")
    r = RequestResult(
        wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=15005.0,
        request_id="33333333-3333-3333-3333-333333333333", body_bytes=10, error=None,
    )
    rec = build_record("API_CONTAINER", "rlwa-api-container", "api", "container", fake_args, r, streams)
    rec["db_paused_detected_at"] = iso(detected[0]) if detected else None
    rec["scenario"] = "cold"
    check("build_record がログにマッチする", rec["log_matched"] is True)
    check("Duration が 15000ms（DB復帰待ちがここに乗る想定。実機未検証）", rec["duration_ms"] == 15000.0)
    check("db_paused_detected_at が付与されている", rec["db_paused_detected_at"] == "2026-09-28T09:10:00+00:00")
    check("scenario フィールドが付与されている", rec["scenario"] == "cold")
    check("variant は db-paused-cold", rec["variant"] == "db-paused-cold")

    # -- 5) summarize_and_print が例外を投げない、warm+cold混在でも動く --
    print("== self-test 5: summarize_and_print ==")
    rec_warm_like = dict(rec)
    rec_warm_like["cold"] = True
    try:
        summarize_and_print([rec, rec_warm_like], "warm")
        check("summarize_and_print が例外を投げない（warm+cold混在時も）", True)
    except Exception as e:  # noqa: BLE001
        check(f"summarize_and_print が例外を投げた: {e!r}", False)

    try:
        summarize_and_print([], "cold")
        check("summarize_and_print が空リストでも例外を投げない", True)
    except Exception as e:  # noqa: BLE001
        check(f"summarize_and_print が空リストで例外を投げた: {e!r}", False)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} 件失敗: {failures}")
        return 1
    print("SELF-TEST PASSED: 全チェック OK")
    return 0


# ── メイン処理 ────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--urls", help="KEY=URL 形式のファイル（例: bench/results/raw/urls.env）")
    p.add_argument("--target", help="キー 1 つ、例 API_CONTAINER")
    p.add_argument("--scenario", choices=["cold", "warm"], help="cold: 実行環境ごとコールド / warm: 実行環境はウォームのままDBだけpause")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--label", help="JSONL に記録するラベル（例 dbpaused-cold）")
    p.add_argument("--memory", type=int, default=1024, help="記録用（このスクリプトはデプロイしない）")
    p.add_argument("--out", help="出力 JSONL パス（追記）")
    p.add_argument("--region", default="ap-northeast-1")
    p.add_argument("--cluster-id", default="rlwanetworkdbstack-auroracluster23d869c0")
    p.add_argument("--max-wait", type=float, default=1500.0, help="pause 待ちの上限秒")
    p.add_argument("--keepwarm-interval", type=float, default=60.0, help="scenario=warm で /up を送る間隔秒")
    p.add_argument("--log-wait-timeout", type=float, default=180.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--self-test", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    missing = [
        name
        for name, val in (
            ("--urls", args.urls),
            ("--target", args.target),
            ("--scenario", args.scenario),
            ("--label", args.label),
            ("--out", args.out),
        )
        if val is None
    ]
    if missing:
        print(f"ERROR: 次の引数が必要です: {', '.join(missing)}", file=sys.stderr)
        return 2

    urls = load_urls_env(args.urls)
    if args.target not in urls:
        print(f"ERROR: urls ファイルに無いキー: {args.target}", file=sys.stderr)
        return 2

    # build_record は args.label / args.memory / args.variant を直接参照するため、
    # variant をここで組み立てて Namespace に足す。
    args.variant = f"db-paused-{args.scenario}"

    if args.dry_run:
        dry_run_plan(urls, args)
        return 0

    url = urls[args.target]
    function_name = function_name_for_key(args.target)
    app, package = app_and_package_for_key(args.target)
    accept = "text/html" if app == "web" else "application/json"

    if args.scenario == "warm":
        print(f"== warm-up: {args.concurrency} 本の GET /posts（実行環境とDBを温める。この結果は記録しない） ==")
        warm_results, _ = run_batch(url, "/posts", accept, args.concurrency, timeout=60.0)
        print(f"  warm-up statuses={[r.status for r in warm_results]}")

    wait_reference = datetime.now(timezone.utc)
    print(f"== pause 待ち開始（基準時刻={iso(wait_reference)}） ==")

    def keepwarm_ping() -> None:
        kw_results, _ = run_batch(url, "/up", "*/*", args.concurrency, timeout=30.0)
        print(f"  [keepwarm] /up x{args.concurrency} statuses={[r.status for r in kw_results]}")

    keepwarm_fn = keepwarm_ping if args.scenario == "warm" else None
    detected = wait_for_pause(args.cluster_id, args.region, wait_reference, args.max_wait, keepwarm_fn, args.keepwarm_interval)
    if detected is None:
        print(f"ERROR: max-wait ({args.max_wait}s) 経過しても DB の pause を検知できませんでした", file=sys.stderr)
        return 1
    detected_ts, detected_val = detected
    print(f"pause 検知: {iso(detected_ts)} (Maximum={detected_val})")

    print(f"== measure: {args.concurrency} 本の GET /posts ==")
    results, batch_start_wall = run_batch(url, "/posts", accept, args.concurrency, timeout=60.0)
    start_time_ms = int((batch_start_wall - 10) * 1000)
    request_ids = {r.request_id for r in results if r.request_id}
    events = poll_logs_until_matched(function_name, start_time_ms, request_ids, args.region, args.log_wait_timeout)
    streams = build_stream_index(events)

    records = []
    for r in results:
        rec = build_record(args.target, function_name, app, package, args, r, streams)
        rec["db_paused_detected_at"] = iso(detected_ts)
        rec["scenario"] = args.scenario
        records.append(rec)

    append_jsonl(args.out, records)
    summarize_and_print(records, args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
