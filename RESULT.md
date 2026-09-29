# 検証結果: Rails 8 を AWS Lambda Web Adapter で動かす

- 検証期間: 2026-09-24 〜 2026-09-28
- リージョン / アーキテクチャ: ap-northeast-1（東京）/ arm64
- 目的: 通常モードと API モードの Rails 8 を AWS Lambda Web Adapter（以下 LWA）で動かし、必要な設定・ライブラリとコールドスタート時間を確かめる

本文中の数値は、特に断りがない限り実測値である。実測でないものは「推測」「見積り」と明記した。

## 1. 結論

- 通常モード・API モードとも、Rails 8.1.4 をほぼ標準の構成のまま Lambda で動かせた。3 通りのパッケージ（コンテナイメージ、コンテナイメージ + SnapStart、zip + LWA Layer）のすべてで、画面表示・一覧取得・登録が動いた。
- アプリに追加した gem は、シークレット取得用の `aws-sdk-secretsmanager` だけである。PostgreSQL ドライバの `pg` gem はクライアントライブラリを同梱しているため、zip 版でも共有ライブラリを追加で入れる必要はなかった。
- コールドスタート時の応答時間（1024MB、中央値、計測端末から見た時間）は、SnapStart が約 1.0〜1.2 秒、コンテナが約 3.0〜3.6 秒、zip が約 7.5〜7.7 秒だった。
- 最も効果が大きいのは SnapStart である。初期化 2.5〜2.9 秒が、スナップショットからの復元 0.45〜0.48 秒に置き換わる。
- 次に効果があるのは eager_load の無効化である。コンテナで初期化が 0.7〜1.0 秒短くなり、初回の遅延読み込みを含めても応答時間は 0.6〜0.9 秒短くなった。
- メモリを 512MB から 3008MB に増やしても、初期化時間はほぼ変わらない。一方、初期化直後の最初のリクエストの処理時間は短くなる。512MB は 1024MB と比べて応答時間が 0.3〜0.6 秒長い。
- zip 版では bootsnap（起動高速化のキャッシュ）が効いておらず、無効にしたほうが速かった。原因は特定できていない（6 章）。
- DB（Aurora Serverless v2）が自動停止（auto-pause）している状態では、Lambda がウォームでもコールドでも、最初のリクエストに約 15〜16 秒かかる。DB の復帰待ちが Lambda のコールドスタートよりはるかに大きい。

## 2. 検証した構成

### 2.1 バージョン

| 項目 | バージョン・値 |
|:---|:---|
| Ruby | 3.4.11（Lambda 公式ベースイメージ `lambda/ruby:3.4`、YJIT 有効でビルドされたもの） |
| Rails | 8.1.4 |
| pg / puma / bootsnap | 1.6.3 / 8.0.2 / 1.26.0 |
| Lambda Web Adapter | 1.1.0 |
| LWA Layer（zip 版） | `arn:aws:lambda:ap-northeast-1:753240598075:layer:LambdaAdapterLayerArm64:30`（1.1.0。README 記載の `:28` は 1.0.1 で古い） |
| DB | Aurora PostgreSQL 17.9、Serverless v2（最小 0 ACU / 最大 2 ACU、自動停止は 300 秒） |
| 入口 | Lambda Function URL（認証なし。検証用） |
| IaC | cdkd 0.291.17（TypeScript の CDK 定義をそのまま使う） |

### 2.2 アプリと関数

- アプリは 2 つ用意した。`apps/web` が通常モード、`apps/api` が API モードで、どちらも Post / Comment の同じ Model・Controller・Migration を持つ。DB は 2 つのアプリで共有し、マイグレーションは api 側から流す。
- Lambda 関数は 7 本作った。
  - URL を持つ 6 本: 2 アプリ × 3 パッケージ（コンテナ / コンテナ + SnapStart / zip）
  - マイグレーション専用の 1 本（`rlwa-api-migrate`、URL なし）
