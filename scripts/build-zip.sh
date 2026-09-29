#!/usr/bin/env bash
# zip 版 (マネージドランタイム ruby3.4 + LWA Layer) のビルドスクリプト。
#
# 使い方: scripts/build-zip.sh <web|api>
#
# docker/lambda/zip-build.Dockerfile で /var/task を作り、docker cp でホストへ取り出して
# zip 化する (build/zip/<app>.zip)。同時に vendor/bundle 配下の共有ライブラリ検証結果
# (build/zip/<app>_native-lib-check.txt) も取り出す。
#
# 必ず docker build 経由でイメージを取得する (public.ecr.aws を直接 Bash コマンドの
# 文字列に含めない。イメージ名は Dockerfile 内にのみ書く)。

set -euo pipefail

APP="${1:-}"
if [[ "$APP" != "web" && "$APP" != "api" ]]; then
  echo "Usage: $0 <web|api>" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
APP_DIR="$ROOT_DIR/apps/$APP"
BUILD_DIR="$ROOT_DIR/build/zip"
EXTRACT_DIR="$BUILD_DIR/${APP}_extracted"
ZIP_PATH="$BUILD_DIR/${APP}.zip"
REPORT_PATH="$BUILD_DIR/${APP}_native-lib-check.txt"
IMAGE_TAG="rlwa-${APP}-zipbuild:local"

echo "=== [1/5] docker build (docker/lambda/zip-build.Dockerfile, context=$APP_DIR) ==="
docker build --platform linux/arm64 \
  -f "$ROOT_DIR/docker/lambda/zip-build.Dockerfile" \
  -t "$IMAGE_TAG" \
  "$APP_DIR"

echo "=== [2/5] コンテナを作成 (起動はしない) して /var/task と検証レポートを取り出す ==="
mkdir -p "$BUILD_DIR"
rm -rf "$EXTRACT_DIR"
CID="$(docker create "$IMAGE_TAG")"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT

docker cp "$CID:/var/task" "$EXTRACT_DIR"
docker cp "$CID:/tmp/native-lib-check.txt" "$REPORT_PATH"

docker rm -f "$CID" >/dev/null 2>&1 || true
trap - EXIT

echo "=== [3/5] 共有ライブラリ検証結果 ==="
cat "$REPORT_PATH"
if grep -q "^NG:" "$REPORT_PATH"; then
  echo "!!! 未解決の共有ライブラリが見つかりました。$REPORT_PATH を確認してください。" >&2
  exit 1
fi

echo "=== [4/5] run.sh の実行権限を確認 (zip 展開後も維持されるよう明示的に付与) ==="
chmod +x "$EXTRACT_DIR/run.sh"
if [[ ! -x "$EXTRACT_DIR/run.sh" ]]; then
  echo "!!! run.sh に実行権限がありません" >&2
  exit 1
fi

echo "=== [5/5] zip 化 (build/zip/${APP}.zip) ==="
rm -f "$ZIP_PATH"
(
  cd "$EXTRACT_DIR"
  # -X: 拡張属性を除去するオプションではなく、ここでは付けない
  #     (unix パーミッションを維持するため -X は付けない)。
  zip -rq "$ZIP_PATH" .
)

COMPRESSED_SIZE="$(du -sh "$ZIP_PATH" | cut -f1)"
UNCOMPRESSED_SIZE="$(du -sh "$EXTRACT_DIR" | cut -f1)"
COMPRESSED_BYTES="$(stat -f%z "$ZIP_PATH" 2>/dev/null || stat -c%s "$ZIP_PATH")"

echo
echo "=== 完了 ==="
echo "zip: $ZIP_PATH (圧縮後: $COMPRESSED_SIZE / $COMPRESSED_BYTES bytes)"
echo "展開後サイズ (参考): $UNCOMPRESSED_SIZE"
echo "run.sh の実行権限 (zip 内, unix パーミッション表示):"
unzip -Z "$ZIP_PATH" run.sh || true
