from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import secrets
import socketserver
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime

import requests


REDIRECT_URI = "http://localhost:3000"

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

SCOPES = [
    "chat:read",
]

STATE = secrets.token_urlsafe(24)

result: dict = {}


def get_token_file() -> Path:
    """
    TWITCH_TOKEN_FILE が指定されていればそこを使用。
    未指定なら twitch_auth.py と同じ場所の
    twitch_tokens.json を使用する。
    """

    configured = os.environ.get(
        "TWITCH_TOKEN_FILE",
        "",
    ).strip()

    if configured:
        return Path(configured)

    return Path(__file__).with_name(
        "twitch_tokens.json"
    )


TOKEN_FILE = get_token_file()


def load_twitch_credentials() -> tuple[str, str]:
    config_path = Path(__file__).with_name(
        "config.local.json"
    )

    config = {}

    if config_path.exists():
        try:
            config = json.loads(
                config_path.read_text(
                    encoding="utf-8"
                )
            )

        except (
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise SystemExit(
                f"config.local.json を読み込めません: {exc}"
            ) from exc

    client_id = os.environ.get(
        "TWITCH_CLIENT_ID",
        "",
    ).strip()

    client_secret = os.environ.get(
        "TWITCH_CLIENT_SECRET",
        "",
    ).strip()

    if not client_id:
        client_id = str(
            config.get(
                "twitch_client_id",
                "",
            )
        ).strip()

    if not client_secret:
        client_secret = str(
            config.get(
                "twitch_client_secret",
                "",
            )
        ).strip()

    return (
        client_id,
        client_secret,
    )


def save_tokens(
    tokens: dict,
) -> None:
    """
    Twitchから取得したtokenを
    twitch_tokens.jsonへ安全に保存する。
    """

    stored = dict(tokens)

    stored["saved_at"] = int(
        time.time()
    )

    TOKEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = TOKEN_FILE.with_suffix(
        TOKEN_FILE.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            stored,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary.replace(
        TOKEN_FILE
    )

    print(
        f"[auth] token saved: "
        f"{TOKEN_FILE.resolve()}",
        flush=True,
    )


def validate_access_token(
    access_token: str,
) -> dict:
    """
    Twitch access tokenをvalidateし、
    login/client_id/scopes等を取得する。
    """

    response = requests.get(
        VALIDATE_URL,
        headers={
            "Authorization": (
                f"OAuth {access_token}"
            )
        },
        timeout=20,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Twitchから無効なaccess tokenが返されました"
        )

    response.raise_for_status()

    validation = response.json()

    returned_client_id = str(
        validation.get(
            "client_id",
            "",
        )
    ).strip()

    if (
        returned_client_id
        and returned_client_id != CLIENT_ID
    ):
        raise RuntimeError(
            "取得したtokenのClient IDが一致しません"
        )

    return validation


parser = argparse.ArgumentParser(
    description="Authorize Twitch chat access"
)

parser.add_argument(
    "--run-probe",
    action="store_true",
    help=(
        "認証後すぐに twitch_reaction_probe.py を起動する"
    ),
)

parser.add_argument(
    "--channel",
    default="yaritaiji",
    help=(
        "Twitch streamer ID "
        "(default: yaritaiji)"
    ),
)

parser.add_argument(
    "--duration-minutes",
    type=float,
    default=0.0,
    help=(
        "録画時間。0なら配信終了まで実行 "
        "(default: 0)"
    ),
)

parser.add_argument(
    "--highlight-seconds",
    type=float,
    default=30.0,
    help=(
        "highlight duration in seconds "
        "(default: 30)"
    ),
)

parser.add_argument(
    "--clip-margin-seconds",
    type=float,
    default=1.0,
    help=(
        "seconds before and after the clip speech "
        "(default: 1)"
    ),
)

parser.add_argument(
    "--utterance-gap-seconds",
    type=float,
    default=4.0,
    help=(
        "merge speech segments separated by "
        "this many seconds (default: 4)"
    ),
)

parser.add_argument(
    "--top-count",
    type=int,
    default=10,
    help=(
        "number of highlights to keep "
        "(default: 10)"
    ),
)

parser.add_argument(
    "--preview-interval-minutes",
    type=float,
    default=1.0,
    help=(
        "live HTML update interval "
        "(default: 1)"
    ),
)

args = parser.parse_args()


CLIENT_ID, CLIENT_SECRET = (
    load_twitch_credentials()
)


if not CLIENT_ID or not CLIENT_SECRET:
    parser.error(
        "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET "
        "または config.local.json の設定が必要です"
    )


if args.duration_minutes < 0:
    parser.error(
        "--duration-minutes must be 0 or greater"
    )

if args.duration_minutes == 0:
    args.duration_minutes = None


if args.highlight_seconds <= 0:
    parser.error(
        "--highlight-seconds must be greater than 0"
    )


if (
    args.duration_minutes is not None
    and args.highlight_seconds
    > args.duration_minutes * 60
):
    parser.error(
        "--highlight-seconds cannot exceed "
        "the recording duration"
    )


if args.clip_margin_seconds < 0:
    parser.error(
        "--clip-margin-seconds must be 0 or greater"
    )


if args.utterance_gap_seconds < 0:
    parser.error(
        "--utterance-gap-seconds must be 0 or greater"
    )




if args.top_count <= 0:
    parser.error(
        "--top-count must be greater than 0"
    )


if args.preview_interval_minutes <= 0:
    parser.error(
        "--preview-interval-minutes "
        "must be greater than 0"
    )


if args.run_probe and not args.channel:
    try:
        args.channel = input(
            "Twitch配信者IDを入力してください: "
        ).strip()

    except EOFError:
        parser.error(
            "--channel または配信者IDの入力が必要です"
        )

    if not args.channel:
        parser.error(
            "配信者IDを入力してください"
        )


class Handler(
    http.server.BaseHTTPRequestHandler
):

    def do_GET(
        self,
    ) -> None:

        parsed = urllib.parse.urlparse(
            self.path
        )

        params = urllib.parse.parse_qs(
            parsed.query
        )

        returned_state = params.get(
            "state",
            [""],
        )[0]

        if returned_state != STATE:

            self.send_response(
                400
            )

            self.end_headers()

            self.wfile.write(
                b"Invalid state"
            )

            return

        if "error" in params:

            result[
                "error"
            ] = params

            self.send_response(
                400
            )

            self.end_headers()

            self.wfile.write(
                b"Authorization denied"
            )

            return

        code = params.get(
            "code",
            [None],
        )[0]

        if not code:

            self.send_response(
                400
            )

            self.end_headers()

            self.wfile.write(
                b"No authorization code"
            )

            return

        result[
            "code"
        ] = code

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )

        self.end_headers()

        self.wfile.write(
            (
                "Twitch authorization complete. "
                "You can close this page."
            ).encode(
                "utf-8"
            )
        )

    def log_message(
        self,
        format,
        *args,
    ) -> None:
        pass


class ReusableTCPServer(
    socketserver.TCPServer
):
    allow_reuse_address = True


def get_access_token(
    code: str,
) -> dict:

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )

    if not response.ok:

        try:
            detail = response.json()

        except ValueError:
            detail = response.text

        raise RuntimeError(
            "Twitch token取得に失敗しました: "
            f"{response.status_code} {detail}"
        )

    tokens = response.json()

    if not tokens.get(
        "access_token"
    ):
        raise RuntimeError(
            "Twitchからaccess_tokenが返されませんでした"
        )

    return tokens