- URL を持つ 6 本は、公開バージョンと別名（`live`）を作り、Function URL を別名に付けた。SnapStart は公開バージョンにしか効かないため、条件を揃える目的で全関数を同じ形にした。
- リクエストの流れ: クライアント → Function URL → Lambda（LWA → Puma → Rails）→ Aurora。DB の認証情報と `SECRET_KEY_BASE` は、Lambda の初期化時に Secrets Manager から取得する。
- DB は検証専用の最小 VPC に置いた（パブリックサブネット 2 AZ、NAT なし）。Aurora をパブリックアクセス可能にし、Lambda は VPC の外から TLS で接続する。

## 3. 動かすために必要だった設定・ライブラリ

### 3.1 gem

| gem | 用途・注意点 |
|:---|:---|
| `rails` 8.1.4 | 8.1.3.1 は json 3.0（2026-09-07 公開）と組み合わせると JSON リクエストの解析で例外になり、400 を返す。8.1.4 で修正されている |
| `pg` 1.6.3 | arm64 Linux 向けのビルド済み gem が libpq を同梱している。zip 版の全共有ライブラリを素のランタイム上で検査し、未解決のものがないことを確かめた |
| `aws-sdk-secretsmanager` ~> 1.136 | 初期化時のシークレット取得に使う。`require: false` にして、取得処理の中でだけ読み込む |

`rails new` では、Lambda で使わない機能を外した（Kamal / Thruster / Solid Cache・Queue・Cable / Action Cable / Active Storage / Action Mailbox / Action Text など）。

### 3.2 Rails の設定（production）

| 設定 | 内容 | 理由 |
|:---|:---|:---|
| 許可するホスト | Function URL のホスト名（`*.lambda-url.ap-northeast-1.on.aws`）を許可する | 許可しないと、Host ヘッダの検査でリクエストが拒否される |
| ホスト検査と HTTPS リダイレクトの除外 | `/up` と `/_lwa/` 配下を対象外にする | LWA の起動確認（`/up`）と SnapStart のフックは、Lambda 内部から `127.0.0.1` に HTTP で届く。除外しないと拒否されるか、HTTPS にリダイレクトされる |
| `assume_ssl` / `force_ssl` | 有効のまま | TLS は Function URL 側で終端される |
| キャッシュストア | メモリ上のストアにする | 既定のファイルストアは `tmp/cache` に書き込む。Lambda のアプリ領域は読み取り専用である |
| ジョブの実行方式 | プロセス内の非同期実行にする | Solid Queue を使わないため |
| ログ出力 | 標準出力 | CloudWatch Logs に送られる |
| DB 接続 | 接続先・ユーザー・パスワードを環境変数から読む。`sslmode=verify-full` にし、RDS の CA 証明書をイメージに同梱する | Aurora 側で TLS を必須にしているため |
| Puma | ワーカー 0（1 プロセス）、スレッド 3。Lambda 上では再起動用プラグインを読み込まない | LWA は 1 実行環境に 1 リクエストずつ送るので、複数プロセスは不要 |
| シークレット取得 | 初期化時に Secrets Manager の `BatchGetSecretValue` で DB 認証情報と `SECRET_KEY_BASE` をまとめて取得し、環境変数に入れる（`config/lambda_env.rb`） | 1 回の API 呼び出しで済ませる。所要時間は SDK の読み込みを含めてコンテナで約 0.2 秒、zip で約 0.5 秒 |

### 3.3 起動スクリプトとパッケージング

共通の起動スクリプト（`run.sh`）で、次のことをしている。

- `HOME` と bundler のユーザー領域を `/tmp` 配下にする。Lambda の実行ユーザーは root ではなく、ホームディレクトリに書き込めるとは限らないため。
- bundler の設定（`BUNDLE_DEPLOYMENT` / `BUNDLE_PATH` / `BUNDLE_WITHOUT`）と `RAILS_ENV`・`PORT` の既定値を入れ、`bundle exec puma` を実行する。

コンテナ版（`apps/<app>/Dockerfile.lambda`）の要点は次のとおり。

- ビルドも実行も Lambda 公式ベースイメージ `lambda/ruby:3.4` で行う。
- LWA のバイナリを `/opt/extensions/` に置く。Lambda が拡張機能として自動で起動する。
- ビルド時の作業ディレクトリを、実行時と同じ `/var/task` にする。bootsnap のキャッシュは絶対パスをキーにするため。
- `bootsnap precompile` に加えて、アプリを一度起動してキャッシュを焼き込む。`bootsnap precompile` は require の解決結果（load path cache）を作らないため。
- 実行時は bootsnap を読み取り専用モードにする（`BOOTSNAP_READONLY=true`）。
- イメージサイズは約 1.05GB（うちベースイメージが約 660MB）。

