#!/usr/bin/env bash
# Lambda 環境をできるだけ模擬したローカル起動確認。
#
# 使い方: scripts/local-lambda-smoke.sh <web|api> <container|zip>
#
# - container: apps/<app>/Dockerfile.lambda でビルドしたイメージを
#   --read-only --tmpfs /tmp、非 root ユーザーで `docker run` する。
#   ENTRYPOINT (/var/task/run.sh) がそのまま使われる。LWA (Lambda Extension) は
#   Lambda の Extensions API 経由でしか起動しないため、ローカルでは起動せず
#   puma だけが動く想定 (docker/lambda 配下の probe で実測済み: Lambda 外では
#   "Extension registration failed" で即終了するだけ)。
# - zip: scripts/build-zip.sh <app> が生成した build/zip/<app>_extracted を
#   素のベースイメージ (public.ecr.aws/lambda/ruby:3.4。Bash コマンドに直接
#   書かないよう docker/lambda/base.Dockerfile 経由でローカルタグ
#   rlwa-lambda-base:local を作って使う) に読み取り専用マウントし、
#   ENTRYPOINT を上書きして /var/task/run.sh を実行する。
#
# DB は docker compose -p rlwa の db サービスを使う (ネットワーク rlwa_default)。
# 4通り (web/api × container/zip) をすべて試したあとの
# `docker compose -p rlwa down` (-v なし) は、db を使い回すためこのスクリプトでは
# 行わない。呼び出し側で最後に1回だけ実行すること。

set -euo pipefail

APP="${1:-}"
MODE="${2:-}"

if [[ "$APP" != "web" && "$APP" != "api" ]] || [[ "$MODE" != "container" && "$MODE" != "zip" ]]; then
  echo "Usage: $0 <web|api> <container|zip>" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"

PORT="${RLWA_SMOKE_PORT:-18080}"
CONTAINER_NAME="rlwa-smoke-${APP}-${MODE}"
NETWORK="rlwa_default"
SECRET_KEY_BASE="$(openssl rand -hex 64)"
# Lambda が実行時に動的に払い出す非特権ユーザー (sbx_user 系) の近似値。
# ベースイメージの /etc/passwd には存在しないため数値 uid:gid をそのまま渡す
# (実際の Lambda の uid とは異なる可能性がある。未検証・参考値として扱う)。
NON_ROOT_UID_GID="993:990"

RESULT_DIR="$ROOT_DIR/build/smoke-results"
mkdir -p "$RESULT_DIR"
RESULT_FILE="$RESULT_DIR/${APP}_${MODE}.txt"
LOG_FILE="$RESULT_DIR/${APP}_${MODE}.log"
: > "$RESULT_FILE"

echo "=== [0/9] DB (docker compose -p rlwa up -d db) を起動 ==="
docker compose -p rlwa up -d db

echo "=== [1/9] db の healthy 待ち ==="
for _ in $(seq 1 30); do
  status="$(docker inspect --format '{{.State.Health.Status}}' rlwa-db-1 2>/dev/null || echo starting)"
  [[ "$status" == "healthy" ]] && break
  sleep 1
done
if [[ "$status" != "healthy" ]]; then
  echo "!!! db が healthy になりませんでした (status=$status)" >&2
  exit 1
fi

echo "=== [2/9] production DB (rlwa) のスキーマ準備 (api サービス経由。migration は api 側が担当する運用) ==="
docker compose -p rlwa run --rm \
  -e RAILS_ENV=production \
  -e SECRET_KEY_BASE="$SECRET_KEY_BASE" \
  -e DB_NAME=rlwa \
  -e DB_SSLMODE=disable \
  api bin/rails db:prepare

echo "=== [3/9] 既存の同名コンテナを削除 ==="
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

COMMON_ARGS=(
  -d --name "$CONTAINER_NAME"
  --platform linux/arm64
  --network "$NETWORK"
  --read-only
  --tmpfs /tmp:rw,size=256m,mode=1777
  -u "$NON_ROOT_UID_GID"
  -p "${PORT}:8080"
  -e RAILS_ENV=production
  -e PORT=8080
  -e SECRET_KEY_BASE="$SECRET_KEY_BASE"
  -e DB_HOST=db
  -e DB_PORT=5432
  -e DB_USERNAME=postgres
  -e DB_PASSWORD=postgres
  -e DB_NAME=rlwa
  -e DB_SSLMODE=disable
  # curl は http://127.0.0.1:<port>/... で叩くため Host ヘッダーは "127.0.0.1:<port>" になる。
  # production.rb の config.hosts は RAILS_EXTRA_HOSTS をそのまま追加するだけで
  # localhost <-> 127.0.0.1 の名前解決は行わないため、両方を明示的に許可する。
  -e RAILS_EXTRA_HOSTS="localhost,127.0.0.1"
  -e AWS_LAMBDA_FUNCTION_NAME="$CONTAINER_NAME"
  # routes.rb はこれらの環境変数があるときだけフック / タスクの受け口をマウントする
  -e AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH=/_lwa/before_checkpoint
  -e AWS_LWA_SNAPSTART_AFTER_RESTORE_PATH=/_lwa/after_restore
  -e RLWA_ENABLE_TASK_EVENTS=true
)