def get_stream_info(
    channel: str,
    access_token: str,
) -> dict:

    empty = {
        "stream_id": "",
        "user_id": "",
        "started_at": "",
        "started_at_epoch": 0.0,
    }

    try:

        response = requests.get(
            STREAMS_URL,
            params={
                "user_login": channel
            },
            headers={
                "Client-Id": CLIENT_ID,
                "Authorization": (
                    f"Bearer {access_token}"
                ),
            },
            timeout=20,
        )

        response.raise_for_status()

        streams = (
            response.json().get(
                "data",
                [],
            )
        )

        if not streams:
            return empty

        stream = streams[0]

        started_at = str(
            stream[
                "started_at"
            ]
        )

        return {
            "stream_id": str(
                stream[
                    "id"
                ]
            ),
            "user_id": str(
                stream[
                    "user_id"
                ]
            ),
            "started_at": started_at,
            "started_at_epoch": (
                datetime.fromisoformat(
                    started_at.replace(
                        "Z",
                        "+00:00",
                    )
                ).timestamp()
            ),
        }

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:

        print(
            f"[stream] 配信開始時刻を"
            f"取得できませんでした: {exc}",
            file=sys.stderr,
        )

        return empty


scope_string = " ".join(
    SCOPES
)

params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": scope_string,
    "state": STATE,
}

