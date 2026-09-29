#!/usr/bin/env bash
# デプロイ後の疎通確認。6 本の Function URL に GET / POST を投げ、ステータスと応答時間を表示する。
# 使い方: bench/smoke.sh <urls.env>
#   urls.env には WEB_CONTAINER_URL などを `KEY=URL` 形式で書く（cdkd deploy の Outputs から作る）
set -uo pipefail
source "$1"

req() { # $1=label $2=method $3=url [$4=json body]
  local out
  if [ -n "${4:-}" ]; then
    out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -X "$2" -H 'Content-Type: application/json' -H 'Accept: application/json' -d "$4" "$3")
  else
    out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -X "$2" -H 'Accept: application/json' "$3")
  fi
  printf '%-16s %-5s %-24s -> %s s\n' "$1" "$2" "${3#https://*/}" "$out"
}

for name in WEB_CONTAINER WEB_SNAPSTART WEB_ZIP API_CONTAINER API_SNAPSTART API_ZIP; do
  base="${!name}"
  base="${base%/}"
  req "$name" GET "$base/up"
  req "$name" GET "$base/posts.json"
  case "$name" in
    WEB_*) req "$name" GET "$base/" ;;
    API_*) req "$name" POST "$base/posts" "{\"post\":{\"title\":\"smoke $name\",\"body\":\"from bench/smoke.sh\"}}" ;;
  esac
  # SnapStart 版以外ではフックがマウントされていないこと、SnapStart 版では LWA が 403 で遮断することを確認する
  req "$name" POST "$base/_lwa/before_checkpoint"
done
