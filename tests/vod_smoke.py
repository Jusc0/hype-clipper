"""Read-only Twitch/VOD integration smoke test for the deployed worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from vod_clip_manager import find_matching_vod, generate_vod_clip
from vps_worker import get_stream_info, get_twitch_identity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("channels", nargs="*")
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    client_id = os.environ["TWITCH_CLIENT_ID"]
    _login, token = get_twitch_identity(
        client_id, os.environ.get("TWITCH_CLIENT_SECRET", "")
    )
    channels = args.channels or [
        item["channel"]
        for item in json.loads(
            Path("/control/channels.json").read_text(encoding="utf-8")
        )["channels"]
    ]
    for channel in channels:
        stream = get_stream_info(channel, token, client_id)
        vod = find_matching_vod(
            stream["user_id"],
            stream["stream_id"],
            stream["started_at"],
            client_id,
            token,
        )
        print(
            channel,
            f"stream={stream['stream_id'] or '-'}",
            f"vod={(vod or {}).get('id', '-')}",
            f"duration={(vod or {}).get('duration_seconds', 0):.0f}",
            f"match={(vod or {}).get('match_method', '-')}",
            flush=True,
        )
        if args.generate and vod:
            output = Path("/tmp/vod-smoke.mp4")
            ok, error, retryable = generate_vod_clip(
                vod["url"], output, 120.0, 30.0
            )
            print(
                channel,
                f"generated={ok}",
                f"bytes={output.stat().st_size if output.is_file() else 0}",
                f"retryable={retryable}",
                f"error={error}",
                flush=True,
            )


if __name__ == "__main__":
    main()
