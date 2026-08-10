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

DATA_DIR = Path(os.environ.get("HYPE_DATA_DIR", "/data"))
TOKEN_FILE = Path(
    os.environ.get("TWITCH_TOKEN_FILE", "/auth/twitch_tokens.json")
)
STATUS_FILE = DATA_DIR / "service_status.json"
CHANNELS_ROOT = DATA_DIR / "channels"
CONTROL_DIR = Path(os.environ.get("HYPE_CONTROL_DIR", "/control"))
CHANNELS_FILE = CONTROL_DIR / "channels.json"
MAX_CHANNELS = max(1, int(os.environ.get("MAX_CHANNELS", "3")))
TOKEN_REFRESH_CHECK_SECONDS = 300.0
TOKEN_REFRESH_BELOW_SECONDS = 900
JST = timezone(timedelta(hours=9), "JST")


def now_iso_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_status(state: str, **details) -> None:
    atomic_write_json(
        STATUS_FILE,
        {
            "state": state,
            "updated_at": now_iso_jst(),
            **details,
        },
    )


def channel_data_dir(channel: str) -> Path:
    root = CHANNELS_ROOT.resolve()
    target = (root / channel).resolve()
    if target.parent != root:
        raise RuntimeError("unsafe channel data path")
    return target


def write_channel_status(channel: str, state: str, **details) -> None:
    atomic_write_json(
        channel_data_dir(channel) / "service_status.json",
        {
            "state": state,
            "updated_at": now_iso_jst(),
            "channel": channel,
            **details,
        },
    )


def load_tokens() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[auth] token file could not be read: {exc}", file=sys.stderr)
        return {}


def save_tokens(tokens: dict) -> None:
    stored = dict(tokens)
    stored["saved_at"] = int(time.time())
    atomic_write_json(TOKEN_FILE, stored)


def validate_token(access_token: str, client_id: str) -> dict | None:
    response = requests.get(
        VALIDATE_URL,
        headers={"Authorization": f"OAuth {access_token}"},
        timeout=20,
    )
    if response.status_code == 401:
        return None
    response.raise_for_status()
    validation = response.json()
    if validation.get("client_id") != client_id:
        print("[auth] stored token belongs to another client", file=sys.stderr)
        return None
    if SCOPES not in validation.get("scopes", []):
        print("[auth] stored token does not include chat:read", file=sys.stderr)
        return None
    return validation


def refresh_tokens(
    tokens: dict, client_id: str, client_secret: str
) -> dict | None:
    refresh_token = str(tokens.get("refresh_token", "")).strip()
    if not refresh_token:
        return None
    form = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        form["client_secret"] = client_secret
    response = requests.post(TOKEN_URL, data=form, timeout=20)
    if response.status_code >= 400:
        print(
            f"[auth] token refresh failed ({response.status_code}); "
            "new device authorization is required",
            file=sys.stderr,
        )
        return None
    refreshed = response.json()
    if not refreshed.get("refresh_token"):
        refreshed["refresh_token"] = refresh_token
    save_tokens(refreshed)
    return refreshed


def device_authorize(client_id: str) -> dict:
    write_status("waiting_for_twitch_authorization")
    response = requests.post(
        DEVICE_URL,
        data={"client_id": client_id, "scopes": SCOPES},
        timeout=20,
    )
    response.raise_for_status()
    device = response.json()
    verification_uri = device["verification_uri"]
    user_code = device["user_code"]
    print("\n=== Twitch authorization required ===", flush=True)
    print(f"Open: {verification_uri}", flush=True)
    print(f"Code: {user_code}\n", flush=True)
    write_status(
        "waiting_for_twitch_authorization",
        verification_uri=verification_uri,
        user_code=user_code,
    )

    interval = max(1, int(device.get("interval", 5)))
    deadline = time.monotonic() + int(device.get("expires_in", 1800))
    while time.monotonic() < deadline:
        time.sleep(interval)
        token_response = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "scopes": SCOPES,
                "device_code": device["device_code"],
                "grant_type": DEVICE_GRANT,
            },
            timeout=20,
        )
        if token_response.ok:
            tokens = token_response.json()
            save_tokens(tokens)
            print("[auth] Twitch authorization completed", flush=True)
            return tokens

        try:
            message = str(token_response.json().get("message", "")).lower()
        except (ValueError, AttributeError):
            message = ""
        if "authorization_pending" in message:
            continue
        if "slow_down" in message:
            interval += 5
            continue
        token_response.raise_for_status()

    raise RuntimeError("Twitch device authorization timed out")


