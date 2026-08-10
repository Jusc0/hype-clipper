# Hype Clipper

Twitch配信の`audio_only`とチャットをリアルタイム監視し、VADとチャット密度から盛り上がり上位10件を更新します。映像は常時受信・録画せず、対応するTwitch VODが対象地点まで公開された時点で30秒動画を生成します。

現在の監視上限は3配信者です。ランキングと動画は配信者ごとに分離され、PCとスマートフォンのWeb UIから切り替えて閲覧できます。YouTubeアップロード機能はありません。

## 処理方式

リアルタイム処理は次の流れです。

1. Streamlinkで`audio_only`とTwitch Chatを受信
2. FFmpegでモノラル16kHz PCMの8秒WAVチャンクへ変換
3. VADで発話区間だけを記録（書き起こしは行わない）
4. 処理済みWAVチャンクを削除
5. 従来のVAD・チャット密度・発話位置ロジックで候補を採点
6. 配信開始からの`offset_seconds`を`highlights.json`へ保存し、動画を待たずランキングを更新
7. Twitch APIで`stream_id`が一致するアーカイブVODを確認
8. VODが対象区間まで伸びたら、StreamlinkのHLS開始位置・区間長指定で必要なセグメントだけを取得
9. 720pを優先して30秒MP4を生成し、Web UIへ反映

8秒チャンクは、次のチャンクが作られてからVADへ渡します。そのため通常時にディスクへ存在する音声は、処理対象と受信中を合わせて約16秒です。VAD処理が終わったチャンクは直ちに削除し、後の動画生成目的では保存しません。

VOD取得時は先頭から読み進めません。クリップ開始位置の12秒前から約42秒分を指定し、HLSセグメント境界へ丸められた必要範囲だけを取得します。VOD全体はダウンロードしません。

## クリップ開始位置

30秒の評価窓ごとに、盛り上がり開始付近の発話をVAD結果から探します。20秒以内に1つ前の発話があればその開始位置を採用し、さらに既定の5秒プリロールを引きます。この従来ロジックで決まった開始位置へ、収集開始時点の配信経過時間を足した値が`offset_seconds`です。

既定値:

- クリップ: 30秒
- プリロール: 5秒
- 1つ前の発話を探す範囲: 20秒
- 発話を結合する無音間隔: 2.5秒
- VAD音声チャンク: 8秒
- VOD確認間隔: 60秒
- 配信終了後のVOD最終確認: 最大15分
- ランキング: 上位10件
- 同時監視: 最大3配信者

## 動画ステータス

ランキング候補には次の状態があります。

- `waiting_vod`: VODの出現または対象区間の反映待ち
- `generating`: 対象HLS区間から動画を生成中
- `ready`: Web UIで再生可能
- `unavailable`: 配信終了後も利用できるVODがない
- `failed`: 生成エラー。配信中はポーリング時に再試行

候補が上位10件から外れると、その候補用に生成済みの動画も削除します。順位が変わっただけなら同じ候補の動画は作り直しません。

## ローカル実行

必要なもの:

- Python 3.10以上
- FFmpeg / ffprobe
- TwitchアプリのClient IDとClient Secret

```powershell
pip install -r requirements.txt
Copy-Item config.example.json config.local.json
```

Twitch Developer ConsoleのOAuth Redirect URLへ次を登録します。

```text
http://localhost:3000
```

配信終了まで監視する基本コマンド:

```powershell
.\.venv\Scripts\python.exe -u twitch_auth.py --run-probe --channel yaritaiji --duration-minutes 0 --highlight-seconds 30 --preroll-seconds 5 --utterance-gap-seconds 2.5 --previous-lookback-seconds 20
```

`--duration-minutes 0`または時間指定なしは配信終了まで、正の値を指定するとその分数で終了します。

主な出力:

```text
reaction_session/
  chat.jsonl
  speech_segments.jsonl
  highlights.json
  reactions.html
  preview_vod_<candidate-id>.mp4
  audio_chunks/                  # 実行中だけ。処理済みから削除
```

`highlights.json`には配信者ID、`stream_id`、配信開始時刻、`offset_seconds`、スコア、順位、動画状態、動画パス、VOD ID・URL・照合方法などを保存します。

## VPS（Docker Compose + Flask + Caddy）

Flaskが監視画面と動画を配信し、CaddyがHTTPSとBasic認証を担当します。workerが最大3配信者の`audio_only`・チャット・VAD・ランキング・VOD動画生成を担当します。

```bash
cp .env.example .env
chmod 600 .env
docker compose up -d --build web caddy
docker compose up -d --build worker
docker compose logs -f worker
```

主な環境変数:

```dotenv
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL=yaritaiji
HIGHLIGHT_SECONDS=30
SEGMENT_SECONDS=8
VOD_POLL_SECONDS=60
VOD_READY_MARGIN_SECONDS=10
VOD_FINALIZE_MINUTES=15
MAX_CHANNELS=3
```

Twitchユーザートークンは5分ごとに期限を確認し、残り15分以下なら共有トークンファイルへ更新します。各VOD確認は共有ファイルから最新トークンを読みます。

状態確認:

```bash
docker compose ps
docker compose logs --tail=100 worker
```

## テスト

```bash
python -m unittest tests.test_vps
```

実配信を使う一時スモークテスト:

```bash
python -m tests.audio_smoke rtainjapan
python -m tests.vod_smoke --generate rtainjapan
```

スモークテストの音声・動画は一時ディレクトリにのみ作成し、終了時に破棄します。
