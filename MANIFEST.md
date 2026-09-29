# MANIFEST（作業チェックリスト）

Rails 8（通常モード / API モード）を AWS Lambda Web Adapter で動かし、必要な設定・ライブラリ・コールドスタート時間を検証する。

状態: `[ ]` 未着手 / `[~]` 作業中 / `[x]` 完了 / `[!]` ブロック

## 確定した前提値（全フェーズ共通）

| 項目 | 値 | 根拠 |
|:---|:---|:---|
| リージョン / アーキテクチャ | ap-northeast-1 / arm64 | ユーザ合意 |
| Ruby | 3.4.11 | Lambda ベースイメージ `lambda/ruby:3.4` の実バージョン（2026-09-24 確認）。Docker Hub `ruby:3.4.11` と一致 |
| Rails | 8.1.4 | 8.1.3.1 は json 3.0（2026-09-07〜）と非互換で JSON リクエストが 400 になる。8.1.4（2026-09-24）の CHANGELOG で修正を確認して更新 |
| pg / puma / bootsnap | 1.6.3 / 8.0.2 / 1.26.0 | rubygems.org latest |
| Lambda Web Adapter | 1.1.0 | GitHub CHANGELOG（2026-09-18） |
| LWA Layer（zip 版） | `arn:aws:lambda:ap-northeast-1:753240598075:layer:LambdaAdapterLayerArm64:30` | GetLayerVersionByArn で description「1.1.0」/ 2026-09-18 作成を確認。README 記載の :28 は 1.0.1（古い） |
| SnapStart | コンテナ版のみ（ruby3.4 マネージドランタイムの zip は非対応）。LWA が `/runtime/restore/next` を実装するため LABEL 不要。フックは `POST` / 2xx 期待 | AWS docs snapstart.html / LWA v1.1.0 `src/snapstart.rs:104-116` |
| Aurora PostgreSQL | 17.9（東京の既定、Serverless v2 min 0 ACU 対応） | DescribeDBEngineVersions |
| ローカル DB | PostgreSQL 17 | Aurora に合わせる |
| DB 構成 | 検証専用の最小 VPC（パブリックサブネット + IGW、NAT なし）+ Aurora PubliclyAccessible、Lambda は VPC 外 | ユーザ合意（default VPC は既存環境で使用中のため使わない） |
| 入口 | Lambda Function URL（AuthType NONE） | ユーザ合意（検証後すぐ削除） |
| コード共有 | 独立 2 アプリ（`apps/web` / `apps/api`）+ 同一の Model/Controller/Migration。DB は共有、migration は api 側が担当 | ユーザ合意 |
| 命名 / タグ | リソース名 `rlwa-` プレフィックス、タグ `Project=rails-lwa-verify` | 既存環境と同じアカウントのため |
| IaC | cdkd（TypeScript CDK） | ユーザ合意 |

## 注意事項

- 既存環境と同じアカウント。既存リソースは lookup も含めて参照しない。
- `public.ecr.aws/...` のイメージ名は Bash コマンドに直接書かない（フック誤検知）。Dockerfile などのファイル内にのみ書く。
- AWS への変更（bootstrap / deploy / destroy）は実行前に必ずユーザ確認。

## フェーズ

### 1. 準備
- [x] ディスク・メモリ・Docker 確認（空き 148GB / Docker 7.7GB）
- [x] git init（`develop` ブランチ）
- [x] 前提値の確定

### 2. Rails 2 アプリ
- [x] `apps/web`（通常モード）生成
- [x] `apps/api`（API モード）生成
- [x] Post / Comment の scaffold（両アプリ同一）
- [x] Lambda 向け設定（tmp / hosts / DB / secret / Puma / 起動時間ログ）。bootsnap のキャッシュ制御はフェーズ3
- [x] docker compose でローカル動作確認（テスト api 10 件 / web 14 件、production スモーク）
- [x] 2 アプリのコード差分チェックスクリプト（`scripts/check-shared-code.sh`）