auth_url = (
    AUTHORIZE_URL
    + "?"
    + urllib.parse.urlencode(
        params
    )
)


print()
print(
    "ブラウザでTwitch認証を行ってください:"
)
print()
print(
    auth_url
)
print()


try:
    webbrowser.open(
        auth_url
    )

except Exception:
    pass


try:

    with ReusableTCPServer(
        (
            "127.0.0.1",
            3000,
        ),
        Handler,
    ) as server:

        while (
            "code" not in result
            and "error" not in result
        ):

            server.handle_request()

except OSError as exc:

    raise SystemExit(
        "localhost:3000 を開けませんでした。\n"
        "別のプログラムがポート3000を"
        f"使用していないか確認してください。\n{exc}"
    ) from exc


if "error" in result:

    print(
        "Twitch認証がキャンセルされました"
    )

    print(
        result[
            "error"
        ]
    )

    raise SystemExit(
        1
    )


tokens = get_access_token(
    result[
        "code"
    ]
)

access_token = str(
    tokens[
        "access_token"
    ]
).strip()


# Twitch側でもtokenが有効か確認
validation = validate_access_token(
    access_token
)


# ★ここが今回重要
# 新しい access_token / refresh_token を
# twitch_tokens.json に保存する
save_tokens(
    tokens
)


login = str(
    validation.get(
        "login",
        "",
    )
).strip()


scopes = validation.get(
    "scopes",
    [],
)


print()
print(
    "=== Twitch authorization complete ==="
)

print(
    f"login      : {login}"
)

print(
    f"scopes     : {', '.join(scopes)}"
)

print(
    f"expires_in : "
    f"{validation.get('expires_in', '')}"
)

print(
    f"token file : "
    f"{TOKEN_FILE.resolve()}"
)


if args.run_probe:

    env = os.environ.copy()

    env[
        "TWITCH_NICK"
    ] = login

    env[
        "TWITCH_OAUTH_TOKEN"
    ] = access_token

    env[
        "TWITCH_CLIENT_ID"
    ] = CLIENT_ID

    # probe側からも同じtokenファイルが分かるようにする
    env[
        "TWITCH_TOKEN_FILE"
    ] = str(
        TOKEN_FILE.resolve()
    )

    stream_info = get_stream_info(
        args.channel,
        access_token,
    )

    probe = Path(
        __file__
    ).with_name(
        "twitch_reaction_probe.py"
    )

    if args.duration_minutes is None:

        print()
        print(
            f"認証完了。"
            f"{args.channel} を配信終了まで取得します。"
        )
        print()

    else:

        print()
        print(
            f"認証完了。"
            f"{args.channel} を"
            f"{args.duration_minutes:g}分間取得します。"
        )
        print()

    command = [
        sys.executable,
        "-u",
        str(
            probe
        ),
        "--channel",
        args.channel,
        "--highlight-seconds",
        str(
            args.highlight_seconds
        ),
        "--clip-margin-seconds",
        str(
            args.clip_margin_seconds
        ),
        "--utterance-gap-seconds",
        str(
            args.utterance_gap_seconds
        ),
        "--top-count",
        str(
            args.top_count
        ),
        "--preview-interval-minutes",
        str(
            args.preview_interval_minutes
        ),
        "--stream-started-at-epoch",
        str(
            stream_info[
                "started_at_epoch"
            ]
        ),
    ]

    for option, value in (
        (
            "--stream-id",
            stream_info[
                "stream_id"
            ],
        ),
        (
            "--stream-user-id",
            stream_info[
                "user_id"
            ],
        ),
        (
            "--stream-started-at",
            stream_info[
                "started_at"
            ],
        ),
    ):

        if value:
            command.extend(
                [
                    option,
                    value,
                ]
            )

    if args.duration_minutes is not None:

        command.extend(
            [
                "--duration-minutes",
                str(
                    args.duration_minutes
                ),
            ]
        )

    raise SystemExit(
        subprocess.call(
            command,
            env=env,
        )
    )
