# config/lambda_lifecycle.rb (web / api で同一内容)
#
# Lambda SnapStart のライフサイクルフック。config/routes.rb から
# POST /_lwa/before_checkpoint と POST /_lwa/after_restore にマウントする、
# ActionController を経由しない素の Rack アプリケーション。
#
# コントローラを経由しないため CSRF 保護 (ActionController::RequestForgeryProtection) の
# 影響を受けない。production.rb 側でも /_lwa/ は host_authorization / ssl_options の
# redirect 対象から除外済み (config/environments/production.rb 参照)。
#
# 呼び出し元: Lambda Web Adapter (LWA) v1.1.0。
#   - AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH に before_checkpoint のパスを指定すると、
#     スナップショット取得の直前に LWA が POST (ボディなし) で呼び出す。
#   - AWS_LWA_SNAPSTART_AFTER_RESTORE_PATH に after_restore のパスを指定すると、
#     スナップショットからの復元直後に LWA が POST (ボディなし) で呼び出す。
#   - どちらも 2xx を返す必要がある (LWA 側の実装は README / src/snapstart.rs 参照)。
#
# config/ 配下は Zeitwerk の autoload/eager_load 対象外なので、require_relative で
# 明示的に読み込む (config/lambda_env.rb と同じやり方)。

module LambdaLifecycle
  module_function

  # ActiveRecord の全コネクション(全 role / 全 shard)を切断する。
  #
  # Rails 8.1 の ConnectionHandler#clear_all_connections! は各コネクションプールに対して
  # pool.disconnect! を呼び、確立済みの TCP/TLS ソケットを閉じる
  # (activerecord-8.1.4/lib/active_record/connection_adapters/abstract/connection_handler.rb
  #  の ConnectionHandler#clear_all_connections! で実装を確認済み)。
  #
  # スナップショット取得時点で開いたままの DB ソケットを凍結してしまうと、復元後に
  # 別の実行環境で同じソケット状態を共有してしまい不正な通信になりかねないため、
  # チェックポイント前には必ず切断しておく。
  def clear_active_record_connections!
    return unless defined?(ActiveRecord::Base)

    ActiveRecord::Base.connection_handler.clear_all_connections!
  end

  # POST /_lwa/before_checkpoint
  BeforeCheckpoint = lambda do |_env|
    t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    clear_active_record_connections!

    elapsed_ms = ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0) * 1000).round
    puts "[lwa] before_checkpoint_ms=#{elapsed_ms}"

    [200, { "Content-Type" => "text/plain" }, ["ok"]]
  end

  # POST /_lwa/after_restore
  AfterRestore = lambda do |_env|
    t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)

    # 復元直後にも改めてコネクションをクリアしておく。次にリクエストが来た時点の
    # 遅延接続 (ActiveRecord の lazy connect) に任せることで、新しい実行環境の
    # ネットワーク経路で確実に接続し直させる。
    clear_active_record_connections!

    # Ruby の既定の疑似乱数生成器 (Kernel#rand / Array#sample など が使う Random::DEFAULT
    # 相当のグローバル PRNG) はプロセス起動時に一度だけシードされ、以後は同じ内部状態を
    # 使い続ける。SnapStart のスナップショットにはこの内部状態もそのまま含まれるため、
    # 再シードしないと、同じスナップショットから復元された複数の実行環境が同一の乱数列を
    # 生成してしまう恐れがある。Random.srand (引数なし) は Random.new_seed 相当の
    # OS 由来の新しいシードで再初期化するため、実行環境ごとに異なる列になる。
    #
    # 一方 SecureRandom (SecureRandom.hex 等、Rails のトークン生成などで使われる) は
    # securerandom 0.4.1 (Ruby 3.4.11 同梱) の実装上、既定で Random.urandom(size) を
    # 直接呼ぶ (gen_random_urandom, ruby/securerandom.rb) ため、呼び出しの都度カーネルの
    # getrandom(2)/`/dev/urandom` から新しい乱数バイト列を取得する。ここには「一度シードした
    # 状態を使い回す」という要素が無いため、SnapStart のスナップショット/復元によって値が
    # 重複する経路にはならない (カーネル側の乱数源が復元後も適切に再シードされる前提。
    # これは Firecracker microVM 側の責務であり Ruby アプリケーションからは検証できない
    # ため、本調査では未検証としてフラグを立てる)。OpenSSL::Random へのフォールバックは
    # Random.urandom が失敗した場合のみ使われる (Linux では通常発生しない)。
    Random.srand

    elapsed_ms = ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0) * 1000).round
    puts "[lwa] after_restore_ms=#{elapsed_ms}"

    [200, { "Content-Type" => "text/plain" }, ["ok"]]
  end
end
