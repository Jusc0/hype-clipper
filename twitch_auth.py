import argparse
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime

import requests
import secrets

REDIRECT_URI = "http://localhost:3000"

SCOPES = [
    "chat:read",
]

STATE = secrets.token_urlsafe(24)

result = {}


def load_twitch_credentials():
    config_path = Path(__file__).with_name("config.local.json")
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"config.local.json を読み込めません: {exc}") from exc
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id:
        client_id = str(config.get("twitch_client_id", "")).strip()
    if not client_secret:
        client_secret = str(config.get("twitch_client_secret", "")).strip()
    return client_id, client_secret

parser = argparse.ArgumentParser(description="Authorize Twitch chat access")
parser.add_argument(
    "--run-probe",
    action="store_true",
    help="run twitch_reaction_probe.py immediately without printing the tokens",
)
parser.add_argument(
    "--channel",
    default="yaritaiji",
    help="Twitch streamer ID (default: yaritaiji)",
)
parser.add_argument(
    "--duration-minutes",
    type=float,
    default=0.0,
    help="stop after this many minutes; use 0 to run until the stream ends (default: 0)",
)
parser.add_argument(
    "--highlight-seconds",
    type=float,
    default=30.0,
    help="highlight duration in seconds (default: 30)",
)
parser.add_argument(
    "--preroll-seconds",
    type=float,
    default=5.0,
    help="seconds to include before the selected utterance (default: 5)",
)
parser.add_argument(
    "--utterance-gap-seconds",
    type=float,
    default=2.5,
    help="merge speech segments separated by this many seconds (default: 2.5)",
)
parser.add_argument(
    "--previous-lookback-seconds",
    type=float,
    default=20.0,
    help="maximum distance to use the previous utterance (default: 20)",
)
parser.add_argument(
    "--top-count",
    type=int,
    default=10,
    help="number of highlights to keep and display (default: 10)",
)
parser.add_argument(
    "--preview-interval-minutes",
    type=float,
    default=1.0,
    help="live HTML update interval in minutes (default: 1)",
)
args = parser.parse_args()

CLIENT_ID, CLIENT_SECRET = load_twitch_credentials()

if not CLIENT_ID or not CLIENT_SECRET:
    parser.error(
        "TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET または config.local.json が必要です"
    )

if args.duration_minutes is not None and args.duration_minutes < 0:
    parser.error("--duration-minutes must be 0 or greater")
if args.duration_minutes == 0:
    args.duration_minutes = None
if args.highlight_seconds <= 0:
    parser.error("--highlight-seconds must be greater than 0")
if (args.duration_minutes is not None
        and args.highlight_seconds > args.duration_minutes * 60):
    parser.error("--highlight-seconds cannot exceed the recording duration")
if args.preroll_seconds < 0:
    parser.error("--preroll-seconds must be 0 or greater")
if args.utterance_gap_seconds < 0:
    parser.error("--utterance-gap-seconds must be 0 or greater")
if args.previous_lookback_seconds < 0:
    parser.error("--previous-lookback-seconds must be 0 or greater")
if args.top_count <= 0:
    parser.error("--top-count must be greater than 0")
if args.preview_interval_minutes <= 0:
    parser.error("--preview-interval-minutes must be greater than 0")
if args.run_probe and not args.channel:
    try:
        args.channel = input("Twitch配信者IDを入力してください: ").strip()
    except EOFError:
        parser.error("--channel または配信者IDの入力が必要です")
    if not args.channel:
        parser.error("配信者IDを入力してください")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if params.get("state", [""])[0] != STATE:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid state")
            return

        if "error" in params:
            result["error"] = params
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization denied")
            return

        code = params.get("code", [None])[0]

        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No authorization code")
            return

        result["code"] = code

        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            "Twitch authorization complete. You can close this page.".encode()
        )

    def log_message(self, format, *args):
        pass


def get_access_token(code):
    response = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def get_stream_started_at_epoch(channel, access_token):
    try:
        response = requests.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": channel},
            headers={
                "Client-Id": CLIENT_ID,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=20,
        )
        response.raise_for_status()
        streams = response.json().get("data", [])
        if not streams:
            return 0.0
        return datetime.fromisoformat(
            streams[0]["started_at"].replace("Z", "+00:00")
        ).timestamp()
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        print(f"[stream] 配信開始時刻を取得できませんでした: {exc}")
        return 0.0


scope_string = " ".join(SCOPES)

params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": scope_string,
    "state": STATE,
}

auth_url = (
    "https://id.twitch.tv/oauth2/authorize?"
    + urllib.parse.urlencode(params)
)

print()
print("このURLをブラウザで開いてTwitch認証してください:")
print()
print(auth_url)
print()

try:
    webbrowser.open(auth_url)
except Exception:
    pass


with socketserver.TCPServer(("127.0.0.1", 3000), Handler) as server:
    while "code" not in result and "error" not in result:
        server.handle_request()


if "error" in result:
    print("認証失敗:")
    print(result["error"])
    raise SystemExit


tokens = get_access_token(result["code"])

if args.run_probe:
    access_token = tokens["access_token"]
    validation = requests.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {access_token}"},
        timeout=20,
    )
    validation.raise_for_status()

    env = os.environ.copy()
    env["TWITCH_NICK"] = validation.json()["login"]
    env["TWITCH_OAUTH_TOKEN"] = access_token
    stream_started_at_epoch = get_stream_started_at_epoch(
        args.channel, access_token
    )
    probe = Path(__file__).with_name("twitch_reaction_probe.py")
    if args.duration_minutes is None:
        print(f"\n認証完了。{args.channel} を配信終了まで収集します。\n")
    else:
        print(f"\n認証完了。{args.channel} を {args.duration_minutes:g}分間収集します。\n")
    command = [
        sys.executable,
        "-u",
        str(probe),
        "--channel",
        args.channel,
        "--highlight-seconds",
        str(args.highlight_seconds),
        "--preroll-seconds",
        str(args.preroll_seconds),
        "--utterance-gap-seconds",
        str(args.utterance_gap_seconds),
        "--previous-lookback-seconds",
        str(args.previous_lookback_seconds),
        "--top-count",
        str(args.top_count),
        "--preview-interval-minutes",
        str(args.preview_interval_minutes),
        "--stream-started-at-epoch",
        str(stream_started_at_epoch),
    ]
    if args.duration_minutes is not None:
        command.extend(["--duration-minutes", str(args.duration_minutes)])
    raise SystemExit(subprocess.call(command, env=env))

print()
print("=== ACCESS TOKEN ===")
print(tokens["access_token"])

print()
print("=== REFRESH TOKEN ===")
print(tokens.get("refresh_token"))

print()
print("=== SCOPES ===")
print(tokens.get("scope"))

print()
print("expires_in:", tokens.get("expires_in"))
