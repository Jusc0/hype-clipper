"""Short live audio_only capture smoke test; writes only to a temp directory."""

from __future__ import annotations

import argparse
import tempfile
import time
import wave
from pathlib import Path

from twitch_reaction_probe import AudioOnlyCapture


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("channel")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        chunk_dir = Path(temporary) / "audio_chunks"
        capture = AudioOnlyCapture(
            f"https://www.twitch.tv/{args.channel}", chunk_dir, 8
        )
        capture.start()
        time.sleep(10)
        capture.stop()
        durations = []
        for path in sorted(chunk_dir.glob("*.wav")):
            with wave.open(str(path), "rb") as wav:
                durations.append(wav.getnframes() / wav.getframerate())
        print(
            f"channel={args.channel}",
            f"chunks={len(durations)}",
            "durations=" + ",".join(f"{value:.2f}" for value in durations),
            "video_files=0",
            flush=True,
        )


if __name__ == "__main__":
    main()
