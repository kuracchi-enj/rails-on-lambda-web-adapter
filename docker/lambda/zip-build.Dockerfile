# syntax=docker/dockerfile:1.7
#
# zip 版 (マネージドランタイム ruby3.4 + LWA Layer) のビルド用 Dockerfile。
# web / api で共用 (アプリのソースは docker build のコンテキストとして渡す。
# scripts/build-zip.sh <web|api> を参照)。
#
# コンテナ版 (Dockerfile.lambda) の build stage とほぼ同じ内容だが、
#   - LWA バイナリは含めない (Layer 側 (LambdaAdapterLayerArm64) から提供されるため)
#   - vendor/bundle 配下の全 .so を ldd で検査し、未解決の共有ライブラリが無いか確認して
#     /tmp/native-lib-check.txt に結果を書き出す (scripts/build-zip.sh が docker cp で回収する)
# という2点が異なる。
#
# ベースイメージは Dockerfile.lambda と同じ public.ecr.aws/lambda/ruby:3.4
# (zip 版のマネージドランタイムと Ruby/OS を揃えるため)。

ARG BASE_IMAGE=public.ecr.aws/lambda/ruby:3.4
FROM ${BASE_IMAGE}

# ldd/readelf/file: 同梱ライブラリの検証用。gcc/gcc-c++/make: msgpack 等の native ext ビルド用。
# zip には含めない (このイメージ自体を zip 化するわけではなく、/var/task だけを
# docker cp で取り出すため、ここでインストールしたツール類は zip サイズに影響しない)。
RUN dnf install -y gcc gcc-c++ make binutils findutils

WORKDIR /var/task

ENV BUNDLE_DEPLOYMENT="true" \
    BUNDLE_PATH="/var/task/vendor/bundle" \
    BUNDLE_WITHOUT="development:test" \
    BUNDLE_JOBS="4"

COPY Gemfile Gemfile.lock ./
RUN bundle install

COPY . .

ENV BOOTSNAP_CACHE_DIR="/var/task/tmp/bootsnap-cache"
RUN bundle exec bootsnap precompile --gemfile app/ config/ lib/

# アセットは web のみ (api には app/assets が無い)。Dockerfile.lambda と同じロジック。
RUN if [ -d app/assets ]; then \
      SECRET_KEY_BASE_DUMMY=1 RAILS_ENV=production bin/rails assets:precompile; \
    fi

ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /var/task/config/certs/rds-global-bundle.pem
RUN chmod 0644 /var/task/config/certs/rds-global-bundle.pem

RUN BOOTSNAP_READONLY=false SECRET_KEY_BASE_DUMMY=1 RAILS_ENV=production \
    DB_HOST=127.0.0.1 DB_SSLMODE=disable \
    bin/rails runner 'puts "[build] warmup ok"'

# --- ネイティブ拡張の共有ライブラリ検証 ---
# vendor/bundle 配下の全 .so (.so 単体 / .so.N のいずれも) について ldd を実行し、
# "not found" (未解決の共有ライブラリ) が無いかを確認する。何が見つかったかも含めて
# /tmp/native-lib-check.txt に残す (zip には含めない)。
# `< /tmp/so_list.txt` によるリダイレクトで while を現在のシェルのまま回す
# (パイプ経由だとサブシェルになり missing 変数がループ外へ伝播しないため)。
RUN set -eu; \
    report=/tmp/native-lib-check.txt; \
    : > "$report"; \
    echo "=== vendor/bundle 配下の .so 一覧と ldd 結果 ===" >> "$report"; \
    find vendor/bundle -type f \( -name "*.so" -o -name "*.so.*" \) | sort > /tmp/so_list.txt; \
    missing=0; \
    while IFS= read -r so; do \
      { echo "--- $so ---"; ldd "$so" 2>&1; } >> "$report"; \
      if ldd "$so" 2>&1 | grep -q "not found"; then missing=1; fi; \
    done < /tmp/so_list.txt; \
    echo "=== 判定 ===" >> "$report"; \
    if [ "$missing" = "1" ]; then \
      echo "NG: 未解決の共有ライブラリ (not found) があります" >> "$report"; \
    else \
      echo "OK: 未解決の共有ライブラリはありません" >> "$report"; \
    fi; \
    cat "$report"

# --- 不要ファイルの削減 (Dockerfile.lambda と同じ方針) ---
# gem ディレクトリ名のワイルドカード判定 (*-test-* 等) は rack-test gem
# (actionpack の正式なランタイム依存) を巻き込んで削除してしまうことが
# scripts/local-lambda-smoke.sh の実行で判明したため使わない。gem のダウンロード
# キャッシュとビルド時の一時ファイルのみを削除する保守的な方針にする。
RUN rm -rf vendor/bundle/ruby/*/cache \
    ; rm -f tmp/local_secret.txt \
    ; rm -rf log/* tmp/cache/* tmp/pids/* \
    ; true
