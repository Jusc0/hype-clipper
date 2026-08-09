# Hype Clipper

Twitchライブを配信終了まで監視し、チャットが盛り上がった上位10区間を、それぞれきっかけの発言から30秒ずつ映像付きで切り抜きます。
映像は720pを優先して収録し、最終動画も720pで保存します。
映像全編は保存せず、5秒セグメントのローリングバッファとして既定では直近約65秒だけ保持します。チャット盛り上がり候補の映像だけを一時退避します。

書き起こしは行いません。軽量な音声区間検出（VAD）で発言の開始・終了時刻だけを記録し、既定では2.5秒以内で続く音声を同じ発言としてまとめます。切り抜きは、きっかけと推定した発言の1つ前の発言開始より5秒前から始めます。前の発言が20秒より離れている場合は、きっかけの発言開始より5秒前から始めます。

## 必要なもの

- Python 3.10+
- ffmpeg
- TwitchアプリのClient IDとClient Secret

```powershell
pip install -r requirements.txt
```

`ffmpeg -version` が通ることも確認してください。

## Twitch認証設定

Twitch Developer Consoleでアプリを作成し、OAuth Redirect URLへ次を登録します。

```text
http://localhost:3000
```

`config.example.json`を`config.local.json`へコピーし、認証情報を記入します。`config.local.json`はGitの対象外です。

```powershell
Copy-Item config.example.json config.local.json
notepad config.local.json
```

```json
{
  "twitch_client_id": "your-client-id",
  "twitch_client_secret": "your-client-secret"
}
```

環境変数を使う場合は次のように設定できます。環境変数が`config.local.json`より優先されます。

```powershell
$env:TWITCH_CLIENT_ID="your-client-id"
$env:TWITCH_CLIENT_SECRET="your-client-secret"
```

## ターミナルから実行

PowerShell:

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe
```

引数を省略した場合は、`yaritaiji`を配信終了まで収集し、30秒の切り抜きを発言開始の5秒前から作成します。発言の結合間隔は2.5秒、1つ前の発言を探す範囲は20秒です。

これは次の指定と同じです。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel yaritaiji --duration-minutes 0 --highlight-seconds 30 --preroll-seconds 5 --utterance-gap-seconds 2.5 --previous-lookback-seconds 20
```

ブラウザでTwitch認証すると収集が始まり、配信終了を検知すると自動終了します。

配信者IDをコマンドに直接指定することもできます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706
```

途中で終了する場合は `Ctrl+C`。

配信終了を待たず指定時間で止める場合は、分単位で指定できます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706 --duration-minutes 15
```

`--duration-minutes 0`を指定した場合も、デフォルトと同じく配信終了まで継続します。

切り抜き時間を変える場合は、秒単位で指定できます。例えば45秒にする場合:

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706 --highlight-seconds 45
```

開始余白や発言間隔も秒単位で指定できます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706 --preroll-seconds 3 --utterance-gap-seconds 2.5 --previous-lookback-seconds 20
```

- `--preroll-seconds`: 選んだ発言開始より何秒前から切り抜くか
- `--utterance-gap-seconds`: 何秒以内で続く音声を同じ発言にまとめるか
- `--previous-lookback-seconds`: 1つ前の発言を採用する最大間隔

ローリングバッファは、切り抜き時間・開始余白・発言間隔に合わせて自動調整されます。

## 配信中の暫定ハイライト

収集中は1分ごとに暫定上位10件と再生用の仮動画を更新します。ターミナルに表示される `PC preview` または `phone preview` のURLを開くと、ページを再読み込みせずカード部分だけが自動更新されます。
同じ候補の順位が変わっただけなら仮動画を再利用し、暫定上位10件から外れた候補の仮動画は削除します。

PCとスマホを同じWi-Fiへ接続すると、ターミナルに表示される `phone preview` のURLをスマホで開けます。Windowsファイアウォールの確認が表示された場合は、プライベートネットワークでのPython通信を許可してください。プレビューはスクリプト実行中だけ同じLAN内へ公開されます。

収集を停止したあと、保存済み結果だけをPCやスマホで見る場合:

```powershell
.\.venv\Scripts\python.exe -u serve_preview.py
```

表示された `phone preview` をスマホで開き、終了するときは `Ctrl+C` を押します。

更新間隔と表示件数も変更できます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706 --preview-interval-minutes 1 --top-count 10
```

配信終了後は、正確な開始位置で変換した確定動画へ置き換わります。処理済みのVAD音声チャンクは順次削除されるため、長時間配信でも音声ファイルが増え続けません。

出力:

```text
reaction_session/
  chat.jsonl
  speech_segments.jsonl
  reactions.html
  highlight_chat_1.mp4
  highlight_chat_2.mp4
  ...
  highlight_chat_10.mp4
  audio_chunks/
```

`reactions.html` では、重複しないチャット盛り上がり上位10件を再生できます。

`speech_segments.jsonl`には発言内容ではなく、VADが検出した開始・終了時刻だけを保存します。個別発言は画面に表示しません。

### 反応時間を狭める

```powershell
python twitch_reaction_probe.py --reaction-start 2 --reaction-end 6
```

最初は意味判定や感情分類は入れず、「時間だけで発言→反応がどこまで成立するか」を見るための版です。

## VPS版（Docker Compose + Flask + Caddy）

VPS版では、Twitchの収集・VAD・切り抜きをworkerコンテナで行い、Flaskが結果を配信し、CaddyがHTTPSとパスワード保護を担当します。PCとスマホはブラウザーで見るだけです。

このVPSでは、独自ドメインがなくても次のホスト名を使用できます。`sslip.io`のDNSによって`163.44.122.195`へ解決されます。

```text
https://hype.163-44-122-195.sslip.io/
```

### 初期設定

VPSで設定ファイルを作ります。

```bash
cp .env.example .env
chmod 600 .env
```

Caddy用の閲覧パスワードをハッシュ化します。

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '任意の強いパスワード'
```

出力されたハッシュを`.env`の`HYPE_BASIC_AUTH_HASH`へ、シングルクォートを付けて保存します。続けてTwitchのClient ID、Client Secret、配信者IDを設定します。

```dotenv
HYPE_BASIC_AUTH_USER=hype
HYPE_BASIC_AUTH_HASH='$2a$...'
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL=yaritaiji
```

### 起動

まずWeb画面を起動します。

```bash
docker compose up -d --build web caddy
```

workerを起動し、初回だけTwitchのデバイス認証を行います。

```bash
docker compose up -d --build worker
docker compose logs -f worker
```

ログに表示されるTwitch認証URLとコードをPCまたはスマホで開きます。認証後のアクセストークンとリフレッシュトークンはDockerボリュームへ保存され、Gitや閲覧用コンテナには渡されません。

配信終了時にworkerは正常停止します。次の配信で再び開始する場合は次を実行します。

```bash
docker compose start worker
```

設定やコードを変更した場合は、`start`ではなく次を使用します。

```bash
docker compose up -d --build worker
```

状態確認:

```bash
docker compose ps
docker compose logs --tail=100 worker
```

`docker compose down`では動画・認証・Caddy証明書の名前付きボリュームは保持されます。`docker compose down -v`は保存データを削除するため使用しないでください。

### 1GB VPS向け設定

`compose.yaml`ではworker 560MB、Flask 112MB、Caddy 96MBのメモリ上限を設定しています。書き起こしは行わず、Gunicornは1 worker・2 threads、FFmpeg変換は既存処理のとおり直列実行です。ホスト側には2GB程度のswapを用意してください。