def get_twitch_identity(client_id: str, client_secret: str) -> tuple[str, str]:
    tokens = load_tokens()
    access_token = str(tokens.get("access_token", "")).strip()
    validation = None
    if access_token:
        validation = validate_token(access_token, client_id)
        if validation and int(validation.get("expires_in", 0)) > 300:
            return validation["login"], access_token

    refreshed = refresh_tokens(tokens, client_id, client_secret)
    if refreshed:
        access_token = refreshed["access_token"]
        validation = validate_token(access_token, client_id)
        if validation:
            return validation["login"], access_token

    tokens = device_authorize(client_id)
    access_token = tokens["access_token"]
    validation = validate_token(access_token, client_id)
    if not validation:
        raise RuntimeError("Twitch returned an invalid access token")
    return validation["login"], access_token


def refresh_running_identity(
    nick: str,
    access_token: str,
    client_id: str,
    client_secret: str,
) -> tuple[str, str]:
    """Refresh without entering the interactive device flow."""
    tokens = load_tokens()
    stored_access_token = str(tokens.get("access_token", "")).strip()
    candidates = [stored_access_token, access_token]
    best_validation = None
    best_token = access_token
    for candidate in dict.fromkeys(token for token in candidates if token):
        try:
            validation = validate_token(candidate, client_id)
        except requests.RequestException as exc:
            print(f"[auth] periodic validation failed: {exc}", file=sys.stderr)
            return nick, access_token
        if not validation:
            continue
        best_validation = validation
        best_token = candidate
        if int(validation.get("expires_in", 0)) > TOKEN_REFRESH_BELOW_SECONDS:
            return str(validation.get("login", nick)), candidate

    try:
        refreshed = refresh_tokens(tokens, client_id, client_secret)
    except requests.RequestException as exc:
        print(f"[auth] periodic refresh failed: {exc}", file=sys.stderr)
        refreshed = None
    if refreshed:
        refreshed_token = str(refreshed.get("access_token", "")).strip()
        try:
            validation = validate_token(refreshed_token, client_id)
        except requests.RequestException as exc:
            print(f"[auth] refreshed token validation failed: {exc}", file=sys.stderr)
            validation = None
        if validation:
            print("[auth] Twitch access token refreshed", flush=True)
            return str(validation.get("login", nick)), refreshed_token
    if best_validation:
        return str(best_validation.get("login", nick)), best_token
    return nick, access_token


def normalized_channel() -> str:
    channel = os.environ.get("TWITCH_CHANNEL", "").strip().lower().lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        raise RuntimeError(
            "TWITCH_CHANNEL must contain only letters, numbers, and underscores"
        )
    return channel


def normalize_channel(value: str) -> str:
    channel = value.strip().lower().lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        raise ValueError("invalid Twitch channel")
    return channel


