from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


def load_ready_highlights(
    manifest_path: Path,
    limit: int = 10,
) -> list[dict]:
    """
    highlights.jsonからready状態の候補を取得する。

    1. ランキング上位limit件を選ぶ
    2. 選ばれた候補を元配信の時系列順に並べる
    """

    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Highlights manifest not found: {manifest_path}"
        )

    payload = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    highlights = payload.get("highlights", [])

    ready = [
        item
        for item in highlights
        if (
            item.get("video_status") == "ready"
            and item.get("video_path")
        )
    ]

    # まずランキング上位10件を選ぶ
    ready = sorted(
        ready,
        key=lambda item: int(
            item.get("rank", 999999)
        ),
    )[:limit]

    # 選ばれたTop10を元配信の時系列順に並べる
    ready = sorted(
        ready,
        key=lambda item: float(
            item.get("offset_seconds", 0.0)
        ),
    )

    return ready


def format_chapter_time(seconds: float) -> str:
    """
    YouTubeチャプター用の時刻へ変換。
    """

    total_seconds = max(
        0,
        int(round(seconds)),
    )

    hours, remainder = divmod(
        total_seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{seconds:02d}"
    )


def build_youtube_chapters(
    highlights: list[dict],
    clip_duration_seconds: float = 30.0,
) -> str:
    """
    まとめ動画内の位置をYouTubeチャプターにする。

    動画自体は元配信の時系列順。
    チャプター名には元ランキング順位を表示する。

    例:
    00:00 7位
    00:30 2位
    01:00 1位
    """

    lines = []
    elapsed = 0.0

    for item in highlights:
        chapter_stamp = format_chapter_time(
            elapsed
        )

        rank = int(
            item.get("rank", 0)
        )

        lines.append(
            f"{chapter_stamp} {rank}位"
        )

        elapsed += clip_duration_seconds

    return "\n".join(lines)


def compile_highlights(
    session_dir: Path,
    output_path: Path,
    limit: int = 10,
) -> tuple[Path, list[dict]]:
    """
    readyなランキングTop10を選び、
    元配信の時系列順に1本へ結合する。
    """

    manifest_path = (
        session_dir / "highlights.json"
    )

    highlights = load_ready_highlights(
        manifest_path,
        limit=limit,
    )

    if not highlights:
        raise RuntimeError(
            "No ready highlights available"
        )

    input_paths = []

    for item in highlights:
        path = (
            session_dir
            / str(item["video_path"])
        )

        if not path.is_file():
            raise FileNotFoundError(
                f"Highlight video not found: {path}"
            )

        input_paths.append(path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.unlink(
        missing_ok=True
    )

    # FFmpeg concat用リスト
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        encoding="utf-8",
        delete=False,
    ) as concat_file:

        concat_path = Path(
            concat_file.name
        )

        for path in input_paths:
            escaped = (
                str(path.resolve())
                .replace(
                    "'",
                    "'\\''",
                )
            )

            concat_file.write(
                f"file '{escaped}'\n"
            )

    try:
        # 全クリップは同じ設定で生成されているので、
        # まず再エンコードなしで高速結合する。
        copy_command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        result = subprocess.run(
            copy_command,
            capture_output=True,
            text=True,
            timeout=300,
        )

        # codec/container条件などでcopy結合できなかった場合のみ
        # 再エンコードへフォールバック。
        if (
            result.returncode != 0
            or not output_path.is_file()
            or output_path.stat().st_size == 0
        ):
            print(
                "[compile] stream copy failed; "
                "retrying with re-encode",
                flush=True,
            )

            output_path.unlink(
                missing_ok=True
            )

            encode_command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-vf",
                "scale=-2:min(ih\\,720)",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]

            result = subprocess.run(
                encode_command,
                capture_output=True,
                text=True,
                timeout=600,
            )

            if (
                result.returncode != 0
                or not output_path.is_file()
                or output_path.stat().st_size == 0
            ):
                raise RuntimeError(
                    "Highlight compilation failed: "
                    + result.stderr.strip()
                )

    finally:
        concat_path.unlink(
            missing_ok=True
        )

    print(
        f"[compile] created: {output_path}",
        flush=True,
    )

    print(
        "[compile] order:",
        flush=True,
    )

    for index, item in enumerate(
        highlights,
        1,
    ):
        print(
            f"  {index}: "
            f"rank={item.get('rank')} "
            f"stream_offset="
            f"{float(item.get('offset_seconds', 0.0)):.1f}s",
            flush=True,
        )

    return output_path, highlights