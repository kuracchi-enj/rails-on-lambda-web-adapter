# config/initializers/boot_timing.rb
#
# 起動時間計測: config/boot.rb で記録した $RLWA_BOOT_T0 を起点に、
# Rails の初期化が完了した時点までの経過時間を STDOUT に出す。

Rails.application.config.after_initialize do
  if defined?($RLWA_BOOT_T0) && $RLWA_BOOT_T0
    elapsed_ms = ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - $RLWA_BOOT_T0) * 1000).round
    puts "[boot] rails_initialized_ms=#{elapsed_ms}"
  end
end