zip 版（`scripts/build-zip.sh`）の要点は次のとおり。

- コンテナ版と同じベースイメージの中でビルドし、`/var/task` の中身を zip にする。サイズは圧縮後約 48MB、展開後約 135〜139MB。
- ハンドラ名を `run.sh` にし、環境変数 `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` を設定する。LWA Layer がこのスクリプトを直接実行する。
- Dockerfile の `ENV` にあたるものがないため、`BUNDLE_*`・`PORT`・`AWS_LWA_*`・`BOOTSNAP_*`・証明書パスなどを、すべて Lambda 関数の環境変数で渡す。bundler の設定は、ビルド時に環境変数で渡しただけでは設定ファイルに残らない。
- 注意: 不要ファイルを消すときに `rack-test` gem まで消すと起動に失敗する。actionpack が実行時に読み込むため。

### 3.4 Lambda と LWA の設定

- `AWS_LWA_PORT=8080`、`AWS_LWA_READINESS_CHECK_PATH=/up`（Rails 標準のヘルスチェック。DB には触らない）
- タイムアウト 30 秒（マイグレーション関数は 300 秒）
- 実行ロールの権限
  - 2 つのシークレットに対する `secretsmanager:GetSecretValue`
  - `secretsmanager:BatchGetSecretValue`。この操作はリソースを絞れないため `"*"` を指定する（AWS のドキュメントの例と同じ形）
- Function URL には、Function URL 経由の呼び出しに限った実行権限を付ける

### 3.5 SnapStart

- 使えるのはコンテナイメージの関数だけである。zip の ruby3.4 マネージドランタイムは SnapStart に対応していない。
- LWA 1.1.0 が SnapStart の復元の仕組みに対応しているため、イメージに特別なラベルは要らない。コンテナ版と同じイメージをそのまま使った。
- フックとして、環境変数 `AWS_LWA_SNAPSTART_BEFORE_CHECKPOINT_PATH` と `AWS_LWA_SNAPSTART_AFTER_RESTORE_PATH` に Rails 側のパスを指定する。LWA はそのパスに POST を送り、2xx の応答を期待する。
  - スナップショット取得の直前: DB 接続をすべて閉じる
  - 復元の直後: DB 接続を閉じ直し、乱数の種を初期化し直す
- フックのルートは、SnapStart 用の環境変数がある関数でだけ有効にした。LWA は外部からフックのパスに来たリクエストを 403 で拒否する（Function URL から叩いて確認した）。
- 公開バージョンを作るたびにスナップショットが作られ、そのときに Rails の初期化が 1 回走る。初回デプロイ時のログでは、この初期化に約 9.4〜10.1 秒かかっていた。
- 復元後の最初のリクエストの処理時間は、コンテナ版の初回と同じくらいかかる（1024MB で 0.3〜0.5 秒）。

### 3.6 DB（Aurora Serverless v2）

- 最小 0 ACU にすると、接続がない状態が 300 秒続いたあとに自動停止する。停止中は ACU 課金が止まる。
- **アイドル接続が 1 本でも残っていると自動停止しない。** Lambda の実行環境は DB 接続を持ったまま残るので、DB 側の `idle_session_timeout` を 60 秒にして、アイドル接続を DB 側から切るようにした。
  - Rails（ActiveRecord 8.1）は、切られた接続を次のリクエストで自動的に張り直す。ローカルの PostgreSQL 17 で、エラーなく再接続することを確かめた。
- 最後の DB アクセスから停止までは約 7 分だった（アイドル接続の切断 60 秒 + 自動停止の待ち 300 秒 + 監視メトリクスの 1 分刻み）。
- `rds.force_ssl=1` にして TLS を必須にした。
- DB の管理者パスワードは CDK で作ったシークレットに入れた。RDS が自動で管理するシークレットは、cdkd でその ARN を取り出せなかったため使っていない。

### 3.7 マイグレーションの実行

