"""Create a protected VPS .env without printing Twitch credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess


CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")


def quote_dotenv(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--domain", required=True)
    parser.add_argument("--channel", default="yaritaiji")
    parser.add_argument("--username", default="hype")
    args = parser.parse_args()

    channel = args.channel.strip().lower().lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        parser.error("invalid Twitch channel")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client_id = str(config.get("twitch_client_id", "")).strip()
    client_secret = str(config.get("twitch_client_secret", "")).strip()
    if not client_id or not client_secret:
        parser.error("config must contain twitch_client_id and twitch_client_secret")

    password = secrets.token_urlsafe(18)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "caddy:2-alpine",
            "caddy",
            "hash-password",
            "--plaintext",
            password,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    password_hash = result.stdout.strip()
    if not password_hash:
        raise RuntimeError("Caddy did not return a password hash")

    values = {
        "HYPE_DOMAIN": args.domain,
        "HYPE_BASIC_AUTH_USER": args.username,
        "HYPE_BASIC_AUTH_HASH": password_hash,
        "TWITCH_CLIENT_ID": client_id,
        "TWITCH_CLIENT_SECRET": client_secret,
        "TWITCH_CHANNEL": channel,
        "DURATION_MINUTES": "0",
        "HIGHLIGHT_SECONDS": "30",
        "PREROLL_SECONDS": "5",
        "UTTERANCE_GAP_SECONDS": "2.5",
        "PREVIOUS_LOOKBACK_SECONDS": "20",
        "TOP_COUNT": "10",
        "PREVIEW_INTERVAL_MINUTES": "1",
        "SEGMENT_SECONDS": "8",
        "VOD_POLL_SECONDS": "60",
        "VOD_READY_MARGIN_SECONDS": "10",
        "VOD_FINALIZE_MINUTES": "15",
        "MAX_CHANNELS": "3",
    }
    args.output.write_text(
        "\n".join(f"{key}={quote_dotenv(value)}" for key, value in values.items())
        + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)
    print(f"WEB_USERNAME={args.username}")
    print(f"WEB_PASSWORD={password}")


if __name__ == "__main__":
    main()