### 3. パッケージング
- [x] コンテナ版（LWA 同梱）: `apps/<app>/Dockerfile.lambda`、イメージ 1.05GB（うちベースイメージ約 660MB）
- [x] コンテナ + SnapStart 版（LWA フック実装）: イメージは共通、フックはフック用環境変数がある関数でのみマウント
- [x] zip 版 + LWA Layer: `scripts/build-zip.sh`、圧縮後 約 48MB / 展開後 約 135〜139MB
  - pg 1.6.3 のプラットフォーム版 gem が独自 SONAME の libpq（`libpq-ruby-pg.so.1`、RUNPATH は相対）を同梱しているため、**共有ライブラリの追加同梱は不要**（vendor/bundle 配下の全 .so を素のランタイムで ldd 検査して未解決なし）。当初の「libpq / ldap / sasl の同梱が必要」はソースビルド時の想定で、実測で否定
- [x] 読み取り専用 FS・非 root でのローカル起動確認（web/api × container/zip の 4 通り）

### 4. インフラ（cdkd）
- [x] NetworkDb スタック（VPC / SG / Aurora / Secret）
- [x] App スタック（Lambda 7 本（migrate 含む）+ エイリアス live + Function URL 6 本）
- [x] `cdkd synth`（`cdkd diff` は bootstrap 後）
  - DB シークレットは CDK 生成（cdkd は `MasterUserSecret.SecretArn` を解決できない: rds-provider.ts:584-590）
  - Function URL の InvokeFunction 権限は `InvokedViaFunctionUrl: true` 付き
  - Writer に `idle_session_timeout=60000ms` の DB パラメータグループ（インスタンスレベルのパラメータ）。Aurora はアイドル接続が1本でも残ると auto-pause しないため（AWS docs）。ActiveRecord 8.1 が DB 側の切断後に透過的に再接続することはローカル PG 17 で確認済み（PID が入れ替わり、GET 200 / POST 201、アプリ側エラーなし）

### 5. デプロイ（要ユーザ確認）
- [x] `cdkd bootstrap`（2026-09-25。S3 `cdkd-state-<account-id>` / `cdkd-assets-<account-id>-ap-northeast-1`、ECR `cdkd-container-assets-<account-id>-ap-northeast-1`。撤去は `cdkd bootstrap --destroy --include-state-bucket`）
- [x] `cdkd diff --all`: NetworkDb 20（DB パラメータグループ追加後）/ App 58 リソースを新規作成。Aurora クラスタと Writer は Cloud Control 経由（SDK 未対応プロパティのため）
- [x] `cdkd deploy`（2026-09-28）: NetworkDb 20 / App 58 リソース
  - 1回目の NetworkDb は SG ルール説明文の全角ダッシュで失敗し、cdkd が自動ロールバック（残存なしを確認）→ 修正して再デプロイ（509 秒）
  - App は ECR への push で失敗: Docker の containerd イメージストアでは、認証なしの push に ECR が `Your authorization token has expired` を返し、cdkd の認証エラー判定（docker-asset-publisher.ts:61-65）に一致せず再ログインしない（cdkd の不具合）。事前に `docker login` して回避（115 秒）
  - 回避の過程で ECR トークンが macOS キーチェーンに保存された（空の DOCKER_CONFIG でも docker login が credsStore: osxkeychain を自動設定）
- [x] migrate（`rlwa-api-migrate` に `{"task":"db:migrate"}` を invoke。LWA の pass-through → `/_lwa/tasks`）
- [x] 疎通確認（`bench/smoke.sh`）: 6 本とも GET 200 / POST 201。`/_lwa/before_checkpoint` は非 SnapStart で 404、SnapStart で 403（LWA のガード）
  - 初回の参考値（各 1 回）: Init コンテナ 3.4s / 9.8s、zip 7.3s / 7.0s、SnapStart Restore 0.52s / 0.58s。migrate 初回は DB の auto-pause 復帰で 15.8s

