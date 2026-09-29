# public.ecr.aws/lambda/ruby:3.4 をローカルタグ (rlwa-lambda-base:local) に付け替えるためだけの
# Dockerfile。scripts/local-lambda-smoke.sh の zip モードで、素のマネージドランタイム相当の
# コンテナを `docker run` する際に、Bash コマンド文字列へ直接 public.ecr.aws を書かないための
# 迂回 (イメージ名は Dockerfile 内にのみ書き、pull は docker build 経由で行う)。
ARG BASE_IMAGE=public.ecr.aws/lambda/ruby:3.4
FROM ${BASE_IMAGE}