- 専用の関数（`rlwa-api-migrate`）を用意した。api のイメージをそのまま使い、URL は付けていない。
- LWA は、HTTP 以外のイベントを指定したパスに POST で転送できる（`AWS_LWA_PASS_THROUGH_PATH=/_lwa/tasks`）。この関数に `{"task":"db:migrate"}` を渡して呼び出すと、Rails 側でその rake タスクが実行される。
- 実行できるタスクは `db:migrate` と `db:migrate:status` だけに限った。それ以外は 400 を返す。

## 4. コールドスタートの計測結果

### 4.1 計測方法

- 1 回の計測（セル）ごとに、関数の環境変数を 1 つ変えて再デプロイした。これで新しいバージョンができ、別名が差し替わるので、すべての実行環境がコールドな状態から始まる。
- 関数ごとに、`GET /posts` を 10 本同時に送った（web は HTML、api は JSON）。関数は 1 本ずつ順番に計測した。
- 各リクエストを、Lambda の実行ログ（REPORT 行）とリクエスト ID で突き合わせた。初期化時間（Init Duration）、SnapStart の復元時間（Restore Duration）、リクエストの処理時間（Duration）をログから取った。
- 応答時間は、計測端末（ローカル PC）からリクエストを送って応答を読み終わるまでの時間である。ネットワークの往復と TLS の接続を含む。
- 計測前に DB を起こしておいた。DB の復帰待ちは、4.6 の計測以外には含まれない。
- 計測したリクエストは 680 本（ほかに DB 停止時が 6 本）で、すべて HTTP 200 だった。1 本のリクエストに 1 つの実行環境が割り当てられ、すべてコールドだったことをログで確かめた。
- 注意: メモリ別の計測とチューニングの計測は、意図と違い `AWS_LWA_ASYNC_INIT=true` で行った（5 章）。4.5 で `false` と比べ、差はばらつきの範囲だった。

### 4.2 メモリ別の初期化時間と応答時間

初期化時間（SnapStart は復元時間）。単位は秒、各マス 20 本の中央値。

| 関数 | 512MB | 1024MB | 1769MB | 3008MB |
|:---|---:|---:|---:|---:|
| web コンテナ | 3.03 | 2.89 | 3.02 | 2.86 |
| api コンテナ | 2.87 | 2.48 | 2.92 | 2.79 |
| web SnapStart（復元） | 0.50 | 0.48 | 0.48 | 0.49 |
| api SnapStart（復元） | 0.47 | 0.45 | 0.49 | 0.46 |
| web zip | 6.76 | 6.70 | 6.55 | 7.00 |
| api zip | 6.74 | 6.64 | 6.74 | 6.68 |

計測端末から見た応答時間。単位は秒、各マス 20 本の中央値。

| 関数 | 512MB | 1024MB | 1769MB | 3008MB |
|:---|---:|---:|---:|---:|
| web コンテナ | 4.06 | 3.57 | 3.63 | 3.38 |
| api コンテナ | 3.67 | 3.04 | 3.37 | 3.28 |
| web SnapStart | 1.61 | 1.15 | 1.04 | 1.11 |
| api SnapStart | 1.24 | 0.99 | 0.99 | 0.94 |
| web zip | 8.32 | 7.70 | 7.38 | 7.96 |
| api zip | 8.00 | 7.50 | 7.45 | 7.48 |

p90 と生データは `bench/results/summary.md` と `bench/results/coldstart.csv` にある。

### 4.3 メモリの影響

- 初期化時間は、メモリを変えてもほぼ一定だった。同じ設定のセル同士でも中央値が最大約 0.6 秒ぶれており、メモリによる差はその範囲に収まる。
- 推測: 初期化フェーズでは、割り当てメモリにかかわらず十分な CPU が与えられている可能性がある。
- 一方、初期化直後の最初のリクエストの処理時間は、メモリを増やすほど短くなった。単位はミリ秒、中央値。

| 関数 | 512MB | 1024MB | 1769MB | 3008MB |
|:---|---:|---:|---:|---:|
| web コンテナ | 735 | 366 | 269 | 261 |
| api コンテナ | 507 | 258 | 208 | 202 |
| web SnapStart | 875 | 497 | 380 | 352 |
| api SnapStart | 573 | 315 | 271 | 245 |
| web zip | 1028 | 499 | 329 | 334 |
| api zip | 693 | 349 | 247 | 235 |

