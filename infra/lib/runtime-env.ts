/**
 * Environment variables that the zip-packaged (ruby3.4 managed runtime + LWA
 * layer) Lambda functions need but that a container image gets for free from
 * its `Dockerfile.lambda` `ENV` instructions.
 *
 * The zip package has no Dockerfile, so every one of these has to be set
 * explicitly on the Lambda function resource instead. Keeping them in one
 * constant (rather than inlining them per function) is what lets us assert
 * "the zip functions see the same runtime environment as the container
 * functions" at a glance.
 *
 * Synced directly from `apps/web/Dockerfile.lambda`'s runtime-stage `ENV`
 * blocks (byte-identical to `apps/api/Dockerfile.lambda` — confirmed with
 * `diff`, and enforced by `scripts/check-shared-code.sh`), NOT guessed:
 *
 *   ENV PORT="8080" AWS_LWA_PORT="8080" AWS_LWA_READINESS_CHECK_PATH="/up"
 *   ENV RAILS_ENV="production" BUNDLE_DEPLOYMENT="true" \
 *       BUNDLE_PATH="/var/task/vendor/bundle" BUNDLE_WITHOUT="development:test"
 *   ENV BOOTSNAP_CACHE_DIR="/var/task/tmp/bootsnap-cache" BOOTSNAP_READONLY="true"
 *   ENV DB_SSLROOTCERT="/var/task/config/certs/rds-global-bundle.pem"
 *
 * `BUNDLE_PATH` / `BUNDLE_DEPLOYMENT` / `BUNDLE_WITHOUT` describe where the
 * zip's `vendor/bundle` lives — true for the zip package too, since
 * `scripts/build-zip.sh` packages the same `bundle install --deployment`
 * output tree (`build/zip/<app>_extracted/vendor/bundle`, confirmed present
 * alongside `Gemfile.lock` in the extracted zip layout). `BOOTSNAP_READONLY`
 * applies identically: Lambda always mounts `/var/task` read-only regardless
 * of package type.
 *
 * NOT copied here: `AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH` /
 * `AWS_LWA_SNAPSTART_AFTER_RESTORE_PATH` — the Dockerfile's own comment says
 * those are deliberately NOT baked into the image (SnapStart-or-not is a
 * per-Lambda-function distinction, not a per-image one); `app-stack.ts` sets
 * them only on the `*-snapstart` functions, container or zip alike.
 *
 * `RlwaAppStack`'s `bootsnap: false` context toggle (which sets
 * `DISABLE_BOOTSNAP=1`) needs no app-side check: `apps/<app>/config/boot.rb`
 * requires `bootsnap/setup`, and bootsnap's own `Bootsnap.default_setup`
 * skips setup entirely when `DISABLE_BOOTSNAP` is set (bootsnap 1.26.0
 * `lib/bootsnap.rb`: `if enabled?("BOOTSNAP")`, where `enabled?(key)` is
 * `!ENV["DISABLE_#{key}"]`). The 2026-09-28 bootsnap-off run confirms it took
 * effect (container Init 2.5-2.9s -> 6.1-6.7s, RESULT.md section 4.5).
 */
export const ZIP_RUNTIME_ENV: Readonly<Record<string, string>> = {
  RAILS_ENV: 'production',
  PORT: '8080',
  AWS_LWA_PORT: '8080',
  AWS_LWA_READINESS_CHECK_PATH: '/up',
  BUNDLE_DEPLOYMENT: 'true',
  BUNDLE_PATH: '/var/task/vendor/bundle',
  BUNDLE_WITHOUT: 'development:test',
  BOOTSNAP_CACHE_DIR: '/var/task/tmp/bootsnap-cache',
  BOOTSNAP_READONLY: 'true',
  DB_SSLROOTCERT: '/var/task/config/certs/rds-global-bundle.pem',
};
