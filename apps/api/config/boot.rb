ENV["BUNDLE_GEMFILE"] ||= File.expand_path("../Gemfile", __dir__)

# 起動時間計測の起点 (rails-on-lambda-web-adapter 検証用)。bootsnap の require より前に取る。
$RLWA_BOOT_T0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)

require "bundler/setup" # Set up gems listed in the Gemfile.
require "bootsnap/setup" # Speed up boot time by caching expensive operations.
