# Hype Clipper

Twitchライブを配信終了まで監視し、チャットが盛り上がった上位10区間を、それぞれきっかけの発言から60秒ずつ映像付きで切り抜きます。
映像は480pを優先して収録し、最終動画も480pで保存します。
映像全編は保存せず、5秒セグメントのローリングバッファとして既定では直近約93秒だけ保持します。チャット盛り上がり候補の映像だけを一時退避します。

書き起こしは行いません。軽量な音声区間検出（VAD）で発言の開始・終了時刻だけを記録し、既定では2.5秒以内で続く音声を同じ発言としてまとめます。切り抜きは、きっかけと推定した発言の1つ前の発言開始より3秒前から始めます。前の発言が20秒より離れている場合は、きっかけの発言開始より3秒前から始めます。

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

表示された `Twitch配信者ID` に、見たい配信者のIDを入力します。ブラウザでTwitch認証すると収集が始まり、配信終了を検知すると自動終了します。

配信者IDをコマンドに直接指定することもできます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706
```

途中で終了する場合は `Ctrl+C`。

配信終了を待たず指定時間で止める場合は、分単位で指定できます。

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel indegnasen0706 --duration-minutes 15
```

`--duration-minutes 0`を指定した場合も、時間指定を省略した場合と同じく配信終了まで継続します。

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

収集中は1分ごとに暫定上位10件と再生用の仮動画を更新します。ターミナルに表示される `live preview` のURLを開くと、ページを再読み込みせずカード部分だけが自動更新されます。
同じ候補の順位が変わっただけなら仮動画を再利用し、暫定上位10件から外れた候補の仮動画は削除します。

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

`reactions.html` では、重複しないチャット盛り上がり上位3件を再生できます。

`speech_segments.jsonl`には発言内容ではなく、VADが検出した開始・終了時刻だけを保存します。個別発言は画面に表示しません。

### 反応時間を狭める

```powershell
python twitch_reaction_probe.py --reaction-start 2 --reaction-end 6
```

最初は意味判定や感情分類は入れず、「時間だけで発言→反応がどこまで成立するか」を見るための版です。