- 応答時間で差が大きいのは 512MB と 1024MB の間である。1769MB 以上ではほとんど変わらない。

### 4.4 1024MB での内訳

中央値。「Rails の初期化」は、Rails の読み込みを始めてから（bundler による gem の準備を含む）初期化が終わるまでの時間で、シークレット取得を含む。Puma の起動や LWA の起動確認は含まない。

| 関数 | Init / 復元 | Rails の初期化 | うち SDK の読み込み | うちシークレット取得 | 最初のリクエスト処理 | 最大メモリ使用量 |
|:---|---:|---:|---:|---:|---:|---:|
| web コンテナ | 2.89s | 2.23s | 0.08s | 0.15s | 0.37s | 211MB |
| api コンテナ | 2.48s | 1.86s | 0.06s | 0.10s | 0.26s | 208MB |
| web SnapStart | 復元 0.48s | （スナップショット時に済） | - | - | 0.50s | 213MB |
| api SnapStart | 復元 0.45s | （スナップショット時に済） | - | - | 0.32s | 209MB |
| web zip | 6.70s | 5.95s | 0.25s | 0.28s | 0.50s | 239MB |
| api zip | 6.64s | 5.90s | 0.28s | 0.28s | 0.35s | 237MB |

- SnapStart の課金対象の復元時間（Billed Restore Duration）は 18〜21ms で、復元時間そのもの（約 450ms）よりずっと短かった。
- zip 版は、Rails の初期化がコンテナ版の約 3 倍、SDK の読み込みが約 3〜4 倍かかっている。ファイルの読み込み全般が遅いか、bootsnap のキャッシュが効いていない（4.5、6 章）。

### 4.5 チューニングの効果（1024MB、コンテナと zip）

初期化時間。単位は秒、中央値。baseline は 20 本、ほかは各 10 本。

| 設定 | web コンテナ | api コンテナ | web zip | api zip |
|:---|---:|---:|---:|---:|
| baseline | 2.89 | 2.48 | 6.70 | 6.64 |
| bootsnap を無効 | 6.68 | 6.06 | 4.71 | 3.78 |
| eager_load を無効 | 1.85 | 1.76 | 5.20 | 4.59 |
| YJIT を無効 | 2.95 | 2.37 | 7.33 | 6.53 |
| ASYNC_INIT を false | 2.54 | 2.73 | 7.22 | 6.89 |

- **bootsnap**: コンテナ版では、無効にすると初期化が約 3.6〜3.8 秒遅くなった。キャッシュが大きく効いている。zip 版では逆に、無効にすると 2.0〜2.9 秒速くなった。zip 版の bootsnap は、何もしないより遅くなる方向に働いている。
- **bootsnap なし同士の比較**: zip 版（3.8〜4.7 秒）のほうがコンテナ版（6.1〜6.7 秒）より速い。推測: zip 版でキャッシュが正しく効けば、SnapStart を使わない構成の中で zip 版が最も速くなる可能性がある。
- **eager_load**: 無効にすると、Rails の初期化はコンテナで 2.2 秒から 0.9 秒に縮んだ。クラスの読み込みが最初のリクエストに回るが、応答時間でもコンテナで 0.6〜0.9 秒（3.57 秒 → 2.63 秒、3.04 秒 → 2.42 秒）、zip で 1.1〜1.8 秒短くなった。
- **YJIT、ASYNC_INIT**: どちらも差はばらつきの範囲だった。ASYNC_INIT は、初期化が 9.8 秒を超えたときだけ働く仕組みである。今回の初期化は最大でも約 7.6 秒なので、効かないのは想定どおり。
- 計測は `GET /posts` の 1 種類だけで、初回のページ描画が重いアプリでは eager_load 無効の効果が小さくなる可能性がある（推測）。

### 4.6 DB が自動停止しているとき（api コンテナ、1024MB、3 本同時）

DB の自動停止を監視メトリクス（`ServerlessDatabaseCapacity` が 0）で確かめてから、`GET /posts` を送った。

