"""Headless Hype Clipper worker with persistent Twitch device authentication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = "chat:read"

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")

DATA_DIR = Path(
    os.environ.get(
        "HYPE_DATA_DIR",
        "/data",
    )
)

TOKEN_FILE = Path(
    os.environ.get(
        "TWITCH_TOKEN_FILE",
        "/auth/twitch_tokens.json",
    )
)

STATUS_FILE = (
    DATA_DIR
    / "service_status.json"
)

CHANNELS_ROOT = (
    DATA_DIR
    / "channels"
)

CONTROL_DIR = Path(
    os.environ.get(
        "HYPE_CONTROL_DIR",
        "/control",
    )
)

CHANNELS_FILE = (
    CONTROL_DIR
    / "channels.json"
)

MAX_CHANNELS = max(
    1,
    int(
        os.environ.get(
            "MAX_CHANNELS",
            "3",
        )
    ),
)

TOKEN_REFRESH_CHECK_SECONDS = 300.0
TOKEN_REFRESH_BELOW_SECONDS = 900

JST = timezone(
    timedelta(hours=9),
    "JST",
)


def now_iso_jst() -> str:
    return datetime.now(
        JST
    ).isoformat(
        timespec="seconds"
    )


def atomic_write_json(
    path: Path,
    payload: dict,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_suffix(
        path.suffix
        + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary.replace(
        path
    )

    try:
        path.chmod(
            0o600
        )

    except OSError:
        pass


def write_status(
    state: str,
    **details,
) -> None:

    atomic_write_json(
        STATUS_FILE,
        {
            "state": state,
            "updated_at": now_iso_jst(),
            **details,
        },
    )


def channel_data_dir(
    channel: str,
) -> Path:

    root = (
        CHANNELS_ROOT.resolve()
    )

    target = (
        root
        / channel
    ).resolve()

    if target.parent != root:
        raise RuntimeError(
            "unsafe channel data path"
        )

    return target


def write_channel_status(
    channel: str,
    state: str,
    **details,
) -> None:

    atomic_write_json(
        channel_data_dir(
            channel
        )
        / "service_status.json",
        {
            "state": state,
            "updated_at": now_iso_jst(),
            "channel": channel,
            **details,
        },
    )


# ---------------------------------------------------------
# Twitch authentication
# ---------------------------------------------------------

def load_tokens() -> dict:

    try:
        return json.loads(
            TOKEN_FILE.read_text(
                encoding="utf-8"
            )
        )

    except FileNotFoundError:
        return {}

    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:

        print(
            f"[auth] token file could not be read: {exc}",
            file=sys.stderr,
        )

        return {}


def save_tokens(
    tokens: dict,
) -> None:

    stored = dict(
        tokens
    )

    stored[
        "saved_at"
    ] = int(
        time.time()
    )

    atomic_write_json(
        TOKEN_FILE,
        stored,
    )


def validate_token(
    access_token: str,
    client_id: str,
) -> dict | None:

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
        return None

    response.raise_for_status()

    validation = (
        response.json()
    )

    if (
        validation.get(
            "client_id"
        )
        != client_id
    ):

        print(
            "[auth] stored token belongs to another client",
            file=sys.stderr,
        )

        return None

    if (
        SCOPES
        not in validation.get(
            "scopes",
            [],
        )
    ):

        print(
            "[auth] stored token does not include chat:read",
            file=sys.stderr,
        )

        return None

    return validation


def refresh_tokens(
    tokens: dict,
    client_id: str,
    client_secret: str,
) -> dict | None:

    refresh_token = str(
        tokens.get(
            "refresh_token",
            "",
        )
    ).strip()

    if not refresh_token:
        return None

    form = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    if client_secret:
        form[
            "client_secret"
        ] = client_secret

    response = requests.post(
        TOKEN_URL,
        data=form,
        timeout=20,
    )

    if response.status_code >= 400:

        print(
            f"[auth] token refresh failed "
            f"({response.status_code}); "
            "new device authorization is required",
            file=sys.stderr,
        )

        return None

    refreshed = (
        response.json()
    )

    if not refreshed.get(
        "refresh_token"
    ):

        refreshed[
            "refresh_token"
        ] = refresh_token

    save_tokens(
        refreshed
    )

    return refreshed


def device_authorize(
    client_id: str,
) -> dict:

    write_status(
        "waiting_for_twitch_authorization"
    )

    response = requests.post(
        DEVICE_URL,
        data={
            "client_id": client_id,
            "scopes": SCOPES,
        },
        timeout=20,
    )

    response.raise_for_status()

    device = (
        response.json()
    )

    verification_uri = (
        device[
            "verification_uri"
        ]
    )

    user_code = (
        device[
            "user_code"
        ]
    )

    print(
        "\n=== Twitch authorization required ===",
        flush=True,
    )

    print(
        f"Open: {verification_uri}",
        flush=True,
    )

    print(
        f"Code: {user_code}\n",
        flush=True,
    )

    write_status(
        "waiting_for_twitch_authorization",
        verification_uri=verification_uri,
        user_code=user_code,
    )

    interval = max(
        1,
        int(
            device.get(
                "interval",
                5,
            )
        ),
    )

    deadline = (
        time.monotonic()
        + int(
            device.get(
                "expires_in",
                1800,
            )
        )
    )

    while (
        time.monotonic()
        < deadline
    ):

        time.sleep(
            interval
        )

        token_response = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "scopes": SCOPES,
                "device_code": (
                    device[
                        "device_code"
                    ]
                ),
                "grant_type": DEVICE_GRANT,
            },
            timeout=20,
        )

        if token_response.ok:

            tokens = (
                token_response.json()
            )

            save_tokens(
                tokens
            )

            print(
                "[auth] Twitch authorization completed",
                flush=True,
            )

            return tokens

        try:

            message = str(
                token_response.json().get(
                    "message",
                    "",
                )
            ).lower()

        except (
            ValueError,
            AttributeError,
        ):
            message = ""

        if (
            "authorization_pending"
            in message
        ):
            continue

        if (
            "slow_down"
            in message
        ):

            interval += 5
            continue

        token_response.raise_for_status()

    raise RuntimeError(
        "Twitch device authorization timed out"
    )


def get_twitch_identity(
    client_id: str,
    client_secret: str,
) -> tuple[str, str]:

    tokens = load_tokens()

    access_token = str(
        tokens.get(
            "access_token",
            "",
        )
    ).strip()

    validation = None

    if access_token:

        validation = validate_token(
            access_token,
            client_id,
        )

        if (
            validation
            and int(
                validation.get(
                    "expires_in",
                    0,
                )
            )
            > 300
        ):

            return (
                validation["login"],
                access_token,
            )

    refreshed = refresh_tokens(
        tokens,
        client_id,
        client_secret,
    )

    if refreshed:

        access_token = (
            refreshed[
                "access_token"
            ]
        )

        validation = validate_token(
            access_token,
            client_id,
        )

        if validation:

            return (
                validation["login"],
                access_token,
            )

    tokens = device_authorize(
        client_id
    )

    access_token = (
        tokens[
            "access_token"
        ]
    )

    validation = validate_token(
        access_token,
        client_id,
    )

    if not validation:

        raise RuntimeError(
            "Twitch returned an invalid access token"
        )

    return (
        validation["login"],
        access_token,
    )


def refresh_running_identity(
    nick: str,
    access_token: str,
    client_id: str,
    client_secret: str,
) -> tuple[str, str]:

    """
    Refresh without entering the
    interactive device flow.
    """

    tokens = load_tokens()

    stored_access_token = str(
        tokens.get(
            "access_token",
            "",
        )
    ).strip()

    candidates = [
        stored_access_token,
        access_token,
    ]

    best_validation = None
    best_token = access_token

    for candidate in dict.fromkeys(
        token
        for token in candidates
        if token
    ):

        try:

            validation = (
                validate_token(
                    candidate,
                    client_id,
                )
            )

        except requests.RequestException as exc:

            print(
                f"[auth] periodic validation failed: {exc}",
                file=sys.stderr,
            )

            return (
                nick,
                access_token,
            )

        if not validation:
            continue

        best_validation = validation
        best_token = candidate

        if (
            int(
                validation.get(
                    "expires_in",
                    0,
                )
            )
            > TOKEN_REFRESH_BELOW_SECONDS
        ):

            return (
                str(
                    validation.get(
                        "login",
                        nick,
                    )
                ),
                candidate,
            )

    try:

        refreshed = refresh_tokens(
            tokens,
            client_id,
            client_secret,
        )

    except requests.RequestException as exc:

        print(
            f"[auth] periodic refresh failed: {exc}",
            file=sys.stderr,
        )

        refreshed = None

    if refreshed:

        refreshed_token = str(
            refreshed.get(
                "access_token",
                "",
            )
        ).strip()

        try:

            validation = (
                validate_token(
                    refreshed_token,
                    client_id,
                )
            )

        except requests.RequestException as exc:

            print(
                f"[auth] refreshed token validation failed: {exc}",
                file=sys.stderr,
            )

            validation = None

        if validation:

            print(
                "[auth] Twitch access token refreshed",
                flush=True,
            )

            return (
                str(
                    validation.get(
                        "login",
                        nick,
                    )
                ),
                refreshed_token,
            )

    if best_validation:

        return (
            str(
                best_validation.get(
                    "login",
                    nick,
                )
            ),
            best_token,
        )

    return (
        nick,
        access_token,
    )


# ---------------------------------------------------------
# Channel handling
# ---------------------------------------------------------

def normalize_channel(
    value: str,
) -> str:

    channel = (
        value
        .strip()
        .lower()
        .lstrip("#")
    )

    # Twitch URLも許可
    for prefix in (
        "https://www.twitch.tv/",
        "http://www.twitch.tv/",
        "https://twitch.tv/",
        "http://twitch.tv/",
        "www.twitch.tv/",
        "twitch.tv/",
    ):

        if channel.startswith(
            prefix
        ):

            channel = (
                channel[
                    len(prefix):
                ]
                .split(
                    "/",
                    1,
                )[0]
                .split(
                    "?",
                    1,
                )[0]
                .strip()
                .lower()
            )

            break

    if not CHANNEL_RE.fullmatch(
        channel
    ):

        raise ValueError(
            "invalid Twitch channel"
        )

    return channel


def normalized_channel() -> str:

    raw = os.environ.get(
        "TWITCH_CHANNEL",
        "",
    )

    try:

        return normalize_channel(
            raw
        )

    except ValueError as exc:

        raise RuntimeError(
            "TWITCH_CHANNEL must be a valid Twitch channel "
            "ID or Twitch channel URL"
        ) from exc


def get_stream_info(
    channel: str,
    access_token: str,
    client_id: str,
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
                "Client-Id": client_id,
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
            f"[stream:{channel}] "
            f"start time unavailable: {exc}",
            file=sys.stderr,
        )

        return empty


def get_stream_started_at_epoch(
    channel: str,
    access_token: str,
    client_id: str,
) -> float:

    return float(
        get_stream_info(
            channel,
            access_token,
            client_id,
        )[
            "started_at_epoch"
        ]
    )


def add_numeric_option(
    command: list[str],
    env_name: str,
    option: str,
) -> None:

    value = os.environ.get(
        env_name,
        "",
    ).strip()

    if value:

        command.extend(
            [
                option,
                value,
            ]
        )


def build_probe_command(
    channel: str,
    output_dir: Path | None = None,
    stream_started_at_epoch: float = 0.0,
    preserve_published: bool = False,
    stream_id: str = "",
    stream_user_id: str = "",
    stream_started_at: str = "",
    utterance_gap_seconds: float | None = None,
) -> list[str]:

    output_dir = (
        output_dir
        or DATA_DIR
    )

    command = [
        sys.executable,
        "-u",
        str(
            Path(
                __file__
            ).with_name(
                "twitch_reaction_probe.py"
            )
        ),
        "--channel",
        channel,
        "--out",
        str(
            output_dir
        ),
        "--no-preview-server",
        "--stream-started-at-epoch",
        str(
            stream_started_at_epoch
        ),
    ]

    for option, value in (
        (
            "--stream-id",
            stream_id,
        ),
        (
            "--stream-user-id",
            stream_user_id,
        ),
        (
            "--stream-started-at",
            stream_started_at,
        ),
    ):

        if value:

            command.extend(
                [
                    option,
                    value,
                ]
            )

    for env_name, option in (
        (
            "DURATION_MINUTES",
            "--duration-minutes",
        ),
        (
            "HIGHLIGHT_SECONDS",
            "--highlight-seconds",
        ),
        (
            "CLIP_MIN_SECONDS",
            "--clip-min-seconds",
        ),
        (
            "CLIP_MAX_SECONDS",
            "--clip-max-seconds",
        ),
        (
            "CLIP_MARGIN_SECONDS",
            "--clip-margin-seconds",
        ),
        (
            "UTTERANCE_GAP_SECONDS",
            "--utterance-gap-seconds",
        ),
        (
            "TOP_COUNT",
            "--top-count",
        ),
        (
            "PREVIEW_INTERVAL_MINUTES",
            "--preview-interval-minutes",
        ),
        (
            "SEGMENT_SECONDS",
            "--segment-seconds",
        ),
        (
            "VOD_POLL_SECONDS",
            "--vod-poll-seconds",
        ),
        (
            "VOD_READY_MARGIN_SECONDS",
            "--vod-ready-margin-seconds",
        ),
        (
            "VOD_FINALIZE_MINUTES",
            "--vod-finalize-minutes",
        ),
    ):

        add_numeric_option(
            command,
            env_name,
            option,
        )

    if utterance_gap_seconds is not None:
        command.extend(["--utterance-gap-seconds", f"{float(utterance_gap_seconds):g}"])

    if preserve_published:

        command.append(
            "--preserve-published-on-start"
        )

    return command


# ---------------------------------------------------------
# Control file
# ---------------------------------------------------------

def default_channels_payload(
    channel: str,
) -> dict:

    return {
        "channels": [
            {
                "channel": channel,
                "request_id": (
                    f"default-{int(time.time())}"
                ),
                "added_at": now_iso_jst(),
            }
        ]
    }


def load_desired_channels(
    default_channel: str,
) -> dict[str, str]:

    if not CHANNELS_FILE.exists():

        atomic_write_json(
            CHANNELS_FILE,
            default_channels_payload(
                default_channel
            ),
        )

    try:

        payload = json.loads(
            CHANNELS_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:

        print(
            f"[control] could not read channels: {exc}",
            file=sys.stderr,
        )

        return {}

    desired = {}

    for entry in payload.get(
        "channels",
        [],
    ):

        if len(
            desired
        ) >= MAX_CHANNELS:
            break

        if not isinstance(
            entry,
            dict,
        ):
            continue

        try:

            channel = (
                normalize_channel(
                    str(
                        entry.get(
                            "channel",
                            "",
                        )
                    )
                )
            )

        except ValueError:
            continue

        request_id = str(
            entry.get(
                "request_id",
                "",
            )
        ).strip()

        if (
            not request_id
            or channel in desired
        ):
            continue

        desired[
            channel
        ] = request_id

    return desired


def load_control_settings() -> dict:
    try:
        payload = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    settings = payload.get("settings", {})
    return settings if isinstance(settings, dict) else {}


def configured_utterance_gap() -> float | None:
    raw = load_control_settings().get("utterance_gap_seconds")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.5 <= value <= 10 else None


def configured_publish_after_idle_minutes() -> float:
    raw = load_control_settings().get("publish_after_idle_minutes", 0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 <= value <= 240 else 0.0


def load_publish_requests() -> dict[str, str]:
    try:
        payload = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    requests = {}
    for entry in payload.get("channels", []):
        if not isinstance(entry, dict):
            continue
        try:
            channel = normalize_channel(str(entry.get("channel", "")))
        except ValueError:
            continue
        request_id = str(entry.get("publish_request_id", "")).strip()
        if request_id:
            requests[channel] = request_id
    return requests


def clear_publish_request(channel: str, publish_request_id: str) -> None:
    try:
        payload = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entries = payload.get("channels", [])
    if not isinstance(entries, list):
        return
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            entry_channel = normalize_channel(str(entry.get("channel", "")))
        except ValueError:
            continue
        if (
            entry_channel == channel
            and str(entry.get("publish_request_id", "")).strip()
            == publish_request_id
        ):
            entry.pop("publish_request_id", None)
            changed = True
    if changed:
        atomic_write_json(CHANNELS_FILE, payload)


# ---------------------------------------------------------
# Process/data helpers
# ---------------------------------------------------------

def purge_channel_data(
    channel: str,
) -> None:

    target = (
        channel_data_dir(
            channel
        )
    )

    if target.exists():

        shutil.rmtree(
            target
        )

    target.mkdir(
        parents=True,
        exist_ok=True,
    )


def stop_process(
    child: subprocess.Popen,
    timeout: float = 15.0,
) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def finish_process_for_publish(
    child: subprocess.Popen,
    timeout: float = 30.0,
) -> None:
    """Ask a probe to finalize normally so the worker can publish its output."""
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGINT)
        child.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def start_youtube_publish(
    channel: str,
    session_dir: Path,
    env: dict,
) -> subprocess.Popen:

    command = [
        sys.executable,
        "-u",
        str(
            Path(
                __file__
            ).with_name(
                "youtube_publish.py"
            )
        ),
        "--channel",
        channel,
        "--session-dir",
        str(
            session_dir
        ),
        "--limit",
        os.environ.get(
            "TOP_COUNT",
            "10",
        ),
    ]

    print(
        f"[publish] starting YouTube publish for #{channel}",
        flush=True,
    )

    return subprocess.Popen(
        command,
        env=env,
        start_new_session=True,
    )


# ---------------------------------------------------------
# Main worker
# ---------------------------------------------------------

def main() -> int:

    client_id = os.environ.get(
        "TWITCH_CLIENT_ID",
        "",
    ).strip()

    client_secret = os.environ.get(
        "TWITCH_CLIENT_SECRET",
        "",
    ).strip()

    if not client_id:

        raise RuntimeError(
            "TWITCH_CLIENT_ID is required"
        )

    default_channel = (
        normalized_channel()
    )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CHANNELS_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    CONTROL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    TOKEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    write_status(
        "authorizing",
        channel=default_channel,
    )

    nick, access_token = (
        get_twitch_identity(
            client_id,
            client_secret,
        )
    )

    child_env = (
        os.environ.copy()
    )

    child_env[
        "TWITCH_NICK"
    ] = nick

    child_env[
        "TWITCH_OAUTH_TOKEN"
    ] = access_token

    # Probe実行中
    active: dict[
        str,
        dict,
    ] = {}

    # YouTube publish中
    publishing: dict[
        str,
        dict,
    ] = {}

    # オフラインで配信開始待ち
    waiting: dict[
        str,
        str,
    ] = {}

    # 同じrequest_idを再実行しないため
    completed: dict[
        str,
        str,
    ] = {}

    cold_start_requests = (
        load_desired_channels(
            default_channel
        )
    )

    shutdown_requested = False

    next_token_refresh_check = (
        time.monotonic()
        + TOKEN_REFRESH_CHECK_SECONDS
    )

    def request_shutdown(
        _signum,
        _frame,
    ):

        nonlocal shutdown_requested

        shutdown_requested = True

    signal.signal(
        signal.SIGTERM,
        request_shutdown,
    )

    signal.signal(
        signal.SIGINT,
        request_shutdown,
    )

    write_status(
        "running",
        max_channels=MAX_CHANNELS,
    )

    while not shutdown_requested:

        # -------------------------------------------------
        # Token refresh
        # -------------------------------------------------

        if (
            time.monotonic()
            >= next_token_refresh_check
        ):

            (
                nick,
                access_token,
            ) = refresh_running_identity(
                nick,
                access_token,
                client_id,
                client_secret,
            )

            child_env[
                "TWITCH_NICK"
            ] = nick

            child_env[
                "TWITCH_OAUTH_TOKEN"
            ] = access_token

            next_token_refresh_check = (
                time.monotonic()
                + TOKEN_REFRESH_CHECK_SECONDS
            )

        desired = (
            load_desired_channels(
                default_channel
            )
        )
        configured_gap = configured_utterance_gap()
        idle_publish_minutes = configured_publish_after_idle_minutes()
        publish_requests = load_publish_requests()

        # -------------------------------------------------
        # 登録解除 / request_id変更:
        # probeを停止
        # -------------------------------------------------

        for channel, running in list(
            active.items()
        ):

            if (
                desired.get(
                    channel
                )
                != running[
                    "request_id"
                ]
            ):
                print(
                    f"[control] stopping #{channel}",
                    flush=True,
                )

                write_channel_status(
                    channel,
                    "stopping",
                )

                stop_process(
                    running[
                        "process"
                    ]
                )

                active.pop(
                    channel,
                    None,
                )

                waiting.pop(
                    channel,
                    None,
                )

                completed.pop(
                    channel,
                    None,
                )

                cold_start_requests.pop(
                    channel,
                    None,
                )

                purge_channel_data(
                    channel
                )
                continue

            if running.get("finishing_for_publish"):
                continue

            ranking_file = channel_data_dir(channel) / "highlights.json"
            try:
                ranking_mtime_ns = ranking_file.stat().st_mtime_ns
            except OSError:
                ranking_mtime_ns = None
            if ranking_mtime_ns != running.get("ranking_mtime_ns"):
                running["ranking_mtime_ns"] = ranking_mtime_ns
                running["last_ranking_activity_at"] = time.monotonic()

            manual_requested = channel in publish_requests
            idle_seconds = time.monotonic() - running["last_ranking_activity_at"]
            idle_reached = (
                idle_publish_minutes > 0
                and idle_seconds >= idle_publish_minutes * 60
            )
            if manual_requested or idle_reached:
                reason = "manual request" if manual_requested else "ranking idle"
                print(f"[publish] finishing #{channel}: {reason}", flush=True)
                running["finishing_for_publish"] = True
                if manual_requested:
                    clear_publish_request(channel, publish_requests[channel])
                write_channel_status(channel, "finalizing_for_publish")
                finish_process_for_publish(running["process"])
                continue

        # -------------------------------------------------
        # 登録解除 / request_id変更:
        # publishも停止
        # -------------------------------------------------

        for channel, job in list(
            publishing.items()
        ):

            if (
                desired.get(
                    channel
                )
                == job[
                    "request_id"
                ]
            ):
                continue

            print(
                f"[publish] stopping #{channel}",
                flush=True,
            )

            write_channel_status(
                channel,
                "stopping_publish",
            )

            stop_process(
                job[
                    "process"
                ]
            )

            publishing.pop(
                channel,
                None,
            )

            waiting.pop(
                channel,
                None,
            )

            completed.pop(
                channel,
                None,
            )

            cold_start_requests.pop(
                channel,
                None,
            )

            purge_channel_data(
                channel
            )

        # -------------------------------------------------
        # waiting中の登録解除
        # -------------------------------------------------

        for channel, request_id in list(
            waiting.items()
        ):

            if (
                desired.get(
                    channel
                )
                == request_id
            ):
                continue

            waiting.pop(
                channel,
                None,
            )

            completed.pop(
                channel,
                None,
            )

            cold_start_requests.pop(
                channel,
                None,
            )

            purge_channel_data(
                channel
            )

        # -------------------------------------------------
        # completed済み登録の削除 / 新requestへの切替
        # -------------------------------------------------

        for channel, request_id in list(
            completed.items()
        ):

            if (
                desired.get(
                    channel
                )
                == request_id
            ):
                continue

            # publish中なら上のループで処理する
            if channel in publishing:
                continue

            completed.pop(
                channel,
                None,
            )

            waiting.pop(
                channel,
                None,
            )

            cold_start_requests.pop(
                channel,
                None,
            )

            purge_channel_data(
                channel
            )

        # -------------------------------------------------
        # 新規probe開始 / オフライン待機
        # -------------------------------------------------

        for channel, request_id in (
            desired.items()
        ):

            # ★重要
            # publish中は絶対に再起動しない
            if (
                channel in active
                or channel in publishing
                or completed.get(
                    channel
                )
                == request_id
            ):
                continue

            output_dir = (
                channel_data_dir(
                    channel
                )
            )

            # cold start時に前回完成品があるなら
            # 新しいprobe開始時に維持する
            preserve_published = (
                cold_start_requests.get(
                    channel
                )
                == request_id
                and (
                    output_dir
                    / "reactions.html"
                ).is_file()
            )

            # ★重要
            # purgeより先にライブ確認する。
            # オフラインなら既存ファイルを消さない。
            stream_info = (
                get_stream_info(
                    channel,
                    access_token,
                    client_id,
                )
            )

            # ★オフラインならprobeを起動しない
            if not stream_info[
                "stream_id"
            ]:

                waiting[
                    channel
                ] = request_id

                write_channel_status(
                    channel,
                    "waiting_for_live",
                    request_id=request_id,
                )

                continue

            # ライブ開始を検出
            waiting.pop(
                channel,
                None,
            )

            completed.pop(
                channel,
                None,
            )

            # 実際に新しいセッションを開始するので
            # cold-startフラグをここで消す
            cold_start_requests.pop(
                channel,
                None,
            )

            if preserve_published:

                output_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            else:

                purge_channel_data(
                    channel
                )

            command = (
                build_probe_command(
                    channel,
                    output_dir,
                    stream_info[
                        "started_at_epoch"
                    ],
                    preserve_published=(
                        preserve_published
                    ),
                    stream_id=(
                        stream_info[
                            "stream_id"
                        ]
                    ),
                    stream_user_id=(
                        stream_info[
                            "user_id"
                        ]
                    ),
                    stream_started_at=(
                        stream_info[
                            "started_at"
                        ]
                    ),
                    utterance_gap_seconds=configured_gap,
                )
            )

            write_channel_status(
                channel,
                "starting",
                request_id=request_id,
            )

            child = subprocess.Popen(
                command,
                env=child_env,
                start_new_session=True,
            )

            active[
                channel
            ] = {
                "request_id": request_id,
                "process": child,
                "ranking_mtime_ns": None,
                "last_ranking_activity_at": time.monotonic(),
            }

            write_channel_status(
                channel,
                "running",
                pid=child.pid,
                request_id=request_id,
                stream_started_at_epoch=(
                    stream_info[
                        "started_at_epoch"
                    ]
                ),
                stream_id=(
                    stream_info[
                        "stream_id"
                    ]
                ),
            )

            print(
                f"[control] started "
                f"#{channel} "
                f"(pid {child.pid})",
                flush=True,
            )

        # -------------------------------------------------
        # Probe終了確認
        # -------------------------------------------------

        for channel, running in list(
            active.items()
        ):

            return_code = (
                running[
                    "process"
                ].poll()
            )

            if return_code is None:
                continue

            active.pop(
                channel,
                None,
            )

            request_id = (
                running[
                    "request_id"
                ]
            )

            # ---------------------------------------------
            # 正常終了 → publish
            # ---------------------------------------------

            if return_code == 0:

                # Docker stop / SIGTERMを受けているなら
                # publishを開始しない
                if shutdown_requested:

                    print(
                        f"[publish] #{channel} skipped "
                        "because worker is shutting down",
                        flush=True,
                    )

                    continue

                # probe終了直後にユーザーが登録解除した場合も
                # publishを開始しない。
                latest_desired = (
                    load_desired_channels(
                        default_channel
                    )
                )

                if (
                    latest_desired.get(
                        channel
                    )
                    != request_id
                ):

                    print(
                        f"[publish] #{channel} skipped "
                        "because request was removed or replaced",
                        flush=True,
                    )

                    waiting.pop(
                        channel,
                        None,
                    )

                    completed.pop(
                        channel,
                        None,
                    )

                    purge_channel_data(
                        channel
                    )

                    continue

                try:

                    publish_process = (
                        start_youtube_publish(
                            channel,
                            channel_data_dir(
                                channel
                            ),
                            child_env,
                        )
                    )

                    # ★publish開始直後に登録する。
                    # 次のworkerループで再起動させない。
                    publishing[
                        channel
                    ] = {
                        "request_id": (
                            request_id
                        ),
                        "process": (
                            publish_process
                        ),
                    }

                    write_channel_status(
                        channel,
                        "publishing",
                        exit_code=return_code,
                        publish_pid=(
                            publish_process.pid
                        ),
                        request_id=request_id,
                    )

                except Exception as exc:

                    completed[
                        channel
                    ] = request_id

                    write_channel_status(
                        channel,
                        "publish_error",
                        exit_code=return_code,
                        message=str(
                            exc
                        ),
                        request_id=request_id,
                    )

                    print(
                        f"[publish] #{channel} "
                        f"could not start: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

            # ---------------------------------------------
            # Probe異常終了
            # ---------------------------------------------

            else:

                # ライブ開始確認とprobe開始の間に
                # 配信が終了した可能性をチェック。
                current_stream = (
                    get_stream_info(
                        channel,
                        access_token,
                        client_id,
                    )
                )

                if (
                    desired.get(
                        channel
                    )
                    == request_id
                    and not current_stream[
                        "stream_id"
                    ]
                ):

                    waiting[
                        channel
                    ] = request_id

                    write_channel_status(
                        channel,
                        "waiting_for_live",
                        previous_exit_code=(
                            return_code
                        ),
                        request_id=request_id,
                    )

                    print(
                        f"[control] #{channel} "
                        "went offline before capture started; "
                        "waiting for live",
                        flush=True,
                    )

                else:

                    completed[
                        channel
                    ] = request_id

                    write_channel_status(
                        channel,
                        "error",
                        exit_code=(
                            return_code
                        ),
                        request_id=request_id,
                    )

                    print(
                        f"[control] #{channel} "
                        f"error ({return_code})",
                        flush=True,
                    )

        # -------------------------------------------------
        # YouTube publish終了確認
        # -------------------------------------------------

        for channel, job in list(
            publishing.items()
        ):

            return_code = (
                job[
                    "process"
                ].poll()
            )

            if return_code is None:
                continue

            publishing.pop(
                channel,
                None,
            )

            completed[
                channel
            ] = (
                job[
                    "request_id"
                ]
            )

            if return_code == 0:

                write_channel_status(
                    channel,
                    "published",
                    publish_exit_code=(
                        return_code
                    ),
                    request_id=(
                        job[
                            "request_id"
                        ]
                    ),
                )

                print(
                    f"[publish] #{channel} "
                    "YouTube publish completed",
                    flush=True,
                )

            else:

                write_channel_status(
                    channel,
                    "publish_error",
                    publish_exit_code=(
                        return_code
                    ),
                    request_id=(
                        job[
                            "request_id"
                        ]
                    ),
                )

                print(
                    f"[publish] #{channel} "
                    "YouTube publish failed "
                    f"({return_code})",
                    file=sys.stderr,
                    flush=True,
                )

        # -------------------------------------------------
        # Global status
        # -------------------------------------------------

        write_status(
            "running",
            active_channels=sorted(
                active
            ),
            waiting_channels=sorted(
                waiting
            ),
            publishing_channels=sorted(
                publishing
            ),
            completed_channels=sorted(
                completed
            ),
            configured_channels=sorted(
                desired
            ),
            max_channels=MAX_CHANNELS,
        )

        time.sleep(
            1
        )

    # -----------------------------------------------------
    # Shutdown
    # -----------------------------------------------------

    print(
        "[worker] shutdown requested",
        flush=True,
    )

    # Probe停止
    for channel, running in list(
        active.items()
    ):

        write_channel_status(
            channel,
            "stopping",
        )

        stop_process(
            running[
                "process"
            ]
        )

        write_channel_status(
            channel,
            "stopped",
        )

    # ★YouTube publishも停止
    for channel, job in list(
        publishing.items()
    ):

        print(
            f"[publish] stopping "
            f"#{channel} for worker shutdown",
            flush=True,
        )

        stop_process(
            job[
                "process"
            ]
        )

        write_channel_status(
            channel,
            "stopped",
        )

    write_status(
        "stopped"
    )

    return 0


if __name__ == "__main__":

    try:

        raise SystemExit(
            main()
        )

    except KeyboardInterrupt:

        write_status(
            "stopped"
        )

        raise SystemExit(
            130
        )

    except Exception as exc:

        write_status(
            "error",
            message=str(
                exc
            ),
        )

        print(
            f"[worker] {exc}",
            file=sys.stderr,
        )

        raise SystemExit(
            1
        )
