# QRコードストッカー 開発ルール

このファイルのルールはリポジトリ全体に適用する。既存の動作を保ち、要件に必要な範囲で変更する。

## フロントエンド

- フロントエンドは Bootstrap 5 と Vanilla JavaScript で構築し、ファイルは `docs/` 配下に置く。
- Web サイトルートを `docs/` とし、アセットは `/js/`、`/css/`、`/img/` のルート相対パスで参照する。
- HTML から読み込む JavaScript は `<script type="module" src="/js/main.js"></script>` だけにする。`docs/js/main.js` は `docs/js/app.js` から `initialize()` を import して実行するエントリーポイントとし、アプリケーションの処理はすべて `docs/js/app.js` に記述する。
- JavaScript の関数、コールバック、イベントハンドラーはアロー関数で記述する。
- CSS は HTML に埋め込まず、CSS ファイルは `docs/css/` に置く。原則としてページごとの CSS は 1 ファイルにまとめる。
- 画像は `docs/img/` に置く。ロゴには `/img/logo.svg` を使う。
- すべての HTML に `/img/favicon.svg` と、`docs/img/` に置いた iPhone 用 PNG を使う favicon / Apple touch icon を設定する。
- すべての HTML に `id="em1"` の form を置く。
- すべての button に、その HTML 内で重複しない `id` を付ける。クリック処理は `app.js` で登録し、押された button の id を `b_chk()` に渡して、個別の処理は `b_chk()` 内で分岐する。インラインの `onclick` は使わない。アロー関数では `this` を使わず、`event.currentTarget.id` で押された button の id を取得する。
- 複数タブ間で共有する永続データは IndexedDB に保存し、タブごとの一時データなど必要な場合に限って `sessionStorage` を使う。
- ページ読み込み時に IndexedDB の `sid` を確認する。値がない、または UUID v4 形式でない場合は UUID v4 を生成して保存する。複数タブが同時に初期化しても同じ `sid` を使えるよう、確認と作成は IndexedDB のトランザクション内で行う。
- ページ読み込み時にグローバル IP アドレスを取得し、IndexedDB の `sip` に保存する。取得に失敗した場合は成功扱いにせず、エラーとして処理する。
- Functions にリクエストする場合は、`id="em1"` の form data に次の値を設定して POST する。
  - `idx`: リクエストごとに生成する UUID v4
  - `sid`: IndexedDB に保存した `sid`
  - `n`: リクエストを発生させた button の id
- Functions へのリクエストは同一ドメインの `/api/` 配下に送る。Functions のホスト URL をフロントエンドから直接呼び出さない。
- Functions の応答は成功・失敗を問わず JSON とする。フロントエンドは JSON を解析して後続処理を行い、通信エラーや JSON 解析エラーも明示的に扱う。
- `/api/` へのリクエストは Front Door または IIS から Azure Functions へリバースプロキシする。

## バックエンド

- バックエンドのコードとプロジェクトファイルはリポジトリ直下の `api/` に置く。
- ローカル開発では Python 3.11、Azure Functions では Python 3.14 を使用し、両環境で同じ動作になるようにする。
- Azure Functions Python Programming Model V2 の `FunctionApp` を使用する。
- トリガーは HTTP trigger のみとする。Timer、Queue など他のトリガーは追加しない。

### API URL Rules

- すべての HTTP Trigger Function は `/api/v1/{function-name}` 形式の URL を使用する。`function-name` は Azure Functions の関数名と同じにする。
  - `CreateQr` → `/api/v1/CreateQr`
  - `GetQr` → `/api/v1/GetQr`
  - `DeleteQr` → `/api/v1/DeleteQr`
- 新しい HTTP Trigger Function を作成する場合は、route を `"v1/{function-name}"` として設定する。
- Azure Functions の既定ルート `/api/{function-name}` は使用しない。

- Azure Table Storage の接続文字列は `os.getenv('AZURE_TABLE_STORAGE_CONNECTION_STRING')` で取得する。
- メール送信には AWS SES を使い、環境変数 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`SES_CONFIGURATION_SET_NAME` を使用する。送信元アドレスは `SENDER_EMAIL_ADDRESS` から取得する。
- 決済には Stripe を使い、次の環境変数を使用する。
  - `STRIPE_API_SECRET_NAME`
  - `STRIPE_SECRET_KEY`
  - `STRIPE_PRICE_ID`
  - `STRIPE_SUCCESS_URL`
  - `STRIPE_WEBHOOK_SECRET`
  - `STRIPE_WEBHOOK_SECRET_NAME`
  - `STRIPE_CANCEL_URL`
- シークレットや認証情報をソースコードに直書きしたり、リポジトリにコミットしたりしない。
