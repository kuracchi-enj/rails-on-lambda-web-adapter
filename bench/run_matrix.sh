#!/usr/bin/env bash
# コールドスタート計測のオーケストレーション。
#
# 使い方:
#   bench/run_matrix.sh memory  [--dry-run]   # メモリマトリクス（512/1024/1769/3008 MB × 2 ラウンド × 対象6本）
#   bench/run_matrix.sh tuning  [--dry-run]   # チューニング（bootsnap/eagerload/yjit/asyncinit の各 off/on）
#   bench/run_matrix.sh pilot   [--dry-run]   # 試行: 1024MB の 1 セルだけ。出力は raw/pilot/（集計対象外）
#
# 1 セルの共通手順:
#   (1) infra/ で `cdkd deploy RlwaAppStack -c memory=<M> -c coldNonce=<一意な値> [チューニングの context]`
#       を実行し、ログを bench/results/raw/deploy-<label>.log に保存する。失敗したら中断する。
#   (2) `aws lambda invoke --function-name rlwa-api-migrate ...` で Aurora の auto-pause を起こす
#       （DB 復帰待ちは計測に含めない）。
#   (3) bench/cold_start.py で計測し、JSONL を追記する。
#
# 各フェーズの最後に、context を既定に戻すデプロイ（-c memory=1024 のみ）を行う。
#
# 環境変数:
#   RLWA_URLS  urls.env（KEY=URL 形式）のパス。既定 bench/results/urls.env
#
# 本スクリプトは cdkd deploy / aws lambda invoke という形で AWS を実際に呼び出す。
# 実装担当エージェントは AWS アクセス禁止のため、動作確認は --dry-run のみで行っている
# （実行予定の全コマンドを表示するだけで、cdkd も aws コマンドも python3 も実際には起動しない）。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
INFRA_DIR="${ROOT_DIR}/infra"
RESULTS_DIR="${ROOT_DIR}/bench/results"
RAW_DIR="${RESULTS_DIR}/raw"
URLS_FILE="${RLWA_URLS:-${RESULTS_DIR}/urls.env}"
REGION="ap-northeast-1"
DATE_TAG="$(date +%Y%m%d)"
JSONL_OUT="${RAW_DIR}/coldstart-${DATE_TAG}.jsonl"

ALL_TARGETS="WEB_CONTAINER,API_CONTAINER,WEB_SNAPSTART,API_SNAPSTART,WEB_ZIP,API_ZIP"
TUNING_TARGETS="WEB_CONTAINER,API_CONTAINER,WEB_ZIP,API_ZIP"
TUNING_CDK_TARGETS="rlwa-web-container,rlwa-api-container,rlwa-web-zip,rlwa-api-zip"

DRY_RUN=0
PHASE="${1:-}"
shift || true
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    *)
      echo "Unknown option: ${arg}" >&2
      exit 1
      ;;
  esac
done

if [[ "${PHASE}" != "memory" && "${PHASE}" != "tuning" && "${PHASE}" != "pilot" ]]; then
  echo "Usage: $0 <memory|tuning|pilot> [--dry-run]" >&2
  exit 1
fi

if [[ ! -f "${URLS_FILE}" ]]; then
  echo "ERROR: urls ファイルが見つかりません: ${URLS_FILE}（RLWA_URLS で指定するか bench/results/urls.env を用意すること）" >&2
  exit 1
fi

mkdir -p "${RAW_DIR}"

# セルごとに一意な coldNonce を作る。$1=label はこのスクリプト内で呼び出しごとに
# 必ず異なる値（mem512-r1 / mem512-r2 / tune-bootsnap-off ...）を渡しているため、
# それだけで一意性は決まる。$$ (PID) と date +%s はスクリプトを跨いだ再実行時にも
# 同じ nonce を再利用してしまわないための保険。
# 注意: カウンタ変数をこの関数内でインクリメントして `$(next_nonce ...)` のように
# コマンド置換 (サブシェル) 経由で呼ぶと、そのインクリメントは呼び出し元のシェルに
# 反映されない（bash のコマンド置換は必ずサブシェルで実行されるため）。そのため
# ここではグローバルなカウンタ状態を持たず、引数の label の一意性だけに依拠する。
make_nonce() { # $1=label
  echo "$1-$$-$(date +%s)"
}

# チューニング variant 名 -> cdkd context フラグ 1 個分の値（bash 3.2 に associative array が
# 無いため case で代用）。
variant_context_value() { # $1=variant name
  case "$1" in
    bootsnap-off) echo "bootsnap=false" ;;
    eagerload-off) echo "eagerLoad=false" ;;
    yjit-off) echo "yjit=false" ;;
    asyncinit-on) echo "asyncInit=true" ;;
    asyncinit-off) echo "asyncInit=false" ;;
    *)
      echo "ERROR: unknown variant '$1'" >&2
      exit 1
      ;;
  esac
}

