"""Read-only post-deploy checks inside the web container."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request


def main() -> None:
    base_url = os.environ.get("HYPE_CHECK_URL", "http://127.0.0.1:8000")
    with urllib.request.urlopen(
        f"{base_url}/api/channels", timeout=10
    ) as response:
        payload = json.load(response)
    print("max_channels", payload["max_channels"])
    print("channels", [item["channel"] for item in payload["channels"]])
    print("counts", [item["highlight_count"] for item in payload["channels"]])
    with urllib.request.urlopen(
        f"{base_url}/healthz", timeout=10
    ) as response:
        print("health", response.status, response.read().decode("utf-8"))
    legacy = []
    for name in ("video_buffer", "candidate_buffer", "capture.ts"):
        legacy.extend(Path("/data/channels").glob(f"*/{name}"))
    print("legacy_media", [str(path) for path in legacy])
    print(
        "manifests",
        [str(path) for path in Path("/data/channels").glob("*/highlights.json")],
    )


if __name__ == "__main__":
    main()