def get_stream_info(
    channel: str, access_token: str, client_id: str
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
            params={"user_login": channel},
            headers={
                "Client-Id": client_id,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=20,
        )
        response.raise_for_status()
        streams = response.json().get("data", [])
        if not streams:
            return empty
        stream = streams[0]
        started_at = str(stream["started_at"])
        return {
            "stream_id": str(stream["id"]),
            "user_id": str(stream["user_id"]),
            "started_at": started_at,
            "started_at_epoch": datetime.fromisoformat(
                started_at.replace("Z", "+00:00")
            ).timestamp(),
        }
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        print(
            f"[stream:{channel}] start time unavailable: {exc}",
            file=sys.stderr,
        )
        return empty


def get_stream_started_at_epoch(
    channel: str, access_token: str, client_id: str
) -> float:
    return float(
        get_stream_info(channel, access_token, client_id)["started_at_epoch"]
    )


def add_numeric_option(command: list[str], env_name: str, option: str) -> None:
    value = os.environ.get(env_name, "").strip()
    if value:
        command.extend([option, value])


def build_probe_command(
    channel: str,
    output_dir: Path | None = None,
    stream_started_at_epoch: float = 0.0,
    preserve_published: bool = False,
    stream_id: str = "",
    stream_user_id: str = "",
    stream_started_at: str = "",
) -> list[str]:
    output_dir = output_dir or DATA_DIR
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).with_name("twitch_reaction_probe.py")),
        "--channel",
        channel,
        "--out",
        str(output_dir),
        "--no-preview-server",
        "--stream-started-at-epoch",
        str(stream_started_at_epoch),
    ]
    for option, value in (
        ("--stream-id", stream_id),
        ("--stream-user-id", stream_user_id),
        ("--stream-started-at", stream_started_at),
    ):
        if value:
            command.extend([option, value])
    for env_name, option in (
        ("DURATION_MINUTES", "--duration-minutes"),
        ("HIGHLIGHT_SECONDS", "--highlight-seconds"),
        ("PREROLL_SECONDS", "--preroll-seconds"),
        ("UTTERANCE_GAP_SECONDS", "--utterance-gap-seconds"),
        ("PREVIOUS_LOOKBACK_SECONDS", "--previous-lookback-seconds"),
        ("TOP_COUNT", "--top-count"),
        ("PREVIEW_INTERVAL_MINUTES", "--preview-interval-minutes"),
        ("SEGMENT_SECONDS", "--segment-seconds"),
        ("VOD_POLL_SECONDS", "--vod-poll-seconds"),
        ("VOD_READY_MARGIN_SECONDS", "--vod-ready-margin-seconds"),
        ("VOD_FINALIZE_MINUTES", "--vod-finalize-minutes"),
    ):
        add_numeric_option(command, env_name, option)
    if preserve_published:
        command.append("--preserve-published-on-start")
    return command


def default_channels_payload(channel: str) -> dict:
    return {
        "channels": [
            {
                "channel": channel,
                "request_id": f"default-{int(time.time())}",
                "added_at": now_iso_jst(),
            }
        ]
    }


def load_desired_channels(default_channel: str) -> dict[str, str]:
    if not CHANNELS_FILE.exists():
        atomic_write_json(CHANNELS_FILE, default_channels_payload(default_channel))
    try:
        payload = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[control] could not read channels: {exc}", file=sys.stderr)
        return {}
    desired = {}
    for entry in payload.get("channels", []):
        if len(desired) >= MAX_CHANNELS:
            break
        if not isinstance(entry, dict):
            continue
        try:
            channel = normalize_channel(str(entry.get("channel", "")))
        except ValueError:
            continue
        request_id = str(entry.get("request_id", "")).strip()
        if not request_id or channel in desired:
            continue
        desired[channel] = request_id
    return desired


def purge_channel_data(channel: str) -> None:
    target = channel_data_dir(channel)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def stop_process(child: subprocess.Popen, timeout: float = 15.0) -> None:
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

def start_youtube_publish(
    channel: str,
    session_dir: Path,
    env: dict,
) -> subprocess.Popen:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).with_name("youtube_publish.py")),
        "--channel",
        channel,
        "--session-dir",
        str(session_dir),
        "--limit",
        os.environ.get("TOP_COUNT", "10"),
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