| 状態 | Init | リクエスト処理 | 応答時間 |
|:---|---:|---:|---:|
| Lambda コールド + DB 停止中 | 2.2〜3.0s | 13.0〜13.9s | 16.5s |
| Lambda ウォーム + DB 停止中 | - | 14.7s | 14.8s |

- Lambda の初期化は DB に接続しないため、DB が停止していても初期化時間は変わらない。最初のリクエストで DB に接続するところで、DB の復帰を約 13〜15 秒待つ。
- 同時に送った 3 本は、すべて同じ時刻に応答した。3 本とも DB の復帰を待っていた。
- 関数のタイムアウト（30 秒）には収まった。zip 版だと初期化 7 秒 + 復帰 15 秒で約 22 秒になる（見積り。実測していない）。
- ウォームのケースでは、DB が停止するまでの約 7 分間、60 秒ごとに `/up`（DB に触らない）を送って実行環境を残した。3 本ともウォームのまま計測できた。

## 5. 検証中に見つかった問題と対処

| 問題 | 原因 | 対処 |
|:---|:---|:---|
| Rails 8.1.3.1 で JSON の POST が 400 になる | json 3.0 で JSON 解析の引数の受け取り方が変わり、Rails 側と合わなくなった | Rails 8.1.4 に上げた |
| zip 版に libpq などの共有ライブラリが必要だと想定していた | ソースからビルドする場合の想定だった | `pg` 1.6.3 のビルド済み gem がライブラリを同梱しており、追加は不要だった |
| 不要ファイルの削除で起動しなくなった | テスト用に見える `rack-test` gem が、実行時に必要な依存だった | gem のダウンロードキャッシュだけを消す方針にした |
| `bootsnap precompile` だけでは require の解決結果のキャッシュができない | precompile はコンパイルキャッシュしか作らない | ビルド時にアプリを一度起動し、キャッシュを焼き込んだ |
| Aurora が自動停止しない | Lambda の実行環境がアイドルの DB 接続を持ち続ける | DB 側の `idle_session_timeout` を 60 秒にした |
| cdkd のデプロイでセキュリティグループの作成に失敗した | ルールの説明文に全角ダッシュが入っていた | 半角のハイフンに直した。失敗時は cdkd が自動で元に戻し、残ったリソースはなかった |
| cdkd で RDS が自動管理するシークレットの ARN を取り出せない | cdkd の RDS の処理がこの値に対応していない | CDK でシークレットを作り、DB に渡すようにした |
| cdkd がコンテナイメージを ECR に push できない | Docker の containerd イメージストアでは、未ログインの push に ECR が「トークンの期限切れ」と返す。cdkd の判定がこの文言に一致せず、ログインし直さない（cdkd の不具合） | デプロイ前に手動で `docker login` した |
| メモリ別の計測で ASYNC_INIT が意図と違う値だった | CDK の設定ファイル（`infra/cdk.json`）に `asyncInit: true` が残っており、コード側の既定値 `false` を上書きしていた | 設定ファイルを直し、`false` のセルを追加で計測した。差はばらつきの範囲だったため、メモリ別の計測は取り直していない |

## 6. 確認できていないこと・残課題

- **zip 版で bootsnap が逆効果になる原因**。候補は次の 2 つで、どちらも実機では確かめていない。
  - A: マネージドランタイムの Ruby と、ビルドに使ったコンテナイメージの Ruby が同一ビルドではない。bootsnap は Ruby のバージョン文字列（ビルド情報を含む）をキャッシュのキーに使うため、少しでも違うとキャッシュ全体が無効になる。ロードパスの索引も毎回作り直しになる。
  - B: Lambda が zip を展開するとき、ファイルの更新時刻が 2 秒単位に丸められる。bootsnap は更新時刻が 1 秒でもずれるとキャッシュを使わない。今回の zip は約 8 割のファイルが奇数秒の更新時刻を持っていた。zip 自体は正確な時刻（拡張タイムスタンプ）も持っているが、Lambda がそれを使うかはわからない。
  - 切り分け方: zip の関数に環境変数 `BOOTSNAP_LOG=1` と `BOOTSNAP_REVALIDATE=1` を付けて 1 回起動する。キャッシュを使えなかったファイルがログに出る。ほとんどが「miss」なら A、「revalidated」（中身の比較でキャッシュを使えた）なら B である。アプリのコードは変えずに済む。
