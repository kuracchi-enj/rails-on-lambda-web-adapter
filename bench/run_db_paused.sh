#!/usr/bin/env bash
# Aurora Serverless v2 auto-pause からの DB 復帰時間計測（db_paused.py）のオーケストレーション。
#
# 使い方:
#   bench/run_db_paused.sh [--dry-run]
#
# 手順:
#   (1) cold シナリオ: coldNonce を新しくして cdkd deploy し（全実行環境がコールドになる。
#       DB には触らない）、db_paused.py --scenario cold で DB の pause を待ってから計測する。
#       デプロイに失敗したら中断する。
#   (2) warm シナリオ: デプロイせずに db_paused.py --scenario warm で計測する
#       （db_paused.py が内部で /posts の同時リクエストで実行環境とDBを温め、pause を待つ間
#       /up への同時リクエストで実行環境を生かし続ける）。
#   (3) 最後に memory=1024 のみのデプロイで既定の設定に戻す。
#
# 環境変数:
#   RLWA_URLS            urls.env（KEY=URL 形式）のパス。既定 bench/results/raw/urls.env
#   RLWA_DB_TARGET        計測対象キー。既定 API_CONTAINER
#   RLWA_DB_CONCURRENCY   同時リクエスト数。既定 3
#
# 本スクリプトは cdkd deploy という形で AWS を実際に呼び出す。実装担当エージェントは
# AWS アクセス禁止のため、動作確認は --dry-run のみで行っている（実行予定の全コマンドを
# 表示するだけで、cdkd も python3 も実際には起動しない）。bash 3.2（macOS 標準）互換のため
# 連想配列は使わない。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
INFRA_DIR="${ROOT_DIR}/infra"
RESULTS_DIR="${ROOT_DIR}/bench/results"
RAW_DIR="${RESULTS_DIR}/raw"
URLS_FILE="${RLWA_URLS:-${RAW_DIR}/urls.env}"
TARGET="${RLWA_DB_TARGET:-API_CONTAINER}"
CONCURRENCY="${RLWA_DB_CONCURRENCY:-3}"
REGION="ap-northeast-1"
DATE_TAG="$(date +%Y%m%d)"
JSONL_OUT="${RAW_DIR}/db-paused-${DATE_TAG}.jsonl"

DRY_RUN=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    *)
      echo "Unknown option: ${arg}" >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "${URLS_FILE}" ]]; then
  echo "ERROR: urls ファイルが見つかりません: ${URLS_FILE}（RLWA_URLS で指定するか bench/results/raw/urls.env を用意すること）" >&2
  exit 1
fi

mkdir -p "${RAW_DIR}"

# セルごとに一意な coldNonce を作る（bench/run_matrix.sh の make_nonce と同じ考え方: label の
# 一意性 + PID + 秒 で十分。カウンタ変数はコマンド置換のサブシェル経由では呼び出し元に反映
# されないため持たない）。
make_nonce() { # $1=label
  echo "$1-$$-$(date +%s)"
}

deploy_cell() { # $1=label、残りは cdkd deploy に渡す追加引数（-c key=value ...）
  local label="$1"
  shift
  local log_file="${RAW_DIR}/deploy-${label}.log"
  echo "+ (cd ${INFRA_DIR} && AWS_REGION=${REGION} ./node_modules/.bin/cdkd deploy RlwaAppStack $* > ${log_file} 2>&1)"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    if ! (cd "${INFRA_DIR}" && AWS_REGION="${REGION}" ./node_modules/.bin/cdkd deploy RlwaAppStack "$@" >"${log_file}" 2>&1); then
      echo "ERROR: cdkd deploy が '${label}' で失敗しました。ログ: ${log_file}" >&2
      exit 1
    fi
  fi
}

measure() { # $1=scenario (cold|warm)
  local scenario="$1"
  echo "+ python3 -B ${ROOT_DIR}/bench/db_paused.py --urls ${URLS_FILE} --target ${TARGET} --scenario ${scenario} --concurrency ${CONCURRENCY} --label dbpaused-${scenario} --memory 1024 --out ${JSONL_OUT} --region ${REGION}"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    python3 -B "${ROOT_DIR}/bench/db_paused.py" \
      --urls "${URLS_FILE}" \
      --target "${TARGET}" \
      --scenario "${scenario}" \
      --concurrency "${CONCURRENCY}" \
      --label "dbpaused-${scenario}" \
      --memory 1024 \
      --out "${JSONL_OUT}" \
      --region "${REGION}"
  fi
}

echo "== db-paused: target=${TARGET} concurrency=${CONCURRENCY} out=${JSONL_OUT} =="

# (1) cold シナリオ
cold_nonce="$(make_nonce "dbpaused-cold")"
deploy_cell "dbpaused-cold" -c "memory=1024" -c "coldNonce=${cold_nonce}"
measure "cold"

# (2) warm シナリオ（デプロイなし。db_paused.py が内部で温める）
measure "warm"

# (3) 既定の設定に戻す
deploy_cell "dbpaused-reset" -c "memory=1024"

echo "Done. JSONL: ${JSONL_OUT}"