if [[ "$MODE" == "container" ]]; then
  echo "=== [4/9] docker build (apps/$APP/Dockerfile.lambda) ==="
  docker build --platform linux/arm64 -f "apps/$APP/Dockerfile.lambda" -t "rlwa-${APP}-lambda:local" "apps/$APP"

  echo "=== [5/9] コンテナ起動 (ENTRYPOINT=/var/task/run.sh) ==="
  docker run "${COMMON_ARGS[@]}" "rlwa-${APP}-lambda:local"
else
  EXTRACT_DIR="$ROOT_DIR/build/zip/${APP}_extracted"
  if [[ ! -d "$EXTRACT_DIR" ]]; then
    echo "=== [4/9] zip 未ビルドのため scripts/build-zip.sh $APP を実行 ==="
    bash scripts/build-zip.sh "$APP"
  else
    echo "=== [4/9] build/zip/${APP}_extracted を使用 (既存) ==="
  fi

  echo "=== [5/9] 素のマネージドランタイム相当コンテナで /var/task を読み取り専用マウントして起動 ==="
  # LD_LIBRARY_PATH は public.ecr.aws/lambda/ruby:3.4 の ENV 既定値 (2026-09-24 確認: docker
  # inspect で取得) をそのまま使う。マネージドランタイム (zip) 自体の値をローカルから
  # 直接確認する方法が無いため、コンテナベースイメージの値からの類推であり未検証。
  docker run "${COMMON_ARGS[@]}" \
    -v "$EXTRACT_DIR:/var/task:ro" \
    -e LD_LIBRARY_PATH="/var/lang/lib:/lib64:/usr/lib64:/var/runtime:/var/runtime/lib:/var/task:/var/task/lib:/opt/lib" \
    --entrypoint /var/task/run.sh \
    rlwa-lambda-base:local
fi

echo "=== [6/9] 起動待ち (GET /up) ==="
READY=0
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/up" || true)"
  if [[ "$code" == "200" ]]; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" != "1" ]]; then
  echo "!!! /up が 200 になりませんでした。ログ:" >&2
  docker logs "$CONTAINER_NAME" > "$LOG_FILE" 2>&1 || true
  tail -100 "$LOG_FILE" >&2 || true
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  exit 1
fi

check() {
  local desc="$1" method="$2" path="$3" expect="$4" data="${5:-}"
  local code
  if [[ -n "$data" ]]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "http://127.0.0.1:${PORT}${path}" -H "Content-Type: application/json" -d "$data")"
  else
    code="$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "http://127.0.0.1:${PORT}${path}")"
  fi
  local status="OK"
  [[ "$code" == "$expect" ]] || status="NG"
  echo "[$status] $desc -> HTTP $code (expect $expect)" | tee -a "$RESULT_FILE"
}

echo "=== [7/9] 疎通確認 ===" | tee -a "$RESULT_FILE"
check "GET /up" GET /up 200
if [[ "$APP" == "web" ]]; then
  check "GET /" GET / 200
fi
check "GET /posts.json" GET /posts.json 200
if [[ "$APP" == "api" ]]; then
  check "POST /posts.json" POST /posts.json 201 '{"post":{"title":"smoke","content":"local-lambda-smoke"}}'
fi
check "POST /_lwa/before_checkpoint" POST /_lwa/before_checkpoint 200
check "POST /_lwa/after_restore" POST /_lwa/after_restore 200
check "GET /posts.json (再接続確認)" GET /posts.json 200
if [[ "$APP" == "api" ]]; then
  check "POST /_lwa/tasks (db:migrate:status)" POST /_lwa/tasks 200 '{"task":"db:migrate:status"}'
  check "POST /_lwa/tasks (許可外のタスク)" POST /_lwa/tasks 400 '{"task":"db:drop"}'
fi

echo "=== [8/9] 起動ログ ([boot] / [lwa]) と 読み取り専用FSエラーの確認 ===" | tee -a "$RESULT_FILE"
docker logs "$CONTAINER_NAME" > "$LOG_FILE" 2>&1 || true
grep -E '^\[boot\]|^\[lwa\]' "$LOG_FILE" | tee -a "$RESULT_FILE" || true
if grep -iE 'read-only file system|Errno::EROFS|permission denied' "$LOG_FILE" > /dev/null; then
  echo "[NG] 読み取り専用/権限エラーのログが見つかりました (詳細: $LOG_FILE)" | tee -a "$RESULT_FILE"
else
  echo "[OK] 読み取り専用/権限エラーのログはありません" | tee -a "$RESULT_FILE"
fi

echo "=== [9/9] このコンテナのみ後片付け (db は残す。呼び出し側で最後に compose down すること) ==="
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo
echo "結果: $RESULT_FILE (ログ全文: $LOG_FILE)"
cat "$RESULT_FILE"
