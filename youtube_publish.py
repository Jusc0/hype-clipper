from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from highlight_compiler import (
    build_youtube_chapters,
    compile_highlights,
)
from youtube_uploader import YouTubeUploader


JST = timezone(timedelta(hours=9))


def get_stream_date(highlights: list[dict]) -> str:
    if highlights:
        stream_started_at = str(
            highlights[0].get("stream_started_at", "")
        ).strip()

        if stream_started_at:
            try:
                started = datetime.fromisoformat(
                    stream_started_at.replace("Z", "+00:00")
                )
                return started.astimezone(JST).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return datetime.now(JST).strftime("%Y-%m-%d")


def publish_channel(
    channel: str,
    session_dir: Path,
    limit: int = 10,
) -> str:
    session_dir = session_dir.resolve()

    output_path = session_dir / f"{channel}_top10.mp4"

    compiled_path, highlights = compile_highlights(
        session_dir,
        output_path,
        limit=limit,
    )

    if not highlights:
        raise RuntimeError(
            f"No highlights available for {channel}"
        )

    chapters = build_youtube_chapters(
        highlights,
    )

    stream_date = get_stream_date(highlights)

    client_secrets = os.environ.get(
        "YOUTUBE_CLIENT_SECRETS_FILE",
        "",
    ).strip()

    token_file = os.environ.get(
        "YOUTUBE_TOKEN_FILE",
        "",
    ).strip()

    if not client_secrets:
        raise RuntimeError(
            "YOUTUBE_CLIENT_SECRETS_FILE is not set"
        )

    if not token_file:
        raise RuntimeError(
            "YOUTUBE_TOKEN_FILE is not set"
        )

    uploader = YouTubeUploader(
        client_secrets_file=client_secrets,
        token_file=token_file,
    )

    video_id = uploader.upload(
        compiled_path,
        title=f"{channel} {stream_date}",
        description=chapters,
        privacy_status="unlisted",
        playlist_title=channel,
    )

    print(
        f"[publish] upload complete: "
        f"{channel} -> {video_id}",
        flush=True,
    )

    # YouTubeへのアップロードとプレイリスト追加が
    # 全て成功した後だけ結合済み動画を削除する。
    try:
        compiled_path.unlink(missing_ok=True)
        print(
            f"[publish] deleted compiled file: "
            f"{compiled_path.name}",
            flush=True,
        )
    except OSError as exc:
        print(
            f"[publish] cleanup failed: {exc}",
            flush=True,
        )

    return video_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compile Hype Clipper highlights "
            "and upload to YouTube"
        )
    )

    parser.add_argument(
        "--channel",
        required=True,
    )

    parser.add_argument(
        "--session-dir",
        required=True,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
    )

    args = parser.parse_args()

    publish_channel(
        channel=args.channel,
        session_dir=Path(args.session_dir),
        limit=args.limit,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