deploy_cell() { # $1=label、残りは cdkd deploy に渡す追加引数（-c key=value ...）
  local label="$1"
  shift
  local log_file="${RAW_DIR}/deploy-${label}.log"
  echo "+ (cd ${INFRA_DIR} && AWS_REGION=${REGION} ./node_modules/.bin/cdkd deploy RlwaAppStack $* > ${log_file} 2>&1)"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    if ! (cd "${INFRA_DIR}" && AWS_REGION="${REGION}" ./node_modules/.bin/cdkd deploy RlwaAppStack "$@" >"${log_file}" 2>&1); then
      echo "ERROR: cdkd deploy がセル '${label}' で失敗しました。ログ: ${log_file}" >&2
      exit 1
    fi
  fi
}

wake_db() { # $1=label（出力ファイル名に使う）
  local label="$1"
  local out_file="${RAW_DIR}/migrate-invoke-${label}.json"
  echo "+ aws lambda invoke --function-name rlwa-api-migrate --payload '{\"task\":\"db:migrate:status\"}' --cli-binary-format raw-in-base64-out --region ${REGION} ${out_file}"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    aws lambda invoke \
      --function-name rlwa-api-migrate \
      --payload '{"task":"db:migrate:status"}' \
      --cli-binary-format raw-in-base64-out \
      --region "${REGION}" \
      "${out_file}"
    # invoke 自体は関数エラーでも終了コード 0 になるため、応答本文でタスクの完了を確かめる
    if ! grep -q '"elapsed_ms"' "${out_file}"; then
      echo "ERROR: DB の起動（db:migrate:status）が失敗しました。応答: ${out_file}" >&2
      exit 1
    fi
  fi
}

measure() { # $1=label $2=memory $3=variant $4=targets(カンマ区切り) $5=concurrency
  local label="$1" memory="$2" variant="$3" targets="$4" concurrency="$5"
  echo "+ python3 ${ROOT_DIR}/bench/cold_start.py --urls ${URLS_FILE} --targets ${targets} --concurrency ${concurrency} --label ${label} --memory ${memory} --variant ${variant} --out ${JSONL_OUT} --region ${REGION}"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    python3 "${ROOT_DIR}/bench/cold_start.py" \
      --urls "${URLS_FILE}" \
      --targets "${targets}" \
      --concurrency "${concurrency}" \
      --label "${label}" \
      --memory "${memory}" \
      --variant "${variant}" \
      --out "${JSONL_OUT}" \
      --region "${REGION}"
  fi
}

reset_to_default() {
  deploy_cell "reset" -c memory=1024
}

if [[ "${PHASE}" == "pilot" ]]; then
  # aggregate.py は raw/*.jsonl だけを読むため、サブディレクトリに出せば集計に混ざらない
  JSONL_OUT="${RAW_DIR}/pilot/coldstart-pilot-${DATE_TAG}.jsonl"
  mkdir -p "${RAW_DIR}/pilot"
  label="pilot-mem1024"
  nonce="$(make_nonce "${label}")"
  deploy_cell "${label}" -c "memory=1024" -c "coldNonce=${nonce}"
  wake_db "${label}"
  measure "${label}" 1024 "baseline" "${ALL_TARGETS}" 10
fi

if [[ "${PHASE}" == "memory" ]]; then
  for memory in 512 1024 1769 3008; do
    for round in 1 2; do
      label="mem${memory}-r${round}"
      nonce="$(make_nonce "${label}")"
      deploy_cell "${label}" -c "memory=${memory}" -c "coldNonce=${nonce}"
      wake_db "${label}"
      measure "${label}" "${memory}" "baseline" "${ALL_TARGETS}" 10
    done
  done
  reset_to_default
fi

if [[ "${PHASE}" == "tuning" ]]; then
  # RLWA_VARIANTS（空白区切り）で計測する variant を絞れる
  for variant in ${RLWA_VARIANTS:-bootsnap-off eagerload-off yjit-off asyncinit-on}; do
    label="tune-${variant}"
    nonce="$(make_nonce "${label}")"
    ctx_value="$(variant_context_value "${variant}")"
    deploy_cell "${label}" -c "memory=1024" -c "${ctx_value}" -c "targets=${TUNING_CDK_TARGETS}" -c "coldNonce=${nonce}"
    wake_db "${label}"
    measure "${label}" 1024 "${variant}" "${TUNING_TARGETS}" 10
  done
  reset_to_default
fi

echo "Done. JSONL: ${JSONL_OUT}"