### 6. 計測
- [x] 計測スクリプト（`bench/cold_start.py` / `bench/run_matrix.sh` / `bench/aggregate.py`）。セルごとに `coldNonce` で新バージョンを出して全実行環境をコールドにし、同時 10 リクエストを REPORT 行と RequestId で突き合わせる。試行 1 セルで 60/60 件の突き合わせを確認してから本番を実行
- [x] 基本マトリクス（2 アプリ × 3 パッケージ × メモリ 4 段階 × 2 ラウンド = 480 件、全件コールド・HTTP 200）。集計は `bench/results/summary.md`
  - メモリ（512〜3008MB）で Init / Restore はほぼ変わらない。中央値はコンテナ 2.5〜3.0s、SnapStart Restore 0.45〜0.50s、zip 6.5〜7.0s
  - 注意: `infra/cdk.json` の context が `asyncInit: true` のままだったため、この計測は ASYNC_INIT=true で実施（意図は false）。9a2747f で修正。asyncinit-off の 1 セルとの差は -350〜+520ms で、同じ設定のラウンド間のばらつき（最大約 590ms）の範囲内。Init が 9.8s 未満なら ASYNC_INIT は効かないと判断（推測）
- [x] チューニング（1024MB、container / zip の 4 関数、各 10 件）
  - bootsnap-off: コンテナは 2.5〜2.9s → 6.1〜6.7s と遅くなる一方、zip は 6.6〜6.7s → 3.8〜4.7s と速くなる。zip 版の bootsnap は現状逆効果
  - eager_load-off: コンテナ 2.5〜2.9s → 1.8〜1.9s（0.7〜1.0s 短縮）、zip 6.6〜6.7s → 4.6〜5.2s（1.5〜2.0s 短縮）。初回リクエストで遅延ロードが入るが、クライアント側の合計時間でも短縮（コンテナ 3.0〜3.6s → 2.4〜2.6s）
  - yjit-off / asyncinit-on（= baseline と同設定）/ asyncinit-off: baseline と差なし（ばらつきの範囲）
  - zip 版の遅さの原因候補（未検証）: (A) マネージドランタイムの Ruby とビルド用イメージの Ruby の `RUBY_DESCRIPTION` が違い、bootsnap のキャッシュキー（`bootsnap-1.26.0/lib/bootsnap.rb:9`）が全件不一致になる、(B) zip 展開時の mtime 丸め（zip 内は約 8 割が奇数秒の mtime、bootsnap は mtime 完全一致でしかヒットしない: `ext/bootsnap/bootsnap.c:443-455`）。切り分けは zip 関数に `BOOTSNAP_LOG=1` + `BOOTSNAP_REVALIDATE=1` を付けて 1 回起動し、miss か revalidated かを見る（要デプロイ、未実施）
- [x] DB 状態（稼働中 / auto-pause 復帰）。あわせて auto-pause が実際に起きるかを ServerlessDatabaseCapacity / DatabaseConnections で観察する
  - pause 中は ServerlessDatabaseCapacity が 0.0 になる（07:58 / 08:38 の pause で確認）
  - `bench/run_db_paused.sh`（api コンテナ、1024MB、同時 3 本）。pause を ServerlessDatabaseCapacity=0.0 で検知してから GET /posts
  - Lambda コールド + DB pause: Init 2.2〜3.0s（初期化は DB に触らないので影響なし）+ Duration 13.0〜13.9s（DB 復帰待ち）→ クライアント合計 16.5s。3 本とも同時に返る
  - Lambda ウォーム + DB pause: Duration 14.7s → クライアント合計 14.8s。待機中は 60 秒ごとの /up（DB に触らない）で実行環境を維持し、3 本ともウォームのまま（cold=0）
  - 最後の DB アクセスから pause まで約 7 分（09:59:49 → 10:07。idle_session_timeout 60s + auto-pause 300s + メトリクスの 1 分粒度）
  - 関数タイムアウト 30s に収まる（zip の Init 7s + 復帰 15s でも 22s 程度: 見積り）
  - 観察 1（2026-09-28, UTC）: 作成 07:52 → pause 07:58。migrate で 08:31 に復帰（1〜1.5 ACU）、最後のリクエスト 08:32 頃 → DatabaseConnections 0 が 08:33（idle_session_timeout=60s）→ pause 08:38（300s 後）。idle_session_timeout なしとの比較はしていない

### 7. まとめ・撤去
- [x] レポート（`RESULT.md`）
- [ ] `cdkd destroy`（要ユーザ確認）
