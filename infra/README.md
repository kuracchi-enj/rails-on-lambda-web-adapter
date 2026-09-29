# infra (cdkd)

Rails on Lambda Web Adapter のコールドスタート検証用インフラ。CloudFormation を使わず SDK で直接デプロイする [cdkd](https://github.com/go-to-k/cdkd) で管理する。

既存環境と同じ AWS アカウントにデプロイする。全リソース名は `rlwa-` プレフィックス、全リソースにタグ `Project=rails-lwa-verify` を付与する。既存リソースの lookup は一切行わない（`RlwaNetworkDbStack` の VPC は account を未解決のまま残すことで `Fn::GetAZs` ベースの AZ 選択に固定しており、AWS への問い合わせを発生させない。詳細は `lib/network-db-stack.ts` のコメント）。

## スタック構成

- `RlwaNetworkDbStack` — VPC（パブリックサブネットのみ、2 AZ、NAT なし）、Aurora PostgreSQL Serverless v2、DB マスターユーザ用と SECRET_KEY_BASE 用のシークレット。計測中ずっと残す。
- `RlwaAppStack` — Lambda 7 本（web/api × container/snapstart/zip + api-migrate）。設定を変えて何度も再デプロイする。

`RlwaAppStack` は `RlwaNetworkDbStack` の値をコンストラクトの直接参照で受け取っており（`bin/app.ts`）、cdkd はこれを `Fn::ImportValue` として解決する（`docs/supported-features.md` で cross-stack reference 対応を確認済み）。

## セットアップ

```bash
cd infra
npm install
```

## 型チェック

```bash
npm run typecheck   # = tsc --noEmit
```

## synth

```bash
npx cdkd synth
```

`apps/<app>/Dockerfile.lambda` や `build/zip/<app>.zip` がまだ無い場合、`appsDir` / `zipDir` context でダミーの場所に差し替えられる（下記 context 一覧）。

## diff / deploy / destroy（要 AWS 認証・要ユーザ確認）

```bash
npx cdkd diff
npx cdkd deploy
npx cdkd destroy
```

いずれも cdkd 独自の state 管理用 S3 バケットが必要。優先順位は CLI オプション > `CDKD_STATE_BUCKET` 環境変数 > `cdk.json` の `context.cdkd.stateBucket`（cdkd の `src/cli/config-loader.ts` で確認）。`cdk.json` には含めていないので、デプロイ前に `CDKD_STATE_BUCKET` を設定するか `--state-bucket` を渡すこと。`cdkd bootstrap` の実行が先に必要な場合がある（未検証 —本エージェントは AWS アクセス禁止のため試していない）。

## context 一覧

| context キー | 既定値 | 説明 |
|---|---|---|
| `memory` | `1024` | 全 Lambda 関数のメモリ (MB) |
| `zipDir` | `<repo>/build/zip` | zip 版のビルド成果物ディレクトリ（`web.zip` / `api.zip` を探す） |
| `appsDir` | `<repo>/apps` | コンテナイメージのビルドコンテキスト親ディレクトリ（`<appsDir>/web`, `<appsDir>/api` に `Dockerfile.lambda` を期待） |
| `targets` | 全 7 関数名（カンマ区切り） | チューニング用環境変数を適用する関数を絞り込む |
| `bootsnap` | `true` | `false` で `DISABLE_BOOTSNAP=1` を設定 |
| `eagerLoad` | `true` | `RAILS_EAGER_LOAD` の値 |
| `yjit` | `true` | `RAILS_YJIT` の値 |
| `asyncInit` | `false` | `AWS_LWA_ASYNC_INIT` の値（LWA の既定に合わせる） |
| `coldNonce` | `''`（空） | 空でなければ、URL を持つ 6 関数（web/api × container/snapstart/zip）の環境変数に `RLWA_COLD_NONCE=<値>` を追加する（`rlwa-api-migrate` には追加しない）。値を変えるたびに新しいバージョンが発行され `live` エイリアスが差し替わるため、計測バッチ前に一意な値を渡すと全実行環境をコールドな状態から始められる |

例: メモリを 512MB に、web 系だけ bootsnap を切って diff する。

```bash
npx cdkd diff -c memory=512 -c bootsnap=false -c targets=rlwa-web-container,rlwa-web-snapstart,rlwa-web-zip
```

## synth 動作確認について（本エージェントの検証メモ）

実装中は `apps/*/Dockerfile.lambda` と `build/zip/*.zip` がまだ存在しなかったため、まずスクラッチパッド配下にダミーの Dockerfile（`FROM alpine:3.20` のみ）とダミー zip を用意し、`-c appsDir=... -c zipDir=...` で差し替えて `cdkd synth` を確認した（2 スタック・75 リソースが正常に synth され、`Custom::*` / `AWS::CloudFormation::*` は含まれないことを確認）。

その後、並行して作業していた別エージェントが `apps/*/Dockerfile.lambda` と `build/zip/*.zip`（実ビルド済みの本物）を作成し終えたのを確認できたので、context 上書きなし（デフォルトパス）で `npx cdkd synth` を再実行し、実際の `docker build`（gcc インストール、bundle install、bootsnap precompile、RDS CA バンドル取得、assets:precompile を含む）と実際の zip アセットの読み込みを通して 2 スタックとも正常に synth できることまで確認済み。`RlwaAppStack` の 7 Lambda 関数の環境変数・タイムアウト・メモリ・SnapStart 設定・Layer・6 本の Function URL / Alias / Version / 12 個の resource-based Permission を生成テンプレート JSON から直接確認した。
