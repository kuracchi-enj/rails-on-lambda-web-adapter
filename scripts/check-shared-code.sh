#!/usr/bin/env bash
# apps/web と apps/api の間で「同一であるべきファイル」が実際に同一かを確認するスクリプト。
# 差分があれば diff を表示して非ゼロで終了する。
# あわせて両アプリの controller のアクション一覧(def 行)を並べて表示する。

set -u

cd "$(dirname "$0")/.." || exit 1

WEB=apps/web
API=apps/api

STATUS=0

# 同一であるべきファイル・ディレクトリ
TARGETS=(
  "app/models"
  "db/migrate"
  "db/schema.rb"
  "config/database.yml"
  "config/lambda_env.rb"
  "config/lambda_lifecycle.rb"
  "config/initializers/boot_timing.rb"
  "run.sh"
  "Dockerfile.lambda"
  ".dockerignore"
)

echo "=== 共有コードの差分チェック (apps/web vs apps/api) ==="
for target in "${TARGETS[@]}"; do
  web_path="$WEB/$target"
  api_path="$API/$target"

  if [ ! -e "$web_path" ] && [ ! -e "$api_path" ]; then
    echo "[SKIP] $target: 両方に存在しません"
    continue
  fi

  if [ ! -e "$web_path" ]; then
    echo "[NG] $target: apps/web に存在しません"
    STATUS=1
    continue
  fi

  if [ ! -e "$api_path" ]; then
    echo "[NG] $target: apps/api に存在しません"
    STATUS=1
    continue
  fi

  if diff -r -q "$web_path" "$api_path" > /dev/null 2>&1; then
    echo "[OK] $target: 同一"
  else
    echo "[NG] $target: 差分あり"
    diff -r -u "$web_path" "$api_path"
    STATUS=1
  fi
done

echo
echo "=== controller のアクション一覧 (def 行) ==="
echo "--- apps/web ---"
if [ -d "$WEB/app/controllers" ]; then
  grep -rn "^\s*def " "$WEB/app/controllers" | sort
else
  echo "(controllers ディレクトリなし)"
fi

echo
echo "--- apps/api ---"
if [ -d "$API/app/controllers" ]; then
  grep -rn "^\s*def " "$API/app/controllers" | sort
else
  echo "(controllers ディレクトリなし)"
fi

echo
if [ "$STATUS" -eq 0 ]; then
  echo "=== 結果: 差分なし ==="
else
  echo "=== 結果: 差分あり (STATUS=$STATUS) ==="
fi

exit "$STATUS"
