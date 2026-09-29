# config/lambda_tasks.rb
#
# LWA の非 HTTP イベント転送（AWS_LWA_PASS_THROUGH_PATH）で受け取った Rake タスクを実行する。
# `aws lambda invoke --payload '{"task":"db:migrate"}'` で呼ぶ想定。
#
# Function URL を持たない migrate 専用関数（RLWA_ENABLE_TASK_EVENTS=true）でだけ
# config/routes.rb がマウントする。実行できるのは ALLOWED_TASKS のみ。

require "json"
require "stringio"

module LambdaTasks
  ALLOWED_TASKS = %w[db:migrate db:migrate:status].freeze

  App = lambda do |env|
    payload = env["rack.input"].read
    task = JSON.parse(payload.empty? ? "{}" : payload)["task"]
    unless ALLOWED_TASKS.include?(task)
      body = { error: "task must be one of: #{ALLOWED_TASKS.join(', ')}" }
      next [400, { "Content-Type" => "application/json" }, [body.to_json]]
    end

    require "rake"
    Rails.application.load_tasks unless Rake::Task.task_defined?(task)

    # マイグレーションの出力（ActiveRecord::Migration#say など）は $stdout に出るので、
    # レスポンスで返せるように一時的に差し替える
    output = StringIO.new
    original_stdout = $stdout
    t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    begin
      $stdout = output
      Rake::Task[task].reenable
      Rake::Task[task].invoke
    ensure
      $stdout = original_stdout
    end
    elapsed_ms = ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0) * 1000).round
    puts "[lwa] task=#{task} elapsed_ms=#{elapsed_ms}"

    [200, { "Content-Type" => "application/json" }, [{ task: task, elapsed_ms: elapsed_ms, output: output.string }.to_json]]
  rescue JSON::ParserError, StandardError => e
    [500, { "Content-Type" => "application/json" }, [{ task: task, error: "#{e.class}: #{e.message}" }.to_json]]
  end
end
