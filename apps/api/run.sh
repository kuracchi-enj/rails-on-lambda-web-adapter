#!/bin/sh
# Lambda 用の共通起動スクリプト (web / api で同一内容)。
#
# 使われ方:
#   - コンテナ版: Dockerfile.lambda の ENTRYPOINT ["/var/task/run.sh"] から実行される
#     (Lambda 公式ベースイメージの ENTRYPOINT である /lambda-entrypoint.sh を上書きする)。
#   - zip 版: AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap (LWA Layer) が
#     `exec -- "${LAMBDA_TASK_ROOT}/${_HANDLER}"` を実行する。Lambda の関数ハンドラ名を
#     この "run.sh" というファイル名にすることで、このスクリプトが直接 exec される。
#
# 実行環境の性質 (両方式共通):
#   - /var/task (LAMBDA_TASK_ROOT) は読み取り専用、/tmp のみ書き込み可能。
#   - 実行ユーザーは root ではない (Lambda が動的に払い出す非特権ユーザー)ため、
#     HOME が書き込み可能とは限らない。
set -eu

# $0 が絶対パスであること (コンテナ版 ENTRYPOINT / zip 版ハンドラのどちらも絶対パスで
# 呼ばれる)を前提に、実行時の cwd に依存せず自分のディレクトリへ移動する。
# (Puma の `-C config/puma.rb` が相対パスのため、cwd 次第で解決に失敗しないようにする)
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# HOME / BUNDLE_USER_HOME は書き込み可能な /tmp 配下へ退避させる(既定値。Lambda 側の
# 環境変数で上書き可能)。gem/bundler が起動時に設定ファイルやキャッシュを探しに行った
# ときに読み取り専用 FS 上の HOME (存在しないか書き込めない) に当たってエラーにならないため。
export HOME="${HOME:-/tmp/home}"
export BUNDLE_USER_HOME="${BUNDLE_USER_HOME:-/tmp/bundle_user_home}"
mkdir -p "$HOME" "$BUNDLE_USER_HOME" 2>/dev/null || true

# bootsnap のキャッシュはビルド時にイメージ/zip 側へ焼き込む前提だが、run.sh 単体で
# 起動されるケース(zip をこのスクリプトだけで動かす場合など)でも動くように既定値を
# ここでも設定しておく(Dockerfile.lambda / Lambda 関数設定側の値があればそちらを優先)。
export BOOTSNAP_CACHE_DIR="${BOOTSNAP_CACHE_DIR:-/var/task/tmp/bootsnap-cache}"
export BOOTSNAP_READONLY="${BOOTSNAP_READONLY:-true}"

# bundler の設定 (deployment / path / without) は `bundle install` を ENV 経由で
# 実行しただけでは .bundle/config に永続化されない (実機で確認済み: build/zip/*_extracted/
# に .bundle/config が生成されないことを確認した)。コンテナ版は Dockerfile.lambda の
# イメージ ENV で常に上書きされるが、zip 版は Lambda 関数の環境変数でこれらを渡すのが
# 前提になる。ここでは、Lambda 関数側の環境変数設定を忘れた場合でも `vendor/bundle`
# (このリポジトリのビルドスクリプトが gem を実際に置く場所) を確実に見つけられるよう
# 既定値を設定しておく。
export BUNDLE_DEPLOYMENT="${BUNDLE_DEPLOYMENT:-true}"
export BUNDLE_PATH="${BUNDLE_PATH:-/var/task/vendor/bundle}"
export BUNDLE_WITHOUT="${BUNDLE_WITHOUT:-development:test}"

export RAILS_ENV="${RAILS_ENV:-production}"
export PORT="${PORT:-8080}"

exec bundle exec puma -C config/puma.rb
