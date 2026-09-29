# config/lambda_env.rb
#
# Lambda 実行環境向けの起動前セットアップ。
#
# 目的:
#   ENV["SECRETS_ARNS"](カンマ区切りの Secrets Manager ARN 一覧)が設定されているときだけ、
#   `aws-sdk-secretsmanager` の batch_get_secret_value を1回呼び出して必要な ENV をまとめて展開する。
#   ローカル開発・テストでは SECRETS_ARNS を設定しないため、このファイルは何もしない
#   (aws-sdk-secretsmanager を require すらしない)。
#
# 判定方法:
#   - ENV["DB_SECRET_ARN"] と一致する ARN の中身(JSON, RDS管理シークレット想定 { "username", "password" })
#     -> DB_USERNAME / DB_PASSWORD に展開
#   - ENV["SECRET_KEY_BASE_SECRET_ARN"] と一致する ARN の中身
#     (プレーン文字列、または JSON の "secret_key_base" キー) -> SECRET_KEY_BASE に展開
#   - SECRETS_ARNS はこれらを束ねたカンマ区切りのリストとして渡す想定。
#
# 既に ENV に値がある場合は上書きしない(ローカルでは環境変数で直接渡すため)。
# SDK の読み込み時間と取得にかかった時間を `[boot] secrets_sdk_require_ms=... secrets_fetch_ms=...` の形式で STDOUT に1行出す。

module LambdaEnv
  module_function

  def load!
    secrets_arns = ENV["SECRETS_ARNS"].to_s.split(",").map(&:strip).reject(&:empty?)
    return if secrets_arns.empty?

    t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    require "json"
    require "aws-sdk-secretsmanager"
    t1 = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    client = Aws::SecretsManager::Client.new
    response = client.batch_get_secret_value(secret_id_list: secrets_arns)
    t2 = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    db_secret_arn = ENV["DB_SECRET_ARN"]
    secret_key_base_secret_arn = ENV["SECRET_KEY_BASE_SECRET_ARN"]

    response.secret_values.each do |secret|
      arn = secret.arn
      value = secret.secret_string
      next if value.nil?

      if arn == db_secret_arn
        apply_db_secret(value)
      elsif arn == secret_key_base_secret_arn
        apply_secret_key_base(value)
      end
    end

    # sdk_require: aws-sdk の読み込み時間 / fetch: API 呼び出し（クライアント生成を含む）
    puts "[boot] secrets_sdk_require_ms=#{((t1 - t0) * 1000).round} secrets_fetch_ms=#{((t2 - t1) * 1000).round}"
  end

  def apply_db_secret(json_string)
    data = JSON.parse(json_string)
    set_env_unless_present("DB_USERNAME", data["username"])
    set_env_unless_present("DB_PASSWORD", data["password"])
  rescue JSON::ParserError
    # RDS管理シークレット想定のJSONではない場合は無視する
    nil
  end

  def apply_secret_key_base(raw_string)
    value =
      begin
        data = JSON.parse(raw_string)
        data.is_a?(Hash) ? data["secret_key_base"] : raw_string
      rescue JSON::ParserError
        raw_string
      end
    set_env_unless_present("SECRET_KEY_BASE", value)
  end

  def set_env_unless_present(key, value)
    return if value.nil?
    return unless ENV[key].nil? || ENV[key].empty? # 既に ENV にある値は上書きしない(ローカルは環境変数で直接渡す)

    ENV[key] = value
  end
end
