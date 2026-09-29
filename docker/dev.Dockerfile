# ローカル動作確認専用の開発用 Dockerfile（apps/web, apps/api で共用）。
# Lambda 用のパッケージング（LWA 同梱など）は別フェーズで扱うため、ここには含めない。
FROM ruby:3.4.11

RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       libyaml-dev \
       postgresql-client \
       curl \
    && rm -rf /var/lib/apt/lists/*

# Gemfile.lock (aarch64-linux) に合わせて bundler をロックファイルと同じバージョンにする。
# 具体的なバージョンは apps/*/Gemfile.lock 生成後に確認して合わせる。
RUN gem install bundler --no-document

WORKDIR /app

CMD ["bash"]
