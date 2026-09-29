require "active_support/core_ext/integer/time"

Rails.application.configure do
  # Settings specified here will take precedence over those in config/application.rb.

  # Code is not reloaded between requests.
  config.enable_reloading = false

  # Eager load code on boot for better performance and memory savings (ignored by Rake tasks).
  config.eager_load = ENV.fetch("RAILS_EAGER_LOAD", "true") == "true"

  # YJIT の有効/無効を Lambda でのチューニング検証用に環境変数で切り替えられるようにする。
  config.yjit = ENV.fetch("RAILS_YJIT", "true") == "true"

  # Full error reports are disabled.
  config.consider_all_requests_local = false

  # Cache assets for far-future expiry since they are all digest stamped.
  config.public_file_server.headers = { "cache-control" => "public, max-age=#{1.year.to_i}" }

  # Enable serving of images, stylesheets, and JavaScripts from an asset server.
  # config.asset_host = "http://assets.example.com"

  # Assume all access to the app is happening through a SSL-terminating reverse proxy.
  config.assume_ssl = true

  # Force all access to the app over SSL, use Strict-Transport-Security, and use secure cookies.
  config.force_ssl = true

  # Skip http-to-https redirect for the default health check endpoint と LWA 内部パス。
  # LWA のヘルスチェック(/up)は 127.0.0.1 宛(http)で来るため、SSLリダイレクト対象から除外する。
  config.ssl_options = {
    redirect: {
      exclude: ->(request) { request.path == "/up" || request.path.start_with?("/_lwa/") }
    }
  }

  # Log to STDOUT with the current request id as a default log tag.
  config.log_tags = [ :request_id ]
  config.logger   = ActiveSupport::TaggedLogging.logger(STDOUT)

  # Change to "debug" to log everything (including potentially personally-identifiable information!).
  config.log_level = ENV.fetch("RAILS_LOG_LEVEL", "info")

  # Prevent health checks from clogging up the logs.
  config.silence_healthcheck_path = "/up"

  # Don't log any deprecations.
  config.active_support.report_deprecations = false

  # solid を使わないため、キャッシュは in-process の :memory_store を明示的に使う
  # (未指定だと既定は tmp/cache への :file_store になり、Lambda の読み取り専用 /tmp 方針と相性が悪い)。
  config.cache_store = :memory_store

  # solid_queue を使わないため、Active Job は :async を明示的に使う。
  config.active_job.queue_adapter = :async

  # Ignore bad email addresses and do not raise email delivery errors.
  # Set this to true and configure the email server for immediate delivery to raise delivery errors.
  # config.action_mailer.raise_delivery_errors = false

  # Set host to be used by links generated in mailer templates.
  config.action_mailer.default_url_options = { host: "example.com" }

  # Specify outgoing SMTP server. Remember to add smtp/* credentials via bin/rails credentials:edit.
  # config.action_mailer.smtp_settings = {
  #   user_name: Rails.application.credentials.dig(:smtp, :user_name),
  #   password: Rails.application.credentials.dig(:smtp, :password),
  #   address: "smtp.example.com",
  #   port: 587,
  #   authentication: :plain
  # }

  # Enable locale fallbacks for I18n (makes lookups for any locale fall back to
  # the I18n.default_locale when a translation cannot be found).
  config.i18n.fallbacks = true

  # Do not dump schema after migrations.
  config.active_record.dump_schema_after_migration = false

  # Only use :id for inspections in production.
  config.active_record.attributes_for_inspect = [ :id ]

  # Enable DNS rebinding protection and other `Host` header attacks.
  # Lambda Function URL のホストと、ローカル検証用の RAILS_EXTRA_HOSTS(カンマ区切り)を許可する。
  config.hosts << /\A[a-z0-9]+\.lambda-url\.ap-northeast-1\.on\.aws\z/
  config.hosts.concat(ENV.fetch("RAILS_EXTRA_HOSTS", "").split(",").map(&:strip).reject(&:empty?))

  # Skip DNS rebinding protection for the default health check endpoint と LWA 内部パス。
  # LWA のヘルスチェック(/up)は 127.0.0.1 宛で来るため Host が合わず、除外しないと弾かれる。
  config.host_authorization = {
    exclude: ->(request) { request.path == "/up" || request.path.start_with?("/_lwa/") }
  }
end