- **初期化が 9.8 秒を超えた場合の ASYNC_INIT の挙動**。初回デプロイ直後の api コンテナで、初期化が 9.84 秒になったことが 1 回あった。ASYNC_INIT の打ち切り（9.8 秒）が働いた可能性がある（推測）。原因はイメージの初回取得と考えているが、確かめていない。
- **`idle_session_timeout` を設定しない場合の比較**。設定した状態で自動停止することは確かめたが、設定しない場合に停止しないことは確かめていない（AWS のドキュメントの記載に基づく）。
- **マイグレーションの出力**。マイグレーションは実行されるが、応答に含めるはずの出力が空になる。
- **費用**。SnapStart のキャッシュ・復元の料金や、Aurora の復帰を含めた費用は評価していない。
- **本番に持ち込む場合に見直すべき点**。今回は検証のため次のようにしている。
  - DB を公開し、ポート 5432 をどこからでも受け付けている。
  - Function URL に認証がない。
  - DB のストレージを暗号化していない。
  - 本番では VPC 内への配置、Lambda の VPC 接続（または RDS Proxy・Data API）、認証の追加などを検討する必要がある。
- **計測範囲**。計測したのは一覧画面（`GET /posts`）だけである。重い画面や、書き込み系の初回リクエストは計測していない。

## 7. ファイル構成と再実行の方法

| パス | 内容 |
|:---|:---|
| `apps/web`、`apps/api` | Rails アプリ（通常モード / API モード） |
| `apps/<app>/Dockerfile.lambda`、`apps/<app>/run.sh` | コンテナ版のイメージ定義と共通の起動スクリプト |
| `scripts/build-zip.sh` | zip 版のビルド |
| `scripts/local-lambda-smoke.sh` | 読み取り専用・非 root でのローカル起動確認 |
| `scripts/check-shared-code.sh` | 2 つのアプリで共通にすべきファイルの差分確認 |
| `infra/` | cdkd（CDK）のインフラ定義。context でメモリやチューニングを切り替える（`infra/README.md`） |
| `bench/smoke.sh` | デプロイ後の疎通確認 |
| `bench/run_matrix.sh` | メモリ別・チューニングの計測（`pilot` / `memory` / `tuning`） |
| `bench/run_db_paused.sh` | DB 停止時の計測 |
| `bench/aggregate.py` | 集計（`bench/results/summary.md`、`bench/results/coldstart.csv` を出力） |
| `MANIFEST.md` | 作業の記録（時系列の詳細） |

計測を再実行するには、デプロイ後の Function URL を `bench/results/raw/urls.env` に `WEB_CONTAINER=https://...` の形で書き、次を実行する。

```bash
RLWA_URLS=bench/results/raw/urls.env bash bench/run_matrix.sh memory
```

```bash
python3 -B bench/aggregate.py
```

## 8. 撤去

2026-09-29 に撤去した。`infra/` で次の 2 つを実行した。

```bash
AWS_REGION=ap-northeast-1 ./node_modules/.bin/cdkd destroy --all --force
```

```bash
AWS_REGION=ap-northeast-1 ./node_modules/.bin/cdkd bootstrap --destroy --include-state-bucket --region ap-northeast-1 -y
```

- 1 つ目で 2 スタックの 78 リソース（App 58、NetworkDb 20）を、2 つ目で cdkd の S3 バケット 2 つと ECR リポジトリを削除した。どちらもエラーはなかった。
- シークレットは削除予定の期間を置かずに即時削除された。Aurora の日次の自動スナップショットと自動バックアップも、クラスタと一緒に消えた。
- **Aurora の手動スナップショットが 1 件残った。** cdkd は Aurora クラスタを Cloud Control API 経由で削除しており、その削除処理が最終スナップショットを自動で作ったと考えられる（推測）。CDK 側で削除時の方針を「削除」にしていても作られた。手作業で削除した。
- 撤去後、検証用のタグ（`Project=rails-lwa-verify`）が付いたリソースが 0 件であること、Lambda・ロググループ・IAM ロール・VPC・シークレット・S3・ECR が残っていないことを確認した。
