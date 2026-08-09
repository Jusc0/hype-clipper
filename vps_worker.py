"""Headless Hype Clipper worker with persistent Twitch device authentication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import requests


DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = "chat:read"
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")

DATA_DIR = Path(os.environ.get("HYPE_DATA_DIR", "/data"))
TOKEN_FILE = Path(
    os.environ.get("TWITCH_TOKEN_FILE", "/auth/twitch_tokens.json")
)
STATUS_FILE = DATA_DIR / "service_status.json"


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
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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


def normalized_channel() -> str:
    channel = os.environ.get("TWITCH_CHANNEL", "").strip().lower().lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        raise RuntimeError(
            "TWITCH_CHANNEL must contain only letters, numbers, and underscores"
        )
    return channel


def add_numeric_option(command: list[str], env_name: str, option: str) -> None:
    value = os.environ.get(env_name, "").strip()
    if value:
        command.extend([option, value])


def build_probe_command(channel: str) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).with_name("twitch_reaction_probe.py")),
        "--channel",
        channel,
        "--out",
        str(DATA_DIR),
        "--no-preview-server",
    ]
    for env_name, option in (
        ("DURATION_MINUTES", "--duration-minutes"),
        ("HIGHLIGHT_SECONDS", "--highlight-seconds"),
        ("PREROLL_SECONDS", "--preroll-seconds"),
        ("UTTERANCE_GAP_SECONDS", "--utterance-gap-seconds"),
        ("PREVIOUS_LOOKBACK_SECONDS", "--previous-lookback-seconds"),
        ("TOP_COUNT", "--top-count"),
        ("PREVIEW_INTERVAL_MINUTES", "--preview-interval-minutes"),
        ("SEGMENT_SECONDS", "--segment-seconds"),
    ):
        add_numeric_option(command, env_name, option)
    return command


def main() -> int:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id:
        raise RuntimeError("TWITCH_CLIENT_ID is required")
    channel = normalized_channel()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_status("authorizing", channel=channel)
    nick, access_token = get_twitch_identity(client_id, client_secret)

    command = build_probe_command(channel)
    child_env = os.environ.copy()
    child_env["TWITCH_NICK"] = nick
    child_env["TWITCH_OAUTH_TOKEN"] = access_token
    write_status("starting", channel=channel)
    child = subprocess.Popen(command, env=child_env)

    def forward_signal(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    write_status("running", channel=channel, pid=child.pid)
    return_code = child.wait()
    if return_code == 0:
        write_status("stopped", channel=channel, exit_code=return_code)
    else:
        write_status("error", channel=channel, exit_code=return_code)
    return return_code


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
