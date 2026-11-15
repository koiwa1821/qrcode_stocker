# QRコードストッカー MVP

スマートフォンのブラウザでQRコードを読み取り、内容・読み取り日時・任意のメモをIndexedDBとサーバーに保存するAzure Functionsアプリです。読み取り結果はHTMLとして扱わず、URLも自動で開きません。クラウドへのデプロイと実課金は行いません。

## 実装範囲

- `docs/`: Bootstrap 5 とVanilla JavaScriptのモバイル向けUI。ブラウザ標準の`BarcodeDetector`と同梱の`jsQR`を使い、カメラ・画像からQRを読み取ります。
- `api/`: Azure Functions Python v2とサービス/API実装。ルートFunction Appはリポジトリ直下の既存構成を維持し、`function_app.py`から`api.function_app.app`を読み込みます。
- 保存: 開発時はSQLite、`AZURE_TABLE_STORAGE_CONNECTION_STRING`がある場合はAzure Table Storageを使用します。SQLiteの書き込みは`BEGIN IMMEDIATE`、Table Storageは同一アカウントPartition内のETag付きバッチで上限判定・追加・論理削除を直列化します。
- 認証: メールのワンタイムコード認証。開発モードでは画面に確認コードを表示し、SES設定がある本番構成ではAWS SESで送ります。ログインセッションはHttpOnly Cookieです。
- Paid: サーバーが署名検証したStripe webhookで契約状態を更新します。チェックアウトは`sk_test_`キーのみを受け付けます。開発環境にはローカル限定のStripeモックがあります。

削除・上限整理は論理削除です。通常の一覧・検索・件数から除外しますが、物理削除処理はありません。契約終了後に保持するデータは論理削除と別状態で、終了から1年間は再契約時に再表示できます。

## ローカル起動 (Windows PowerShell)

前提: Python 3.11、Azure Functions Core Tools v4、Node.js/npm。開発ではAzuriteや外部メール・Stripe・Azure Table Storageは不要です。

1. 依存関係をインストールします。

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   npm install
   ```

2. ターミナルAでFunctionsを起動します。

   ```powershell
   $pythonExe = (Resolve-Path '.\.venv\Scripts\python.exe').Path
   $env:languageWorkers__python__defaultExecutablePath = $pythonExe
   $env:VIRTUAL_ENV = (Resolve-Path '.\.venv').Path
$env:PATH = (Split-Path $pythonExe) + ';' + $env:PATH
   $env:APP_ENV = 'development'
   $env:QR_DATABASE_PATH = 'data\qrcode-stocker.sqlite3'
   func start --port 7071
   ```

ローカル実行では`AzureWebJobsStorage`に`UseDevelopmentStorage=true`を使い、`AZURE_TABLE_STORAGE_CONNECTION_STRING`は設定しないでください。これによりFunctionsホストのストレージもローカル向けになり、記録APIはSQLiteを使います。実Azureの接続文字列をローカル設定へ入れないでください。既存の`local.settings.json`を上書きせず、初回セットアップには`local.settings.json.example`を参考にできます。`local.settings.json`はGit管理対象外です。

3. ターミナルBで静的ページと同一オリジンAPIプロキシを起動します。

   ```powershell
   $env:QR_FUNCTIONS_URL = 'http://127.0.0.1:7071'
   .\.venv\Scripts\python.exe -m api.dev_server --port 8000
   ```

4. `http://localhost:8000`を開きます。localhostはブラウザの安全なコンテキストとして扱われ、カメラを試せます。カメラが拒否・非対応の場合は「画像を選択」を使ってください。LAN上のスマートフォンからカメラを試すにはHTTPSの開発用リバースプロキシが必要です。

メール登録は開発用確認コードを画面に表示します。開発用Paidボタンはローカルサーバーでのみ有効です。Functionsを停止しても`data\qrcode-stocker.sqlite3`は残ります。

## 自動テスト

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Stripe、SES、Azure Storageへ接続せず、SQLiteの一時DBでプラン別上限、同時保存、論理削除、登録引き継ぎ、Paid終了・再契約の境界を検証します。

## 外部サービス設定 (任意)

- メール: `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_REGION`、`SENDER_EMAIL_ADDRESS`、任意の`SES_CONFIGURATION_SET_NAME`
- Stripeテストモード: `STRIPE_SECRET_KEY` (`sk_test_`のみ)、`STRIPE_PRICE_ID`、`STRIPE_SUCCESS_URL`、`STRIPE_CANCEL_URL`、`STRIPE_WEBHOOK_SECRET`
- Stripe secret名を使う運用向け予約設定: `STRIPE_API_SECRET_NAME`、`STRIPE_WEBHOOK_SECRET_NAME`
- Azure Table Storage: `AZURE_TABLE_STORAGE_CONNECTION_STRING`

値は環境変数またはAzureのアプリ設定に置き、秘密値をソースやコミットに含めないでください。`STRIPE_WEBHOOK_SECRET`は署名検証に必須です。Stripe webhookの受信APIだけはStripe署名付きのraw bodyを使うため、画面API共通の`idx`/`sid`/`n`フォームではなく、Stripeの仕様通りに検証します。Stripe CLI等から転送する場合のURLは`/api/v1/StripeWebhook`です。

フロントエンドの画面APIはすべて相対パス`/api/v1/{FunctionName}`を利用します。ローカルでは`api.dev_server`が同一オリジンのプロキシを行います。実際にホストする場合もFront DoorまたはIIS等で同じパスをFunctionsへリバースプロキシし、FunctionsホストURLをブラウザへ公開しないでください。デプロイ設定・クラウドリソースはこのMVPでは作成していません。

## データの扱いと初期仮定

- 未登録ブラウザは100件、メール認証済みアカウントは500件/2ブラウザ、Paidは10,000件/100ブラウザです。上限時は新規記録を受け付け、読み取り日時昇順・同時刻は記録ID昇順で最古を論理削除します。
- 認証ブラウザ数が上限なら新しい認証を拒否し、既存ブラウザは自動で解除しません。セッションは30日で失効し、明示ログアウトでそのブラウザを認証数から外します。
- 利用者削除も論理削除です。物理削除・保持期限後の自動消去は行いません。
- Paid終了時はPaid期間中の有効記録を1年間の保持状態に移して通常一覧から隠し、終了後の新規無料記録を最大500件で管理します。再契約で保持期限内の記録を戻し、合計が10,000件を超える場合は古いPaid期間記録を先に論理削除します。
- Excelは`.xlsx`、CSVはUTF-8 BOM付きです。両形式とも表計算ソフトの数式として解釈される文字列をエスケープします。
- IndexedDBのブラウザIDはトランザクション内でUUID v4を作成します。オフライン時はIndexedDBに同期待ちとして保持し、サーバー応答を受けるまでサーバー保存済みとは表示しません。
- 初回ロード時のグローバルIP取得先は`api64.ipify.org`です。取得できない場合はエラーを表示し、IndexedDBへ成功扱いで保存しません。QR内容はIP取得サービスへ送信しません。
