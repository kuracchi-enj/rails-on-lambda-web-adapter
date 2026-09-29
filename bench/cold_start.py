#!/usr/bin/env python3
"""コールドスタート計測: 1バッチ分（1つの label/memory/variant の組み合わせ）を実行する。

対象キー（WEB_CONTAINER など、bench/results/urls.env の KEY=URL 形式）ごとに順番に
（対象間は同時に叩かない = DB への同時接続を抑える）、--concurrency 本の GET を
threading.Barrier で開始を揃えて同時に投げる。その後 `aws logs filter-log-events` を
ポーリングして REPORT / INIT_REPORT / RESTORE_REPORT / [boot] / [lwa] の行を
RequestId（またはログストリーム単位）で突き合わせ、1 リクエスト 1 行の JSONL を追記する。

使い方:
    python3 bench/cold_start.py --urls bench/results/urls.env \\
        --targets WEB_CONTAINER,API_ZIP --concurrency 10 \\
        --label mem1024-r1 --memory 1024 --variant baseline \\
        --out bench/results/raw/coldstart-20260928.jsonl

    python3 bench/cold_start.py --self-test    # オフラインでパーサ・突き合わせロジックを検証
    python3 bench/cold_start.py --dry-run ...  # 実行予定の HTTP リクエスト / aws コマンドを表示するだけ

このスクリプトは自分では AWS を一切呼び出さない --self-test / --dry-run 以外の経路でのみ
`aws logs filter-log-events` を subprocess で呼ぶ。本エージェント（実装担当）は AWS アクセス
禁止のため、--self-test と --dry-run でのみ動作確認済み。実機での挙動は未検証。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics  # noqa: F401  (using our own percentile helper, kept for potential debugging use)
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── データ構造 ────────────────────────────────────────────────────────────


@dataclass
class RequestResult:
    """1 本の HTTP リクエストの計測結果。"""

    wall_start: float
    status: Optional[int]
    ttfb_ms: Optional[float]
    total_ms: Optional[float]
    request_id: Optional[str]
    body_bytes: int
    error: Optional[str]


@dataclass
class StreamInfo:
    """1 つの CloudWatch Logs ログストリーム（= 1 つの Lambda 実行環境）から
    抽出した情報。REPORT は RequestId ごとに複数持ちうる（同じ実行環境が
    複数回ウォームで呼ばれる場合）。boot / lwa / restore_report / init_report は
    実行環境ごとに 1 回しか出ないはずなので、単一の値としてマージする。
    """

    reports: dict = field(default_factory=dict)  # request_id -> {field: raw_str}
    init_report: Optional[dict] = None  # {field: raw_str}
    restore_report: Optional[dict] = None  # {field: raw_str}
    boot: dict = field(default_factory=dict)  # key -> float
    lwa: dict = field(default_factory=dict)  # key -> float


# ── ログ行パーサ ──────────────────────────────────────────────────────────


def _num(value_str: Optional[str]) -> Optional[float]:
    """"11.43 ms" / "1024 MB" のような文字列から先頭の数値を取り出す。"""
    if value_str is None:
        return None
    m = re.search(r"[\d.]+", value_str)
    return float(m.group(0)) if m else None


def _parse_report_style_line(body: str) -> dict:
    """REPORT / INIT_REPORT / RESTORE_REPORT 行の "Key: value" 部分をタブ区切りで分解する。

    実際の CloudWatch Logs は実タブ区切りだが、フィクスチャや別経路の取得結果で
    リテラルな "\\t"（バックスラッシュ + t の2文字）になっている可能性も考慮し、
    まずそれを実タブへ変換してから split する。
    """
    body = body.replace("\\t", "\t")
    fields: dict = {}
    for part in body.split("\t"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, value = part.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_space_kv_line(body: str) -> dict:
    """"[boot] secrets_sdk_require_ms=98 secrets_fetch_ms=159" の "key=val" 部分を取り出す。"""
    return {k: v for k, v in re.findall(r"(\w+)=([\d.]+)", body)}


def parse_platform_line(line: str):
    """1 行を分類してパースする。該当しなければ None。

    戻り値: (kind, fields) or None
        kind: "report" | "init_report" | "restore_report" | "boot" | "lwa"
    """
    line = line.strip("\n")
    if line.startswith("REPORT "):
        return "report", _parse_report_style_line(line[len("REPORT "):])
    if line.startswith("INIT_REPORT "):
        return "init_report", _parse_report_style_line(line[len("INIT_REPORT "):])
    if line.startswith("RESTORE_REPORT "):
        return "restore_report", _parse_report_style_line(line[len("RESTORE_REPORT "):])
    if line.startswith("[boot]"):
        return "boot", _parse_space_kv_line(line[len("[boot]"):])
    if line.startswith("[lwa]"):
        return "lwa", _parse_space_kv_line(line[len("[lwa]"):])
    return None


def build_stream_index(events: list) -> dict:
    """CloudWatch Logs の events（filter-log-events の生 JSON の "events" 配列）を
    logStreamName ごとに StreamInfo へ集約する。

    1 つの message に複数行（app 側の print が複数行バッファされた場合）が
    含まれていても対応できるよう、message を改行で分割してから 1 行ずつ判定する。
    """
    events_sorted = sorted(
        events, key=lambda e: (e.get("timestamp", 0), e.get("eventId", ""))
    )
    streams: dict = defaultdict(StreamInfo)
    for event in events_sorted:
        stream_name = event.get("logStreamName", "")
        message = event.get("message", "") or ""
        si = streams[stream_name]
        for line in message.splitlines():
            parsed = parse_platform_line(line)
            if parsed is None:
                continue
            kind, fields = parsed
            if kind == "report":
                rid = fields.get("RequestId")
                if rid:
                    si.reports[rid] = fields
            elif kind == "init_report":
                si.init_report = fields
            elif kind == "restore_report":
                si.restore_report = fields
            elif kind == "boot":
                si.boot.update({k: float(v) for k, v in fields.items()})
            elif kind == "lwa":
                si.lwa.update({k: float(v) for k, v in fields.items()})
    return streams


def find_report_for_request(streams: dict, request_id: str):
    """request_id を持つ REPORT を含むログストリームを探す。(stream_name, StreamInfo) or None。"""
    for stream_name, si in streams.items():
        if request_id in si.reports:
            return stream_name, si
    return None


# ── レコード生成 ──────────────────────────────────────────────────────────


def build_record(key: str, function_name: str, app: str, package: str, args, r: RequestResult, streams: dict) -> dict:
    rec = {
        "label": args.label,
        "memory": args.memory,
        "variant": args.variant,
        "target": key,
        "function_name": function_name,
        "app": app,
        "package": package,
        "wall_start": r.wall_start,
        "http_status": r.status,
        "ttfb_ms": r.ttfb_ms,
        "client_total_ms": r.total_ms,
        "body_bytes": r.body_bytes,
        "request_id": r.request_id,
        "error": r.error,
        "log_stream": None,
        "log_matched": False,
        "duration_ms": None,
        "billed_duration_ms": None,
        "memory_size_mb": None,
        "max_memory_used_mb": None,
        "init_duration_ms": None,
        "restore_duration_ms": None,
        "billed_restore_duration_ms": None,
        "boot_secrets_sdk_require_ms": None,
        "boot_secrets_fetch_ms": None,
        "boot_rails_initialized_ms": None,
        "boot_puma_booted_ms": None,
        "lwa_after_restore_ms": None,
        "lwa_before_checkpoint_ms": None,
        "cold": False,
    }
    if not r.request_id:
        return rec

    found = find_report_for_request(streams, r.request_id)
    if found is None:
        return rec

    stream_name, si = found
    fields = si.reports[r.request_id]
    rec["log_stream"] = stream_name
    rec["log_matched"] = True
    rec["duration_ms"] = _num(fields.get("Duration"))
    rec["billed_duration_ms"] = _num(fields.get("Billed Duration"))
    rec["memory_size_mb"] = _num(fields.get("Memory Size"))
    rec["max_memory_used_mb"] = _num(fields.get("Max Memory Used"))
    rec["init_duration_ms"] = _num(fields.get("Init Duration"))

    restore_ms = _num(fields.get("Restore Duration"))
    billed_restore_ms = _num(fields.get("Billed Restore Duration"))
    if restore_ms is None and si.restore_report is not None:
        # REPORT 行に Restore Duration が無い場合は、同じログストリームの
        # RESTORE_REPORT 行から拾う（未検証の REPORT フォーマットに対するフォールバック）。
        restore_ms = _num(si.restore_report.get("Restore Duration"))
    rec["restore_duration_ms"] = restore_ms
    rec["billed_restore_duration_ms"] = billed_restore_ms

    rec["boot_secrets_sdk_require_ms"] = si.boot.get("secrets_sdk_require_ms")
    rec["boot_secrets_fetch_ms"] = si.boot.get("secrets_fetch_ms")
    rec["boot_rails_initialized_ms"] = si.boot.get("rails_initialized_ms")
    rec["boot_puma_booted_ms"] = si.boot.get("puma_booted_ms")
    rec["lwa_after_restore_ms"] = si.lwa.get("after_restore_ms")
    rec["lwa_before_checkpoint_ms"] = si.lwa.get("before_checkpoint_ms")

    rec["cold"] = (rec["init_duration_ms"] is not None) or (rec["restore_duration_ms"] is not None)
    return rec


# ── 統計 ──────────────────────────────────────────────────────────────────


def _percentile(sorted_vals: list, p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def stats(values: list) -> dict:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p90": None, "min": None, "max": None}
    return {
        "n": len(vals),
        "median": _percentile(vals, 0.5),
        "p90": _percentile(vals, 0.9),
        "min": vals[0],
        "max": vals[-1],
    }


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def print_summary(key: str, records: list) -> None:
    n = len(records)
    cold = [r for r in records if r["cold"]]
    warm_n = n - len(cold)
    if 0 < warm_n < n:
        print(f"  WARNING: {key}: {warm_n}/{n} 件がコールドでない（バッチにウォームが混在）")
    cold_metric_vals = [
        r["init_duration_ms"] if r["init_duration_ms"] is not None else r["restore_duration_ms"]
        for r in cold
    ]
    s_cold = stats(cold_metric_vals)
    s_client = stats([r["client_total_ms"] for r in records])
    matched = sum(1 for r in records if r["log_matched"])
    errors = sum(1 for r in records if r["error"])
    print(
        f"  {key}: n={n} matched_logs={matched} cold={len(cold)} errors={errors} | "
        f"Init/Restore(ms) median={_fmt(s_cold['median'])} p90={_fmt(s_cold['p90'])} "
        f"min={_fmt(s_cold['min'])} max={_fmt(s_cold['max'])} | "
        f"client_total(ms) median={_fmt(s_client['median'])} p90={_fmt(s_client['p90'])}"
    )


# ── HTTP ──────────────────────────────────────────────────────────────────


def do_request(url: str, accept: str, timeout: float = 60.0) -> RequestResult:
    wall_start = time.time()
    t0 = time.monotonic()
    req = urllib.request.Request(url, method="GET", headers={"Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ttfb_ms = (time.monotonic() - t0) * 1000.0
            body = resp.read()
            total_ms = (time.monotonic() - t0) * 1000.0
            return RequestResult(
                wall_start=wall_start,
                status=resp.status,
                ttfb_ms=ttfb_ms,
                total_ms=total_ms,
                request_id=resp.headers.get("x-amzn-RequestId"),
                body_bytes=len(body),
                error=None,
            )
    except urllib.error.HTTPError as e:
        total_ms = (time.monotonic() - t0) * 1000.0
        try:
            body = e.read()
        except Exception:
            body = b""
        request_id = e.headers.get("x-amzn-RequestId") if e.headers else None
        return RequestResult(
            wall_start=wall_start,
            status=e.code,
            ttfb_ms=None,
            total_ms=total_ms,
            request_id=request_id,
            body_bytes=len(body),
            error=f"HTTPError: {e}",
        )
    except Exception as e:  # noqa: BLE001 - 計測スクリプトなので何であれ記録して続行する
        total_ms = (time.monotonic() - t0) * 1000.0
        return RequestResult(
            wall_start=wall_start,
            status=None,
            ttfb_ms=None,
            total_ms=total_ms,
            request_id=None,
            body_bytes=0,
            error=repr(e),
        )


def run_batch(url: str, path: str, accept: str, concurrency: int, timeout: float):
    """threading.Barrier で開始を揃えた concurrency 本の同時 GET。"""
    barrier = threading.Barrier(concurrency)
    results: list = [None] * concurrency
    full_url = url.rstrip("/") + path

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = do_request(full_url, accept, timeout)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    batch_start_wall = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, batch_start_wall


# ── CloudWatch Logs ───────────────────────────────────────────────────────


def aws_logs_filter(function_name: str, start_time_ms: int, region: str, next_token: Optional[str] = None) -> dict:
    cmd = [
        "aws", "logs", "filter-log-events",
        "--log-group-name", f"/aws/lambda/{function_name}",
        "--start-time", str(start_time_ms),
        "--region", region,
        "--output", "json",
    ]
    if next_token:
        cmd += ["--next-token", next_token]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def fetch_all_events(function_name: str, start_time_ms: int, region: str) -> list:
    events: list = []
    next_token = None
    while True:
        try:
            data = aws_logs_filter(function_name, start_time_ms, region, next_token)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip() if e.stderr else str(e)
            print(f"  WARNING: aws logs filter-log-events failed for {function_name}: {stderr}", file=sys.stderr)
            break
        except json.JSONDecodeError as e:
            print(f"  WARNING: aws logs filter-log-events returned unparsable JSON for {function_name}: {e}", file=sys.stderr)
            break
        events.extend(data.get("events", []))
        next_token = data.get("nextToken")
        if not next_token:
            break
    return events


def poll_logs_until_matched(function_name: str, start_time_ms: int, request_ids: set, region: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    poll_interval = 3.0
    events: list = []
    while True:
        events = fetch_all_events(function_name, start_time_ms, region)
        streams = build_stream_index(events)
        matched = {rid for rid in request_ids if find_report_for_request(streams, rid) is not None}
        if request_ids and matched >= request_ids:
            break
        if time.monotonic() >= deadline:
            missing = request_ids - matched
            if missing:
                sample = sorted(missing)[:5]
                more = "..." if len(missing) > 5 else ""
                print(
                    f"  WARNING: {function_name}: log-wait-timeout ({timeout_s}s) 到達、"
                    f"未マッチ RequestId {len(missing)} 件: {sample}{more}",
                    file=sys.stderr,
                )
            break
        time.sleep(poll_interval)
    return events


# ── ターゲット解決 ────────────────────────────────────────────────────────


def function_name_for_key(key: str) -> str:
    """"WEB_CONTAINER" -> "rlwa-web-container" """
    parts = [p for p in key.lower().split("_") if p]
    return "rlwa-" + "-".join(parts)


def app_and_package_for_key(key: str):
    parts = [p for p in key.upper().split("_") if p]
    app = "web" if parts[0] == "WEB" else "api"
    package = parts[1].lower() if len(parts) > 1 else ""
    return app, package


def load_urls_env(path: str) -> dict:
    urls: dict = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                urls[k] = v
    return urls


# ── メイン処理 ────────────────────────────────────────────────────────────


def process_target(key: str, url: str, args) -> list:
    function_name = function_name_for_key(key)
    app, package = app_and_package_for_key(key)
    path = "/posts"
    accept = "text/html" if app == "web" else "application/json"

    results, batch_start_wall = run_batch(url, path, accept, args.concurrency, timeout=60.0)

    start_time_ms = int((batch_start_wall - 10) * 1000)
    request_ids = {r.request_id for r in results if r.request_id}
    events = poll_logs_until_matched(function_name, start_time_ms, request_ids, args.region, args.log_wait_timeout)
    streams = build_stream_index(events)

    records = [build_record(key, function_name, app, package, args, r, streams) for r in results]
    return records


def append_jsonl(path: str, records: list) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def dry_run_plan(urls: dict, targets: list, args) -> None:
    for key in targets:
        if key not in urls:
            print(f"[dry-run] target={key}: ERROR urls ファイルにキーが見つかりません")
            continue
        url = urls[key]
        function_name = function_name_for_key(key)
        app, package = app_and_package_for_key(key)
        accept = "text/html" if app == "web" else "application/json"
        path = "/posts"
        print(f"[dry-run] target={key} function={function_name} app={app} package={package}")
        print(
            f"    HTTP: {args.concurrency} 本を同時に GET {url.rstrip('/')}{path} "
            f"(Accept: {accept}, timeout=60s, threading.Barrier で開始を揃える)"
        )
        print(
            f"    Logs: aws logs filter-log-events --log-group-name /aws/lambda/{function_name} "
            f"--start-time <batch_start_ms - 10000> --region {args.region} --output json "
            f"（nextToken でページネーション、{args.log_wait_timeout}s を上限に3秒おきポーリング）"
        )
        print(
            f"    Out:  {args.concurrency} 行を追記 -> {args.out} "
            f"(label={args.label} memory={args.memory} variant={args.variant})"
        )


def self_test() -> int:
    """オフラインでパーサ・RequestId 突き合わせロジックを検証する（AWS 呼び出し・HTTP 通信なし）。

    REPORT / [boot] の4行はタスク仕様に記載された実機確認済みの実例をそのまま使う。
    SnapStart の RESTORE_REPORT / REPORT 側の Restore Duration 行は、実機で REPORT 行への
    含まれ方が未検証（仕様書に明記）なため、以下の行は SYNTHETIC（本エージェントが作成した
    合成フィクスチャ、実ログではない）と明記する。RESTORE_REPORT 単独のフォールバック経路と
    REPORT 直接埋め込みの両方の経路を検証する。
    """
    failures: list = []

    def check(name: str, cond: bool) -> None:
        status = "OK" if cond else "FAIL"
        print(f"  [{status}] {name}")
        if not cond:
            failures.append(name)

    fake_args = argparse.Namespace(label="selftest", memory=1024, variant="baseline")

    # -- 1) 通常コールドスタート（container/zip 相当）: 実機ログの実例をそのまま使用 --
    print("== self-test 1: plain cold start (container/zip, non-SnapStart, 実機ログ実例) ==")
    stream_a = "2026/09/28/[$LATEST]aaaa1111"
    events_a = [
        {"logStreamName": stream_a, "timestamp": 1000,
         "message": "[boot] secrets_sdk_require_ms=98 secrets_fetch_ms=159"},
        {"logStreamName": stream_a, "timestamp": 1001, "message": "[boot] rails_initialized_ms=2543"},
        {"logStreamName": stream_a, "timestamp": 1002, "message": "[boot] puma_booted_ms=2579"},
        {"logStreamName": stream_a, "timestamp": 1010,
         "message": (
             "REPORT RequestId: 74998ecc-ed3a-4067-8514-61bbadefd277\t"
             "Duration: 11.43 ms\tBilled Duration: 3370 ms\tMemory Size: 1024 MB\t"
             "Max Memory Used: 199 MB\tInit Duration: 3358.49 ms"
         )},
        # 同一実行環境（同一ストリーム）でのウォーム呼び出し
        {"logStreamName": stream_a, "timestamp": 2000,
         "message": (
             "REPORT RequestId: 2f8c388a-8133-411d-996d-0dde1a6740dd\t"
             "Duration: 1408.64 ms\tBilled Duration: 1409 ms\tMemory Size: 1024 MB\t"
             "Max Memory Used: 208 MB"
         )},
    ]
    streams_a = build_stream_index(events_a)

    rec_cold = build_record(
        "WEB_CONTAINER", "rlwa-web-container", "web", "container", fake_args,
        RequestResult(wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=2.0,
                      request_id="74998ecc-ed3a-4067-8514-61bbadefd277", body_bytes=10, error=None),
        streams_a,
    )
    check("cold record matched log stream", rec_cold["log_matched"] and rec_cold["log_stream"] == stream_a)
    check("Init Duration == 3358.49", rec_cold["init_duration_ms"] == 3358.49)
    check("Duration == 11.43", rec_cold["duration_ms"] == 11.43)
    check("Billed Duration == 3370", rec_cold["billed_duration_ms"] == 3370.0)
    check("Max Memory Used == 199", rec_cold["max_memory_used_mb"] == 199.0)
    check("boot rails_initialized_ms == 2543 (同一ストリームからマージ)", rec_cold["boot_rails_initialized_ms"] == 2543.0)
    check("boot puma_booted_ms == 2579", rec_cold["boot_puma_booted_ms"] == 2579.0)
    check("boot secrets_sdk_require_ms == 98", rec_cold["boot_secrets_sdk_require_ms"] == 98.0)
    check("boot secrets_fetch_ms == 159", rec_cold["boot_secrets_fetch_ms"] == 159.0)
    check("cold フラグ True（Init Duration あり）", rec_cold["cold"] is True)
    check("restore_duration_ms は None（非 SnapStart）", rec_cold["restore_duration_ms"] is None)

    rec_warm = build_record(
        "WEB_CONTAINER", "rlwa-web-container", "web", "container", fake_args,
        RequestResult(wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=2.0,
                      request_id="2f8c388a-8133-411d-996d-0dde1a6740dd", body_bytes=10, error=None),
        streams_a,
    )
    check("ウォーム側は Init Duration なし", rec_warm["init_duration_ms"] is None)
    check("ウォーム側の cold フラグ False", rec_warm["cold"] is False)
    check(
        "ウォーム側も同一ストリームの boot 値を参照できる（仕様: 同じログストリームの値を拾う）",
        rec_warm["boot_rails_initialized_ms"] == 2543.0,
    )

    # -- 2) SnapStart: REPORT 行が直接 Restore Duration を持つケース（SYNTHETIC）--
    print("== self-test 2: SnapStart restore, REPORT が Restore Duration を直接含む場合 (SYNTHETIC) ==")
    stream_snap_init = "2026/09/28/[$LATEST]snapinit01"  # スナップショット作成環境（リクエストを処理しない）
    events_snap_init = [
        {"logStreamName": stream_snap_init, "timestamp": 500, "message": "INIT_REPORT Init Duration: 9908.09 ms"},
        {"logStreamName": stream_snap_init, "timestamp": 501,
         "message": "[boot] secrets_sdk_require_ms=90 secrets_fetch_ms=140"},
        {"logStreamName": stream_snap_init, "timestamp": 502, "message": "[boot] rails_initialized_ms=9500"},
        {"logStreamName": stream_snap_init, "timestamp": 503, "message": "[boot] puma_booted_ms=9560"},
        {"logStreamName": stream_snap_init, "timestamp": 504, "message": "[lwa] before_checkpoint_ms=0"},
    ]
    stream_restored = "2026/09/28/[$LATEST]restoredaa"  # 復元環境（実リクエストを処理する）
    events_snap_restore = [
        {"logStreamName": stream_restored, "timestamp": 1500, "message": "[lwa] after_restore_ms=0"},
        {"logStreamName": stream_restored, "timestamp": 1501, "message": "RESTORE_REPORT Restore Duration: 518.37 ms"},
        # SYNTHETIC: REPORT 行への Restore Duration の含まれ方は実機未検証（仕様書に明記）。
        # ここでは「REPORT 行自体に含まれる」経路を検証するための合成データ。
        {"logStreamName": stream_restored, "timestamp": 1510,
         "message": (
             "REPORT RequestId: 11111111-1111-1111-1111-111111111111\t"
             "Duration: 45.00 ms\tBilled Duration: 563 ms\tMemory Size: 1024 MB\t"
             "Max Memory Used: 210 MB\tRestore Duration: 518.37 ms\tBilled Restore Duration: 519 ms"
         )},
    ]
    streams_snap = build_stream_index(events_snap_init + events_snap_restore)

    rec_snap = build_record(
        "API_SNAPSTART", "rlwa-api-snapstart", "api", "snapstart", fake_args,
        RequestResult(wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=2.0,
                      request_id="11111111-1111-1111-1111-111111111111", body_bytes=10, error=None),
        streams_snap,
    )
    check("復元ストリーム（初期化ストリームではない）にマッチ", rec_snap["log_stream"] == stream_restored)
    check("Restore Duration を REPORT 行から直接取得 == 518.37", rec_snap["restore_duration_ms"] == 518.37)
    check("Billed Restore Duration を REPORT 行から直接取得 == 519", rec_snap["billed_restore_duration_ms"] == 519.0)
    check("cold フラグ True（Restore Duration あり）", rec_snap["cold"] is True)
    check("Init Duration は None（別ストリームの値なので混入しない）", rec_snap["init_duration_ms"] is None)
    check(
        "boot_rails_initialized_ms は None（boot は初期化ストリーム側にしか無い）",
        rec_snap["boot_rails_initialized_ms"] is None,
    )
    check("lwa_after_restore_ms == 0（復元ストリームから取得）", rec_snap["lwa_after_restore_ms"] == 0.0)

    # -- 3) SnapStart: REPORT 行に Restore Duration が無く RESTORE_REPORT にフォールバック（SYNTHETIC）--
    print("== self-test 3: SnapStart restore, REPORT に Restore Duration が無く RESTORE_REPORT にフォールバック (SYNTHETIC) ==")
    stream_restored2 = "2026/09/28/[$LATEST]restoredbb"
    events_snap_restore2 = [
        {"logStreamName": stream_restored2, "timestamp": 1500, "message": "[lwa] after_restore_ms=0"},
        {"logStreamName": stream_restored2, "timestamp": 1501, "message": "RESTORE_REPORT Restore Duration: 600.12 ms"},
        {"logStreamName": stream_restored2, "timestamp": 1510,
         "message": (
             "REPORT RequestId: 22222222-2222-2222-2222-222222222222\t"
             "Duration: 50.00 ms\tBilled Duration: 651 ms\tMemory Size: 1024 MB\tMax Memory Used: 205 MB"
         )},
    ]
    streams_snap2 = build_stream_index(events_snap_restore2)
    rec_snap2 = build_record(
        "WEB_SNAPSTART", "rlwa-web-snapstart", "web", "snapstart", fake_args,
        RequestResult(wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=2.0,
                      request_id="22222222-2222-2222-2222-222222222222", body_bytes=10, error=None),
        streams_snap2,
    )
    check("Restore Duration が RESTORE_REPORT からのフォールバックで == 600.12", rec_snap2["restore_duration_ms"] == 600.12)
    check("フォールバック経路でも cold フラグ True", rec_snap2["cold"] is True)

    # -- 4) 未マッチ RequestId（ログがまだ届いていない） --
    print("== self-test 4: 未マッチ RequestId ==")
    rec_missing = build_record(
        "API_ZIP", "rlwa-api-zip", "api", "zip", fake_args,
        RequestResult(wall_start=0.0, status=200, ttfb_ms=1.0, total_ms=2.0,
                      request_id="00000000-0000-0000-0000-000000000000", body_bytes=10, error=None),
        streams_snap2,
    )
    check("未マッチ -> log_matched False かつ cold False", (not rec_missing["log_matched"]) and rec_missing["cold"] is False)

    # -- 5) 混在バッチの警告パス（例外を投げずに要約できること） --
    print("== self-test 5: cold/warm 混在時の警告パス ==")
    try:
        print_summary("WEB_CONTAINER", [rec_cold, rec_warm])
        check("print_summary が混在バッチで例外を投げない", True)
    except Exception as e:  # noqa: BLE001
        check(f"print_summary が例外を投げた: {e!r}", False)

    # -- 6) function_name_for_key / app_and_package_for_key --
    print("== self-test 6: キー -> 関数名 / app / package の導出 ==")
    check("WEB_CONTAINER -> rlwa-web-container", function_name_for_key("WEB_CONTAINER") == "rlwa-web-container")
    check("API_ZIP -> rlwa-api-zip", function_name_for_key("API_ZIP") == "rlwa-api-zip")
    check("WEB_SNAPSTART -> app=web,package=snapstart", app_and_package_for_key("WEB_SNAPSTART") == ("web", "snapstart"))
    check("API_CONTAINER -> app=api,package=container", app_and_package_for_key("API_CONTAINER") == ("api", "container"))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} 件失敗: {failures}")
        return 1
    print("SELF-TEST PASSED: 全チェック OK")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--urls", help="KEY=URL 形式のファイル（例: bench/results/urls.env）")
    p.add_argument("--targets", help="カンマ区切りのキー、例 WEB_CONTAINER,API_ZIP")
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--label", help="セル名（例 mem1024-r1）")
    p.add_argument("--memory", type=int, help="Lambda メモリ (MB)。JSONL に記録するのみ（デプロイはしない）")
    p.add_argument("--variant", help="baseline / bootsnap-off / eagerload-off / yjit-off / asyncinit-on など")
    p.add_argument("--out", help="出力 JSONL パス（追記）")
    p.add_argument("--region", default="ap-northeast-1")
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
            ("--targets", args.targets),
            ("--label", args.label),
            ("--memory", args.memory),
            ("--variant", args.variant),
            ("--out", args.out),
        )
        if val is None
    ]
    if missing:
        print(f"ERROR: 次の引数が必要です: {', '.join(missing)}", file=sys.stderr)
        return 2

    urls = load_urls_env(args.urls)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = [t for t in targets if t not in urls]
    if unknown:
        print(f"ERROR: urls ファイルに無いキー: {unknown}", file=sys.stderr)
        return 2

    if args.dry_run:
        dry_run_plan(urls, targets, args)
        return 0

    for key in targets:
        print(f"== target={key} ==")
        records = process_target(key, urls[key], args)
        append_jsonl(args.out, records)
        print_summary(key, records)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