def main() -> int:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id:
        raise RuntimeError("TWITCH_CLIENT_ID is required")
    default_channel = normalized_channel()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHANNELS_ROOT.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_status("authorizing", channel=default_channel)
    nick, access_token = get_twitch_identity(client_id, client_secret)
    child_env = os.environ.copy()
    child_env["TWITCH_NICK"] = nick
    child_env["TWITCH_OAUTH_TOKEN"] = access_token
    active: dict[str, dict] = {}
    publishing: dict[str, dict] = {}
    completed: dict[str, str] = {}
    cold_start_requests = load_desired_channels(default_channel)
    shutdown_requested = False
    next_token_refresh_check = time.monotonic() + TOKEN_REFRESH_CHECK_SECONDS

    def request_shutdown(_signum, _frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    write_status("running", max_channels=MAX_CHANNELS)

    while not shutdown_requested:
        if time.monotonic() >= next_token_refresh_check:
            nick, access_token = refresh_running_identity(
                nick, access_token, client_id, client_secret
            )
            child_env["TWITCH_NICK"] = nick
            child_env["TWITCH_OAUTH_TOKEN"] = access_token
            next_token_refresh_check = (
                time.monotonic() + TOKEN_REFRESH_CHECK_SECONDS
            )
        desired = load_desired_channels(default_channel)

        for channel, running in list(active.items()):
            if desired.get(channel) == running["request_id"]:
                continue
            write_channel_status(channel, "stopping")
            stop_process(running["process"])
            active.pop(channel, None)
            completed.pop(channel, None)
            purge_channel_data(channel)

        for channel, request_id in list(completed.items()):
            if desired.get(channel) == request_id:
                continue

            if channel in publishing:
                continue

            completed.pop(channel, None)
            purge_channel_data(channel)

        for channel, request_id in desired.items():
            if channel in active or completed.get(channel) == request_id:
                continue
            output_dir = channel_data_dir(channel)
            preserve_published = (
                cold_start_requests.pop(channel, None) == request_id
                and (output_dir / "reactions.html").is_file()
            )
            if preserve_published:
                output_dir.mkdir(parents=True, exist_ok=True)
            else:
                purge_channel_data(channel)
            stream_info = get_stream_info(
                channel, access_token, client_id
            )
            command = build_probe_command(
                channel,
                output_dir,
                stream_info["started_at_epoch"],
                preserve_published=preserve_published,
                stream_id=stream_info["stream_id"],
                stream_user_id=stream_info["user_id"],
                stream_started_at=stream_info["started_at"],
            )
            write_channel_status(channel, "starting")
            child = subprocess.Popen(
                command,
                env=child_env,
                start_new_session=True,
            )
            active[channel] = {
                "request_id": request_id,
                "process": child,
            }
            write_channel_status(
                channel,
                "running",
                pid=child.pid,
                stream_started_at_epoch=stream_info["started_at_epoch"],
                stream_id=stream_info["stream_id"],
            )
            print(f"[control] started #{channel} (pid {child.pid})", flush=True)

        for channel, running in list(active.items()):
            return_code = running["process"].poll()

            if return_code is None:
                continue

            active.pop(channel, None)

            request_id = running["request_id"]

            if return_code == 0:
                try:
                    publish_process = start_youtube_publish(
                        channel,
                        channel_data_dir(channel),
                        child_env,
                    )

                    publishing[channel] = {
                        "request_id": request_id,
                        "process": publish_process,
                    }

                    write_channel_status(
                        channel,
                        "publishing",
                        exit_code=return_code,
                        publish_pid=publish_process.pid,
                    )

                except Exception as exc:
                    completed[channel] = request_id

                    write_channel_status(
                        channel,
                        "publish_error",
                        exit_code=return_code,
                        message=str(exc),
                    )

                    print(
                        f"[publish] #{channel} could not start: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

            else:
                completed[channel] = request_id

                write_channel_status(
                    channel,
                    "error",
                    exit_code=return_code,
                )

                print(
                    f"[control] #{channel} error ({return_code})",
                    flush=True,
                )
        for channel, job in list(publishing.items()):
            return_code = job["process"].poll()

            if return_code is None:
                continue

            publishing.pop(channel, None)
            completed[channel] = job["request_id"]

            if return_code == 0:
                write_channel_status(
                    channel,
                    "published",
                    publish_exit_code=return_code,
                )

                print(
                    f"[publish] #{channel} YouTube publish completed",
                    flush=True,
                )

            else:
                write_channel_status(
                    channel,
                    "publish_error",
                    publish_exit_code=return_code,
                )

                print(
                    f"[publish] #{channel} YouTube publish failed "
                    f"({return_code})",
                    file=sys.stderr,
                    flush=True,
                )
        write_status(
            "running",
            active_channels=sorted(active),
            publishing_channels=sorted(publishing),
            configured_channels=sorted(desired),
            max_channels=MAX_CHANNELS,
        )
        time.sleep(1)

    for channel, running in list(active.items()):
        write_channel_status(channel, "stopping")
        stop_process(running["process"])
        write_channel_status(channel, "stopped")
    write_status("stopped")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        write_status("stopped")
        raise SystemExit(130)
    except Exception as exc:
        write_status("error", message=str(exc))
        print(f"[worker] {exc}", file=sys.stderr)
        raise SystemExit(1)
