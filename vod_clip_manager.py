"""Resolve Twitch VODs and generate ranked clips without recording live video."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests


VIDEOS_URL = "https://api.twitch.tv/helix/videos"
VOD_QUALITY_FALLBACK = "720p60,720p,best"
HLS_SEGMENT_GUARD_SECONDS = 12.0
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


def parse_twitch_duration(value: str) -> float:
    match = re.fullmatch(
        r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?",
        str(value or "").strip(),
    )
    if not match or not any(match.groupdict().values()):
        return 0.0
    return (
        int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + int(match.group("seconds") or 0)
    )


def parse_rfc3339(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def candidate_id(stream_id: str, offset_seconds: float) -> str:
    value = f"{stream_id}:{offset_seconds:.3f}".encode("utf-8")
    return hashlib.sha1(value).hexdigest()[:14]


def find_matching_vod(
    user_id: str,
    stream_id: str,
    stream_started_at: str,
    client_id: str,
    access_token: str,
) -> dict | None:
    if not user_id or not client_id or not access_token:
        return None
    response = requests.get(
        VIDEOS_URL,
        params={
            "user_id": user_id,
            "type": "archive",
            "sort": "time",
            "first": 20,
        },
        headers={
            "Client-Id": client_id,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=20,
    )
    response.raise_for_status()
    videos = response.json().get("data", [])

    selected = None
    if stream_id:
        selected = next(
            (
                video
                for video in videos
                if str(video.get("stream_id", "")) == stream_id
            ),
            None,
        )
    match_method = "stream_id"
    if selected is None and stream_started_at:
        try:
            started_epoch = parse_rfc3339(stream_started_at)
        except (TypeError, ValueError):
            started_epoch = 0.0
        if started_epoch:
            close_matches = []
            for video in videos:
                # Never substitute a VOD that is explicitly tied to another stream.
                video_stream_id = str(video.get("stream_id", ""))
                if stream_id and video_stream_id:
                    continue
                try:
                    difference = abs(
                        parse_rfc3339(str(video.get("created_at", "")))
                        - started_epoch
                    )
                except (TypeError, ValueError):
                    continue
                if difference <= 300:
                    close_matches.append((difference, video))
            if close_matches:
                selected = min(close_matches, key=lambda item: item[0])[1]
                match_method = "started_at"

    if selected is None:
        return None
    result = dict(selected)
    result["duration_seconds"] = parse_twitch_duration(
        str(selected.get("duration", ""))
    )
    result["match_method"] = match_method
    return result


def vod_has_range(
    vod: dict,
    offset_seconds: float,
    duration_seconds: float,
    margin_seconds: float = 10.0,
) -> bool:
    required_end = offset_seconds + duration_seconds + margin_seconds
    return float(vod.get("duration_seconds", 0.0)) >= required_end


def build_vod_streamlink_command(
    vod_url: str,
    offset_seconds: float,
    duration_seconds: float,
) -> tuple[list[str], float]:
    # Streamlink supports comma-separated fallback stream names. This selects
    # 720p when present and falls back to its dynamically resolved best stream.
    # The HLS start/duration options select only the small group of segments
    # around the requested clip; the VOD is never read from its beginning.
    segment_start = max(0.0, offset_seconds - HLS_SEGMENT_GUARD_SECONDS)
    trim_seconds = offset_seconds - segment_start
    segment_duration = trim_seconds + duration_seconds
    return [
        "streamlink",
        "--loglevel",
        "error",
        "--stdout",
        "--hls-start-offset",
        f"{segment_start:.3f}s",
        "--stream-segmented-duration",
        f"{segment_duration:.3f}s",
        vod_url,
        VOD_QUALITY_FALLBACK,
    ], trim_seconds


@contextmanager
def encoding_slot():
    lock_path = os.environ.get("HYPE_ENCODE_LOCK_FILE", "").strip()
    if not lock_path:
        yield
        return
    try:
        import fcntl
    except ImportError:
        yield
        return
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        print("[vod] waiting for shared encoder slot", flush=True)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def generated_clip_is_complete(path: Path, duration_seconds: float) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        actual_duration = float(result.stdout.strip())
    except ValueError:
        return False
    return result.returncode == 0 and actual_duration >= duration_seconds - 2.0


def generate_vod_clip(
    vod_url: str,
    output_path: Path,
    offset_seconds: float,
    duration_seconds: float,
) -> tuple[bool, str, bool]:
    temporary = output_path.with_suffix(".new.mp4")
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    streamlink_command, trim_seconds = build_vod_streamlink_command(
        vod_url, offset_seconds, duration_seconds
    )
    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-ss",
        f"{trim_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
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
        str(temporary),
    ]
    streamlink_process = None
    ffmpeg_process = None
    try:
        with encoding_slot():
            streamlink_process = subprocess.Popen(
                streamlink_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdin=streamlink_process.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            streamlink_process.stdout.close()
            _output, ffmpeg_error = ffmpeg_process.communicate(timeout=300)
            try:
                streamlink_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                streamlink_process.terminate()
                streamlink_process.wait(timeout=10)
            streamlink_error_bytes = streamlink_process.stderr.read()
    except subprocess.TimeoutExpired:
        if ffmpeg_process and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
        if streamlink_process and streamlink_process.poll() is None:
            streamlink_process.kill()
        temporary.unlink(missing_ok=True)
        return False, "FFmpeg timed out while generating the VOD clip", False
    except (OSError, subprocess.SubprocessError) as exc:
        if ffmpeg_process and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
        if streamlink_process and streamlink_process.poll() is None:
            streamlink_process.kill()
        temporary.unlink(missing_ok=True)
        return False, str(exc), False
    streamlink_error = streamlink_error_bytes.decode(
        "utf-8", errors="replace"
    ).strip()
    if ffmpeg_process.returncode != 0:
        temporary.unlink(missing_ok=True)
        return False, ffmpeg_error.strip() or "FFmpeg failed", False
    # FFmpeg intentionally exits after the requested 30 seconds. Streamlink
    # can then report BrokenPipe while finishing the final selected segment;
    # a validated output is authoritative in that case.
    if generated_clip_is_complete(temporary, duration_seconds):
        temporary.replace(output_path)
        return True, "", False
    if streamlink_process.returncode != 0:
        temporary.unlink(missing_ok=True)
        return False, streamlink_error or "Streamlink could not read the VOD range", True
    temporary.unlink(missing_ok=True)
    return False, "The requested VOD range is not complete yet", True


class VodClipManager(threading.Thread):
    def __init__(
        self,
        output_dir: Path,
        channel: str,
        stream_id: str,
        user_id: str,
        stream_started_at: str,
        client_id: str,
        access_token: str,
        duration_seconds: float,
        poll_seconds: float = 60.0,
        ready_margin_seconds: float = 10.0,
        on_change=None,
    ):
        super().__init__(daemon=True)
        self.output_dir = output_dir
        self.channel = channel
        self.stream_id = stream_id
        self.user_id = user_id
        self.stream_started_at = stream_started_at
        self.client_id = client_id
        self.access_token = access_token
        self.duration_seconds = duration_seconds
        self.poll_seconds = max(30.0, poll_seconds)
        self.ready_margin_seconds = max(0.0, ready_margin_seconds)
        self.on_change = on_change
        self.manifest_path = output_dir / "highlights.json"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop_requested = threading.Event()
        self._entries: dict[str, dict] = {}
        self._ranking_ids: list[str] = []
        self._vod: dict | None = None
        self._next_vod_lookup = 0.0
        self._legacy_cleaned = False

    def _persist_locked(self) -> None:
        atomic_write_json(
            self.manifest_path,
            {
                "schema_version": 1,
                "streamer": self.channel,
                "stream_id": self.stream_id,
                "stream_started_at": self.stream_started_at,
                "updated_at": now_iso_jst(),
                "highlights": [
                    dict(self._entries[item_id])
                    for item_id in self._ranking_ids
                    if item_id in self._entries
                ],
            },
        )

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:
                print(f"[vod] ranking render failed: {exc}", flush=True)

    def sync(self, candidates: list[dict]) -> list[dict]:
        with self._lock:
            if not self._legacy_cleaned:
                for pattern in (
                    "preview_candidate_*.mp4",
                    "preview_chat_*.mp4",
                    "highlight_chat_*.mp4",
                ):
                    for path in self.output_dir.glob(pattern):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                self._legacy_cleaned = True
            next_ids = []
            for rank, candidate in enumerate(candidates, 1):
                offset_seconds = float(candidate["offset_seconds"])
                item_id = candidate.get("candidate_id") or candidate_id(
                    self.stream_id, offset_seconds
                )
                next_ids.append(item_id)
                entry = self._entries.get(item_id)
                if entry is None:
                    entry = {
                        "candidate_id": item_id,
                        "streamer": self.channel,
                        "streamer_login": self.channel,
                        "stream_id": self.stream_id,
                        "stream_user_id": self.user_id,
                        "stream_started_at": self.stream_started_at,
                        "offset_seconds": round(offset_seconds, 3),
                        "duration_seconds": self.duration_seconds,
                        "score": int(candidate.get("score", candidate["chat_count"])),
                        "chat_count": int(candidate["chat_count"]),
                        "trigger_text": candidate.get("trigger_text", ""),
                        "video_status": "waiting_vod",
                        "video_path": "",
                        "vod_id": "",
                        "vod_url": "",
                        "vod_duration_seconds": 0.0,
                        "vod_match_method": "",
                        "last_error": "",
                    }
                    self._entries[item_id] = entry
                entry.update(
                    {
                        "rank": rank,
                        "offset_seconds": round(offset_seconds, 3),
                        "score": int(candidate.get("score", candidate["chat_count"])),
                        "chat_count": int(candidate["chat_count"]),
                        "trigger_text": candidate.get("trigger_text", ""),
                    }
                )

            removed = set(self._ranking_ids) - set(next_ids)
            for item_id in removed:
                entry = self._entries.pop(item_id, None)
                if not entry:
                    continue
                video_name = str(entry.get("video_path", ""))
                if video_name.startswith("preview_vod_") and video_name.endswith(".mp4"):
                    try:
                        (self.output_dir / video_name).unlink(missing_ok=True)
                    except OSError:
                        pass
            self._ranking_ids = next_ids
            self._persist_locked()
            snapshot = self._snapshot_locked()
        self._wake.set()
        return snapshot

    def _snapshot_locked(self) -> list[dict]:
        result = []
        for item_id in self._ranking_ids:
            if item_id not in self._entries:
                continue
            item = dict(self._entries[item_id])
            video_name = str(item.get("video_path", ""))
            if item.get("video_status") == "ready" and video_name:
                item["video_name"] = video_name
            result.append(item)
        return result

    def rankings(self) -> list[dict]:
        with self._lock:
            return self._snapshot_locked()

    def _update(self, item_id: str, **changes) -> bool:
        with self._lock:
            if item_id not in self._entries or item_id not in self._ranking_ids:
                return False
            self._entries[item_id].update(changes)
            self._persist_locked()
        self._notify()
        return True

    def _lookup_vod(self, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and now < self._next_vod_lookup:
            return self._vod
        self._next_vod_lookup = now + self.poll_seconds
        access_token = self.access_token
        token_file = os.environ.get("TWITCH_TOKEN_FILE", "").strip()
        if token_file:
            try:
                stored = json.loads(Path(token_file).read_text(encoding="utf-8"))
                access_token = str(stored.get("access_token", "")).strip() or access_token
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        self._vod = find_matching_vod(
            self.user_id,
            self.stream_id,
            self.stream_started_at,
            self.client_id,
            access_token,
        )
        return self._vod

    def process_once(self, force_vod_refresh: bool = False) -> None:
        with self._lock:
            pending_ids = [
                item_id
                for item_id in self._ranking_ids
                if self._entries[item_id].get("video_status") != "ready"
                or not (self.output_dir / str(
                    self._entries[item_id].get("video_path", "")
                )).is_file()
            ]
        if not pending_ids:
            return
        try:
            vod = self._lookup_vod(force=force_vod_refresh)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            for item_id in pending_ids:
                self._update(
                    item_id,
                    video_status="waiting_vod",
                    last_error=f"VOD lookup: {exc}",
                )
            return
        if vod is None:
            for item_id in pending_ids:
                self._update(
                    item_id,
                    video_status="waiting_vod",
                    last_error="",
                )
            return

        for item_id in pending_ids:
            if self._stop_requested.is_set():
                break
            with self._lock:
                if item_id not in self._entries or item_id not in self._ranking_ids:
                    continue
                entry = dict(self._entries[item_id])
            vod_fields = {
                "vod_id": str(vod.get("id", "")),
                "vod_url": str(vod.get("url", "")),
                "vod_duration_seconds": float(vod.get("duration_seconds", 0.0)),
                "vod_match_method": str(vod.get("match_method", "")),
            }
            if not vod_has_range(
                vod,
                float(entry["offset_seconds"]),
                self.duration_seconds,
                self.ready_margin_seconds,
            ):
                self._update(
                    item_id,
                    video_status="waiting_vod",
                    last_error="",
                    **vod_fields,
                )
                continue

            output_name = f"preview_vod_{item_id}.mp4"
            output_path = self.output_dir / output_name
            if not self._update(
                item_id,
                video_status="generating",
                last_error="",
                **vod_fields,
            ):
                continue
            success, error, retryable = generate_vod_clip(
                str(vod.get("url", "")),
                output_path,
                float(entry["offset_seconds"]),
                self.duration_seconds,
            )
            if success:
                retained = self._update(
                    item_id,
                    video_status="ready",
                    video_path=output_name,
                    last_error="",
                    **vod_fields,
                )
                if not retained:
                    output_path.unlink(missing_ok=True)
                    continue
                print(
                    f"[vod] ready: {self.channel} "
                    f"{entry['offset_seconds']:.1f}s ({output_name})",
                    flush=True,
                )
            else:
                self._update(
                    item_id,
                    video_status="waiting_vod" if retryable else "failed",
                    video_path="",
                    last_error=error[-500:],
                    **vod_fields,
                )

    def mark_unavailable(self) -> None:
        with self._lock:
            item_ids = [
                item_id
                for item_id in self._ranking_ids
                if self._entries[item_id].get("video_status")
                in {"waiting_vod", "generating"}
            ]
        for item_id in item_ids:
            self._update(item_id, video_status="unavailable")

    def run(self) -> None:
        while not self._stop_requested.is_set():
            self._wake.wait(timeout=self.poll_seconds)
            self._wake.clear()
            if self._stop_requested.is_set():
                break
            self.process_once()

    def stop(self) -> None:
        self._stop_requested.set()
        self._wake.set()


__all__ = [
    "VOD_QUALITY_FALLBACK",
    "VodClipManager",
    "build_vod_streamlink_command",
    "candidate_id",
    "find_matching_vod",
    "generate_vod_clip",
    "parse_twitch_duration",
    "vod_has_range",
]
