import argparse
import functools
import html
import http.server
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import wave
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# The Xet downloader can stall on some Windows environments. Use the regular
# Hugging Face HTTP downloader unless the user explicitly configured otherwise.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from faster_whisper.vad import VadOptions, get_speech_timestamps

from vod_clip_manager import VodClipManager, candidate_id as make_candidate_id

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
LIVE_AUDIO_QUALITY = "audio_only"
OUTPUT_VIDEO_HEIGHT = 720
HIGHLIGHT_SECONDS = 30.0
CLIP_MIN_SECONDS = 30.0
CLIP_MAX_SECONDS = 140.0
CLIP_MARGIN_SECONDS = 1.0
UTTERANCE_GAP_SECONDS = 3.5
BUFFER_SAFETY_SECONDS = 30.0
BUFFER_SEGMENT_SAFETY_SECONDS = 10.0
ROLLING_BUFFER_SECONDS = HIGHLIGHT_SECONDS + BUFFER_SAFETY_SECONDS
TOP_HIGHLIGHT_COUNT = 10
PREVIEW_INTERVAL_SECONDS = 60.0
JST = timezone(timedelta(hours=9), "JST")


def normalize_channel(value):
    channel = value.strip()
    channel = re.sub(r"^https?://(?:www\.)?twitch\.tv/", "", channel, flags=re.I)
    channel = channel.split("?", 1)[0].split("#", 1)[0].strip("/@# ")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel):
        raise ValueError("配信者IDは英数字とアンダースコアで入力してください")
    return channel.lower()


def now_ts():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, tz=JST).isoformat(timespec="milliseconds")


def hhmmss(ts):
    return datetime.fromtimestamp(ts, tz=JST).strftime("%H:%M:%S")


def format_elapsed(seconds):
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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
        print("[video] waiting for shared encoder slot", flush=True)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_jsonl(path, obj):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class JsonlTail:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = b""

    def read_new(self):
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
            self.partial = b""
        with self.path.open("rb") as source:
            source.seek(self.offset)
            data = source.read()
            self.offset += len(data)
        if not data:
            return []
        parts = (self.partial + data).split(b"\n")
        self.partial = parts.pop()
        rows = []
        for raw in parts:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw.decode("utf-8")))
        return rows


class QuietPreviewHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        self._byte_range = None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            source = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        stat = os.fstat(source.fileno())
        content_type = self.guess_type(path)
        range_header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match:
            first, last = match.groups()
            if not first and not last:
                source.close()
                self.send_error(416, "Invalid range")
                return None
            if first:
                start = int(first)
                end = int(last) if last else stat.st_size - 1
            else:
                length = int(last)
                start = max(0, stat.st_size - length)
                end = stat.st_size - 1
            if start >= stat.st_size or start > end:
                source.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat.st_size}")
                self.end_headers()
                return None
            end = min(end, stat.st_size - 1)
            self._byte_range = (start, end)
            source.seek(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
            content_length = end - start + 1
        else:
            self.send_response(200)
            content_length = stat.st_size
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        if path.lower().endswith((".html", ".json")):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return source

    def copyfile(self, source, outputfile):
        if self._byte_range is None:
            return super().copyfile(source, outputfile)
        start, end = self._byte_range
        remaining = end - start + 1
        while remaining > 0:
            chunk = source.read(min(256 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def log_message(self, _format, *_args):
        pass


class PreviewServer:
    def __init__(self, directory, preferred_port=8765):
        handler = functools.partial(
            QuietPreviewHandler, directory=str(directory.resolve())
        )
        self.lan_ip = self._detect_lan_ip()
        try:
            self.server = http.server.ThreadingHTTPServer(
                ("0.0.0.0", preferred_port), handler
            )
        except OSError:
            self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _detect_lan_ip():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            probe.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/reactions.html"

    @property
    def phone_url(self):
        return f"http://{self.lan_ip}:{self.server.server_port}/reactions.html"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def extract_audio_clip(source_path, output_path, start_seconds, end_seconds):
    with wave.open(str(source_path), "rb") as source:
        frame_rate = source.getframerate()
        start_frame = max(0, math.floor(start_seconds * frame_rate))
        end_frame = min(source.getnframes(), math.ceil(end_seconds * frame_rate))
        if end_frame <= start_frame:
            return False

        source.setpos(start_frame)
        frames = source.readframes(end_frame - start_frame)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as destination:
            destination.setnchannels(source.getnchannels())
            destination.setsampwidth(source.getsampwidth())
            destination.setframerate(frame_rate)
            destination.setcomptype(source.getcomptype(), source.getcompname())
            destination.writeframes(frames)
    return True


def read_vad_audio(source_path):
    with wave.open(str(source_path), "rb") as source:
        if (source.getnchannels() != 1 or source.getsampwidth() != 2
                or source.getframerate() != 16000):
            raise ValueError("VAD requires mono 16-bit PCM audio at 16 kHz")
        frames = source.readframes(source.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def merge_transcript_rows(rows, gap_seconds):
    merged = []
    for source_row in rows:
        row = dict(source_row)
        row.pop("audio_clip", None)
        previous = merged[-1] if merged else None
        can_merge = (
            previous is not None
            and int(previous["chunk"]) == int(row["chunk"])
            and row["segment_start"] - previous["segment_end"] <= gap_seconds
        )
        if not can_merge:
            merged.append(row)
            continue

        previous["ts_end"] = row["ts_end"]
        previous["iso_end"] = row["iso_end"]
        previous["segment_end"] = row["segment_end"]
        previous["text"] = f'{previous["text"].rstrip()} {row["text"].lstrip()}'
    return merged


def regroup_existing_session(session_dir, gap_seconds):
    transcript_path = session_dir / "transcript.jsonl"
    transcripts = load_jsonl(transcript_path)
    backup_path = session_dir / "transcript.detailed.jsonl"
    if not backup_path.exists():
        shutil.copy2(transcript_path, backup_path)

    transcripts = merge_transcript_rows(transcripts, gap_seconds)
    clip_dir = session_dir / "utterance_clips"
    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for row_index, row in enumerate(transcripts):
        chunk_index = int(row["chunk"])
        wav = session_dir / "audio_chunks" / f"chunk_{chunk_index:06d}.wav"
        if not wav.exists():
            continue
        clip_name = f"utterance_{row_index:06d}.wav"
        clip_path = clip_dir / clip_name
        if extract_audio_clip(wav, clip_path, row["segment_start"], row["segment_end"]):
            row["audio_clip"] = f"utterance_clips/{clip_name}"
            created += 1

    temp_path = transcript_path.with_suffix(".jsonl.tmp")
    with temp_path.open("w", encoding="utf-8") as output:
        for row in transcripts:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp_path.replace(transcript_path)
    return created


def media_interval(row, segment_seconds):
    if "offset_start" in row and "offset_end" in row:
        return float(row["offset_start"]), float(row["offset_end"])
    chunk_base = int(row["chunk"]) * segment_seconds
    return chunk_base + row["segment_start"], chunk_base + row["segment_end"]


def select_dense_window(transcripts, segment_seconds, window_seconds, recording_seconds):
    intervals = [media_interval(row, segment_seconds) for row in transcripts]
    max_start = max(0.0, recording_seconds - window_seconds)
    best_start = 0.0
    best_score = -1.0
    steps = max(1, math.ceil(max_start * 4))
    for step in range(steps + 1):
        start = min(max_start, step / 4)
        end = start + window_seconds
        score = sum(max(0.0, min(interval_end, end) - max(interval_start, start))
                    for interval_start, interval_end in intervals)
        if score > best_score:
            best_start = start
            best_score = score
    return best_start, max(0.0, best_score)


def select_chat_window(chats, recording_started_at, window_seconds, recording_seconds):
    offsets = [row["ts"] - recording_started_at for row in chats]
    offsets = [offset for offset in offsets if 0 <= offset <= recording_seconds]
    max_start = max(0.0, recording_seconds - window_seconds)
    best_start = 0.0
    best_count = -1
    steps = max(1, math.ceil(max_start * 4))
    for step in range(steps + 1):
        start = min(max_start, step / 4)
        end = start + window_seconds
        count = sum(start <= offset <= end for offset in offsets)
        if count > best_count:
            best_start = start
            best_count = count
    return best_start, max(0, best_count)


def find_chat_trigger(transcripts, segment_seconds, peak_start, max_lookback=12.0):
    intervals = sorted(
        ((*media_interval(row, segment_seconds), row) for row in transcripts),
        key=lambda item: (item[0], item[1]),
    )
    candidates = []
    for start, end, row in intervals:
        if start <= peak_start <= end + 1.0:
            candidates.append((start, end, row))
    if candidates:
        target = max(candidates, key=lambda item: item[0])
    else:
        preceding = [
            item for item in intervals
            if item[1] <= peak_start and peak_start - item[1] <= max_lookback
        ]
        if not preceding:
            return max(0.0, peak_start - 3.0), None
        target = max(preceding, key=lambda item: item[1])

    # SpeechDetector has already merged every utterance chain whose gaps are
    # at most --utterance-gap-seconds.  Do not apply another lookback here:
    # it would join distinct utterances separated by a longer silence.
    target_start, _target_end, target_row = target
    return target_start, target_row


def extend_trigger_start_backward(
        speech_segments, trigger_start, gap_seconds, preceding_count,
        return_linked_count=False, return_linked_detail=False):
    """Add N preceding utterances; zero preserves unlimited legacy lookback."""
    intervals = sorted(
        (
            float(row["offset_start"]),
            float(row["offset_end"]),
        )
        for row in speech_segments
        if "offset_start" in row and "offset_end" in row
    )
    start = float(trigger_start)
    included = 0
    reason = "前方に連結対象なし"
    for previous_start, previous_end in reversed(intervals):
        if previous_end > start + 0.001:
            continue
        if start - previous_end > gap_seconds:
            reason = f"Speech gap {gap_seconds:g}秒を超過"
            break
        start = previous_start
        included += 1
        # The setting counts utterances *before* the trigger.  Zero retains
        # the previous unlimited behaviour.
        if preceding_count > 0 and included >= preceding_count:
            reason = f"前方上限 {preceding_count}個に到達"
            break
    else:
        if included:
            reason = "連結できる前方発話の先頭"
    if return_linked_detail:
        return start, included, reason
    if return_linked_count:
        return start, included
    return start


def count_chats_in_window(chats, recording_started_at, start, duration):
    end = start + duration
    return sum(start <= row["ts"] - recording_started_at <= end for row in chats)


def select_top_chat_triggers(transcripts, chats, recording_started_at,
                             segment_seconds, window_seconds,
                             recording_seconds, limit=3):
    max_start = max(0.0, recording_seconds - window_seconds)
    unique = {}
    steps = max(1, math.ceil(max_start * 4))
    for step in range(steps + 1):
        peak_start = min(max_start, step / 4)
        trigger_start, trigger_row = find_chat_trigger(
            transcripts, segment_seconds, peak_start
        )
        trigger_start = min(
            max(0.0, trigger_start - CLIP_MARGIN_SECONDS), max_start
        )
        key = round(trigger_start, 3)
        chat_count = count_chats_in_window(
            chats, recording_started_at, trigger_start, window_seconds
        )
        item = {
            "trigger_start": trigger_start,
            "trigger_text": trigger_row.get("text", "") if trigger_row else "",
            "chat_count": chat_count,
        }
        if key not in unique or chat_count > unique[key]["chat_count"]:
            unique[key] = item

    selected = []
    for item in sorted(unique.values(), key=lambda row: row["chat_count"], reverse=True):
        start = item["trigger_start"]
        end = start + window_seconds
        overlaps = any(
            not (end <= chosen["trigger_start"]
                 or start >= chosen["trigger_start"] + window_seconds)
            for chosen in selected
        )
        if overlaps:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def transcripts_in_window(transcripts, segment_seconds, start, duration):
    end = start + duration
    return [
        row for row in transcripts
        if media_interval(row, segment_seconds)[1] >= start
        and media_interval(row, segment_seconds)[0] <= end
    ]


def create_video_highlight(source_path, output_path, start_seconds, duration_seconds):
    if not source_path.exists() or source_path.stat().st_size == 0:
        return False
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_seconds:.3f}", "-i", str(source_path),
        "-t", f"{duration_seconds:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{OUTPUT_VIDEO_HEIGHT}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output_path),
    ]
    with encoding_slot():
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=180
        )
    if result.returncode != 0:
        print(f"[video] highlight failed: {result.stderr.strip()}")
        return False
    print(f"[video] highlight: {start_seconds:.1f}-{start_seconds + duration_seconds:.1f}s")
    return True


def create_preview_highlight(source_path, output_path, start_seconds, duration_seconds):
    if not source_path.exists() or source_path.stat().st_size == 0:
        return False
    with encoding_slot():
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_seconds:.3f}", "-i", str(source_path),
                "-t", f"{duration_seconds:.3f}",
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
    if result.returncode != 0:
        print(f"[preview] video failed: {result.stderr.strip()}")
        return False
    return True


def resolve_dynamic_clip_duration(
        speech_segments, clip_start, known_offset,
        minimum_seconds=CLIP_MIN_SECONDS,
        maximum_seconds=CLIP_MAX_SECONDS,
        silence_seconds=UTTERANCE_GAP_SECONDS,
        merge_gap_seconds=None,
        confirmation_silence_seconds=None,
        end_margin_seconds=CLIP_MARGIN_SECONDS):
    """Return a confirmed clip duration, or None while more audio is needed."""
    min_end = clip_start + minimum_seconds
    hard_end = clip_start + maximum_seconds
    known_offset = max(clip_start, float(known_offset))

    if known_offset < min_end:
        return None, "waiting_minimum", None

    intervals = sorted(
        (
            float(row["offset_start"]),
            float(row["offset_end"]),
        )
        for row in speech_segments
        if "offset_start" in row and "offset_end" in row
    )
    merge_gap_seconds = (
        silence_seconds if merge_gap_seconds is None else merge_gap_seconds
    )
    confirmation_silence_seconds = (
        silence_seconds
        if confirmation_silence_seconds is None
        else confirmation_silence_seconds
    )
    merged = []
    for start, end in intervals:
        if not merged or start - merged[-1][1] > merge_gap_seconds:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    active = next(
        (
            interval for interval in merged
            if interval[0] <= min_end <= interval[1]
        ),
        None,
    )

    if active is not None:
        speech_end = active[1]
        # 最短地点までに発話が途切れていれば、クリップは最短長で確定。
        # 30秒地点を越えて発話が続いた場合だけ自然終了へ延長する。
        if speech_end <= min_end:
            confirm_at = speech_end + confirmation_silence_seconds
            if known_offset < confirm_at:
                return None, "waiting_silence", speech_end
            return minimum_seconds, "natural_at_minimum", speech_end
        if (
            speech_end >= hard_end
            or speech_end + confirmation_silence_seconds > hard_end
        ):
            if known_offset < hard_end:
                return None, "waiting_hard_max", speech_end
            return maximum_seconds, "hard_max", speech_end

        clip_end = min(
            hard_end,
            max(min_end, speech_end + end_margin_seconds),
        )
        confirm_at = max(
            speech_end + confirmation_silence_seconds,
            clip_end,
        )
        if known_offset < confirm_at:
            return None, "waiting_silence", speech_end
        return clip_end - clip_start, "natural", speech_end

    previous = None
    for interval in merged:
        if interval[1] <= min_end:
            previous = interval
        else:
            break

    if previous is None:
        return minimum_seconds, "natural_silence_at_minimum", None

    speech_end = previous[1]
    clip_end = min(
        hard_end,
        max(min_end, speech_end + end_margin_seconds),
    )
    confirm_at = max(
        min_end,
        speech_end + confirmation_silence_seconds,
        clip_end,
    )
    if confirm_at > hard_end:
        if known_offset < hard_end:
            return None, "waiting_hard_max", speech_end
        return maximum_seconds, "hard_max", speech_end
    if known_offset < confirm_at:
        return None, "waiting_silence", speech_end
    return clip_end - clip_start, "natural_at_minimum", speech_end


def resolve_triggered_clip_duration(
        speech_segments, clip_start, trigger_speech_end, known_offset,
        minimum_seconds=CLIP_MIN_SECONDS,
        maximum_seconds=CLIP_MAX_SECONDS,
        first_follow_gap_seconds=UTTERANCE_GAP_SECONDS,
        gap_follow_up_count=1,
        subsequent_gap_seconds=1.0,
        end_margin_seconds=CLIP_MARGIN_SECONDS,
        return_linked_count=False, return_linked_detail=False):
    """End a hype clip after one long-gap follow-up, then one-second gaps.

    A follow-up is a whole utterance, not a raw VAD fragment.  Fragments that
    are separated by less than the tail gap stay within the current utterance
    and do not consume the follow-up count.  A follow-up count of zero
    preserves the legacy behaviour: every following utterance may start within
    the configured speech gap.
    """
    min_end = clip_start + minimum_seconds
    hard_end = clip_start + maximum_seconds
    known_offset = max(clip_start, float(known_offset))
    gap_follow_ups_seen = 0
    tail_reason = "後続発話なし"

    def outcome(duration, reason, speech_end):
        detail = tail_reason
        if reason == "hard_max":
            detail = f"動画上限 {maximum_seconds:g}秒に到達"
        if return_linked_count:
            if return_linked_detail:
                return duration, reason, speech_end, gap_follow_ups_seen, detail
            return duration, reason, speech_end, gap_follow_ups_seen
        return duration, reason, speech_end

    if known_offset < min_end:
        return outcome(None, "waiting_minimum", trigger_speech_end)

    intervals = sorted(
        (
            float(row["offset_start"]),
            float(row["offset_end"]),
        )
        for row in speech_segments
        if "offset_start" in row and "offset_end" in row
    )
    speech_end = float(trigger_speech_end)
    for start, end in intervals:
        if end <= speech_end + 0.001:
            continue
        gap = start - speech_end
        # This is still the current utterance; VAD may split a spoken sentence
        # into several rows, but those rows must not consume the count.
        if gap < subsequent_gap_seconds:
            speech_end = max(speech_end, end)
            continue

        # A pause at least as long as the tail gap starts another utterance.
        # It may use Speech gap while the configured number remains.
        if (gap_follow_up_count > 0
                and gap_follow_ups_seen >= gap_follow_up_count):
            tail_reason = (
                f"後続上限 {gap_follow_up_count}個の後、"
                f"{subsequent_gap_seconds:g}秒以上の無音"
            )
            break
        if gap >= first_follow_gap_seconds:
            tail_reason = f"Speech gap {first_follow_gap_seconds:g}秒以上の無音"
            break
        speech_end = max(speech_end, end)
        gap_follow_ups_seen += 1
        tail_reason = "後続発話を連結中"

    decision_gap = (
        first_follow_gap_seconds
        if gap_follow_up_count == 0 or gap_follow_ups_seen < gap_follow_up_count
        else subsequent_gap_seconds
    )
    if speech_end >= hard_end or speech_end + decision_gap > hard_end:
        if known_offset < hard_end:
            return outcome(None, "waiting_hard_max", speech_end)
        return outcome(maximum_seconds, "hard_max", speech_end)

    clip_end = min(hard_end, max(min_end, speech_end + end_margin_seconds))
    # The minimum duration can reach into a later, unrelated speech block.
    # Never cut the rendered video in the middle of a VAD-detected utterance:
    # extend that boundary utterance to its end and preserve the end margin.
    boundary_speech_end = max(
        (
            end for start, end in intervals
            if start < clip_end and end >= clip_end
        ),
        default=None,
    )
    if boundary_speech_end is not None:
        speech_end = max(speech_end, boundary_speech_end)
        # Once the minimum-duration boundary lands inside speech, keep
        # chaining later speech while its pause is shorter than the dedicated
        # post-boundary interval. This avoids ending in the middle of a phrase
        # split into multiple VAD utterances around the 30-second mark.
        for start, end in intervals:
            if end <= speech_end + 0.001:
                continue
            if start - speech_end >= subsequent_gap_seconds:
                break
            speech_end = max(speech_end, end)
        decision_gap = subsequent_gap_seconds
        if speech_end >= hard_end or speech_end + decision_gap > hard_end:
            if known_offset < hard_end:
                return outcome(None, "waiting_hard_max", speech_end)
            return outcome(maximum_seconds, "hard_max", speech_end)
        clip_end = min(
            hard_end,
            max(clip_end, boundary_speech_end + end_margin_seconds),
        )
        clip_end = min(
            hard_end,
            max(clip_end, speech_end + end_margin_seconds),
        )
        tail_reason = (
            "最短尺の終端に発話が重なり、その後の発話間隔未満を連結"
        )
    confirm_at = max(clip_end, speech_end + decision_gap)
    if known_offset < confirm_at:
        return outcome(None, "waiting_silence", speech_end)
    return outcome(clip_end - clip_start, "natural", speech_end)


class RealtimeHighlightManager:
    """Keep the existing VAD/chat ranking logic, but store offsets, not video."""

    def __init__(self, chat_path, transcript_path, recording_started_at,
                 segment_seconds, recording_seconds,
                 timeline_offset_seconds=0.0, stream_id="",
                 highlight_seconds=HIGHLIGHT_SECONDS,
                 preroll_seconds=CLIP_MARGIN_SECONDS,
                 buffer_seconds=ROLLING_BUFFER_SECONDS, candidate_limit=8,
                 speech_provider=None,
                 clip_min_seconds=CLIP_MIN_SECONDS,
                 clip_max_seconds=CLIP_MAX_SECONDS,
                 clip_end_silence_seconds=1.0,
                 clip_end_margin_seconds=CLIP_MARGIN_SECONDS):
        self.chat_path = chat_path
        self.transcript_path = transcript_path
        self.recording_started_at = recording_started_at
        self.segment_seconds = segment_seconds
        self.recording_seconds = recording_seconds
        self.timeline_offset_seconds = timeline_offset_seconds
        self.stream_id = stream_id
        self.highlight_seconds = highlight_seconds
        self.preroll_seconds = preroll_seconds
        self.buffer_seconds = buffer_seconds
        self.candidate_limit = candidate_limit
        self.speech_provider = speech_provider
        self.clip_min_seconds = clip_min_seconds
        self.clip_max_seconds = clip_max_seconds
        self.clip_end_silence_seconds = clip_end_silence_seconds
        self.clip_end_margin_seconds = clip_end_margin_seconds
        self.candidates = []
        self.last_window_end = None
        self.chat_tail = JsonlTail(chat_path)
        self.speech_tail = JsonlTail(transcript_path)
        self.chats = []
        self.speech_segments = []

    def _duration_speech_state(self, current_offset):
        if self.speech_provider is not None:
            return self.speech_provider.snapshot()
        return list(self.speech_segments), current_offset

    def _update_candidate_duration(self, item, current_offset, force=False):
        rows, known_offset = self._duration_speech_state(current_offset)
        if (
            self.speech_provider is not None
            and item.get("trigger_speech_end") is not None
        ):
            raw_rows, known_offset = self.speech_provider.raw_snapshot()
            clip_margin_seconds = self.speech_provider.clip_margin_seconds()
            (duration, reason, speech_end, linked_follow_up_count,
             linked_follow_up_reason) = resolve_triggered_clip_duration(
                raw_rows,
                float(item["trigger_start"]),
                float(item["trigger_speech_end"]),
                known_offset,
                minimum_seconds=self.clip_min_seconds,
                maximum_seconds=self.clip_max_seconds,
                first_follow_gap_seconds=self.speech_provider.current_gap_seconds(),
                gap_follow_up_count=self.speech_provider.gap_follow_up_count(),
                subsequent_gap_seconds=self.speech_provider.tail_gap_seconds(),
                end_margin_seconds=clip_margin_seconds,
                return_linked_count=True,
                return_linked_detail=True,
            )
        else:
            clip_margin_seconds = self.clip_end_margin_seconds
            duration, reason, speech_end = resolve_dynamic_clip_duration(
                rows,
                float(item["trigger_start"]),
                known_offset,
                minimum_seconds=self.clip_min_seconds,
                maximum_seconds=self.clip_max_seconds,
                silence_seconds=self.clip_end_silence_seconds,
                end_margin_seconds=clip_margin_seconds,
            )
            linked_follow_up_count = 0
            linked_follow_up_reason = "音声発話の連結情報なし"
        if duration is None and force:
            available = min(
                self.clip_max_seconds,
                max(0.0, known_offset - float(item["trigger_start"])),
            )
            if available >= self.clip_min_seconds:
                duration = available
                reason = "capture_end"
        previous = (
            item.get("duration_seconds"),
            item.get("clip_end_status"),
            item.get("clip_end_reason"),
            item.get("clip_last_speech_end"),
            item.get("linked_follow_up_count"),
            item.get("linked_follow_up_reason"),
            item.get("decision_clip_margin_seconds"),
        )
        item["duration_seconds"] = (
            round(float(duration), 3) if duration is not None else 0.0
        )
        item["clip_end_status"] = "ready" if duration is not None else "waiting"
        item["clip_end_reason"] = reason
        item["clip_last_speech_end"] = speech_end
        item["linked_follow_up_count"] = linked_follow_up_count
        item["linked_follow_up_reason"] = linked_follow_up_reason
        item["decision_clip_margin_seconds"] = clip_margin_seconds
        current = (
            item["duration_seconds"],
            item["clip_end_status"],
            item["clip_end_reason"],
            item["clip_last_speech_end"],
            item["linked_follow_up_count"],
            item["linked_follow_up_reason"],
            item["decision_clip_margin_seconds"],
        )
        return current != previous

    def _refresh_candidate_durations(self, current_offset, force=False):
        changed = False
        for item in self.candidates:
            if item.get("clip_end_status") == "ready":
                continue
            changed = (
                self._update_candidate_duration(item, current_offset, force=force)
                or changed
            )
        return changed

    def _refresh_events(self, current_offset):
        self.chats.extend(self.chat_tail.read_new())
        self.speech_segments.extend(self.speech_tail.read_new())
        keep_after = max(0.0, current_offset - self.buffer_seconds - 10.0)
        keep_after_ts = self.recording_started_at + keep_after
        self.chats = [row for row in self.chats if row["ts"] >= keep_after_ts]
        self.speech_segments = [
            row for row in self.speech_segments
            if media_interval(row, self.segment_seconds)[1] >= keep_after
        ]

    def evaluate(self, current_offset, force=False):
        self._refresh_events(current_offset)
        duration_changed = self._refresh_candidate_durations(
            current_offset,
            force=force,
        )
        safe_end = current_offset if force else current_offset - 5.0
        if safe_end < self.highlight_seconds:
            return duration_changed
        window_end = safe_end if force else math.floor(safe_end / 5.0) * 5.0
        window_end = min(window_end, self.recording_seconds)
        if self.last_window_end is not None and window_end <= self.last_window_end:
            return duration_changed
        self.last_window_end = window_end
        peak_start = max(0.0, window_end - self.highlight_seconds)

        chats = self.chats
        transcripts = self.speech_segments
        trigger_start, trigger_row = find_chat_trigger(
            transcripts, self.segment_seconds, peak_start,
        )
        trigger_speech_end = (
            media_interval(trigger_row, self.segment_seconds)[1]
            if trigger_row is not None
            else None
        )
        trigger_speech_start = trigger_start
        linked_preceding_count = 0
        linked_preceding_reason = "前方に連結対象なし"
        decision_speech_gap_seconds = self.clip_end_silence_seconds
        decision_tail_gap_seconds = self.clip_end_silence_seconds
        decision_preceding_limit = 0
        decision_follow_up_limit = 0
        clip_margin_seconds = self.preroll_seconds
        decision_vad_threshold = 0.5
        decision_vad_min_speech_seconds = 0.15
        decision_vad_min_silence_seconds = 0.5
        if self.speech_provider is not None:
            raw_rows, _ = self.speech_provider.raw_snapshot()
            decision_speech_gap_seconds = self.speech_provider.current_gap_seconds()
            decision_tail_gap_seconds = self.speech_provider.tail_gap_seconds()
            decision_preceding_limit = self.speech_provider.gap_preceding_count()
            decision_follow_up_limit = self.speech_provider.gap_follow_up_count()
            clip_margin_seconds = self.speech_provider.clip_margin_seconds()
            decision_vad_threshold = self.speech_provider.vad_threshold()
            decision_vad_min_speech_seconds = (
                self.speech_provider.vad_min_speech_seconds()
            )
            decision_vad_min_silence_seconds = (
                self.speech_provider.vad_min_silence_seconds()
            )
            raw_trigger_start, raw_trigger_row = find_chat_trigger(
                raw_rows, self.segment_seconds, peak_start,
            )
            if raw_trigger_row is not None:
                trigger_speech_start = raw_trigger_start
                (trigger_start, linked_preceding_count,
                 linked_preceding_reason) = extend_trigger_start_backward(
                    raw_rows,
                    raw_trigger_start,
                    decision_speech_gap_seconds,
                    decision_preceding_limit,
                    return_linked_detail=True,
                )
                trigger_speech_end = media_interval(
                    raw_trigger_row,
                    self.segment_seconds,
                )[1]
        trigger_start = min(
            max(0.0, trigger_start - clip_margin_seconds),
            max(0.0, self.recording_seconds - self.highlight_seconds),
        )
        chat_count = count_chats_in_window(
            chats, self.recording_started_at, trigger_start,
            self.highlight_seconds,
        )

        offset_seconds = max(
            0.0, trigger_start + self.timeline_offset_seconds
        )
        item_id = make_candidate_id(self.stream_id, offset_seconds)
        duplicate = next(
            (
                item for item in self.candidates
                if item["candidate_id"] == item_id
            ),
            None,
        )
        if duplicate and chat_count <= duplicate["chat_count"]:
            return duration_changed
        if duplicate:
            self.candidates.remove(duplicate)

        if (len(self.candidates) >= self.candidate_limit
                and chat_count <= min(item["chat_count"] for item in self.candidates)):
            return duration_changed
        item = {
            "candidate_id": item_id,
            "trigger_start": trigger_start,
            "offset_seconds": offset_seconds,
            "trigger_text": trigger_row.get("text", "") if trigger_row else "",
            "trigger_speech_video_seconds": round(
                max(0.0, trigger_speech_start - trigger_start), 3
            ),
            "linked_preceding_count": linked_preceding_count,
            "linked_preceding_reason": linked_preceding_reason,
            "decision_speech_gap_seconds": decision_speech_gap_seconds,
            "decision_tail_gap_seconds": decision_tail_gap_seconds,
            "decision_clip_margin_seconds": clip_margin_seconds,
            "decision_preceding_limit": decision_preceding_limit,
            "decision_follow_up_limit": decision_follow_up_limit,
            "decision_vad_threshold": decision_vad_threshold,
            "decision_vad_min_speech_seconds": decision_vad_min_speech_seconds,
            "decision_vad_min_silence_seconds": decision_vad_min_silence_seconds,
            "trigger_speech_end": trigger_speech_end,
            "chat_count": chat_count,
            "score": chat_count,
        }
        self._update_candidate_duration(item, current_offset, force=force)
        self.candidates.append(item)
        while len(self.candidates) > self.candidate_limit:
            loser = min(self.candidates, key=lambda row: row["chat_count"])
            self.candidates.remove(loser)
        print(
            f"[ranking] candidate: stream+{offset_seconds:.1f}s / "
            f"{chat_count} chats"
        )
        return True

    def top_non_overlapping(self, limit=TOP_HIGHLIGHT_COUNT):
        selected = []
        selected_intervals = []
        ranked = []
        for item in sorted(self.candidates, key=lambda row: row["chat_count"], reverse=True):
            ranked.extend(self._hard_max_continuations(item))
        for item in ranked:
            start = item["trigger_start"]
            duration = float(item.get("duration_seconds") or 0.0)
            if item.get("clip_end_status") != "ready" or duration <= 0:
                duration = self.clip_max_seconds
            end = start + duration
            if any(not (end <= chosen_start or start >= chosen_end)
                   for chosen_start, chosen_end in selected_intervals):
                continue
            selected.append(item)
            selected_intervals.append((start, end))
            if len(selected) >= limit:
                break
        return selected

    def _hard_max_continuations(self, item):
        """Split a still-speaking hard-max clip into consecutive clips.

        A long uninterrupted utterance must not lose its tail merely because
        the first highlight reached the social-video length cap.
        """
        parts = [item]
        if (
            item.get("clip_end_status") != "ready"
            or item.get("clip_end_reason") != "hard_max"
        ):
            return parts
        try:
            start = float(item["trigger_start"])
            duration = float(item["duration_seconds"])
            speech_end = float(item["clip_last_speech_end"])
        except (KeyError, TypeError, ValueError):
            return parts
        if duration <= 0 or speech_end <= start + duration:
            return parts

        continuation_start = start + duration
        clip_margin_seconds = self.clip_end_margin_seconds
        if self.speech_provider is not None:
            clip_margin_seconds = self.speech_provider.clip_margin_seconds()
        final_end = speech_end + clip_margin_seconds
        while continuation_start < final_end - 0.001:
            continuation_duration = min(
                self.clip_max_seconds,
                final_end - continuation_start,
            )
            if continuation_duration < self.clip_min_seconds:
                break
            offset_seconds = continuation_start + self.timeline_offset_seconds
            continuation = dict(item)
            continuation.update(
                {
                    "candidate_id": make_candidate_id(
                        self.stream_id,
                        offset_seconds,
                    ),
                    "trigger_start": continuation_start,
                    "offset_seconds": offset_seconds,
                    "duration_seconds": round(continuation_duration, 3),
                    "clip_end_reason": "hard_max_continuation",
                    "continuation_of": item.get("candidate_id", ""),
                }
            )
            parts.append(continuation)
            continuation_start += continuation_duration
        return parts


def parse_irc_tags(raw):
    tags = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k] = v
        else:
            tags[part] = ""
    return tags


def unescape_irc_tag(v):
    return (v.replace(r"\s", " ")
             .replace(r"\:", ";")
             .replace(r"\\", "\\")
             .replace(r"\r", "\r")
             .replace(r"\n", "\n"))


class TwitchChatRecorder(threading.Thread):
    def __init__(self, channel, nick, oauth_token, out_path, stop_event):
        super().__init__(daemon=True)
        self.channel = channel.lower().lstrip("#")
        self.nick = nick.lower()
        self.oauth_token = oauth_token
        self.out_path = out_path
        self.stop_event = stop_event
        self.sock = None

    def send(self, text):
        self.sock.sendall((text + "\r\n").encode("utf-8"))

    def run(self):
        ctx = ssl.create_default_context()
        raw_sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=15)
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=IRC_HOST)
        self.sock.settimeout(1.0)

        token = self.oauth_token
        if token.startswith("oauth:"):
            token = token[6:]

        self.send(f"PASS oauth:{token}")
        self.send(f"NICK {self.nick}")
        self.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        self.send(f"JOIN #{self.channel}")
        print(f"[chat] joined #{self.channel}")

        buf = ""
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.sock.recv(65536)
                    if not data:
                        break
                except socket.timeout:
                    continue

                buf += data.decode("utf-8", errors="replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING"):
                        self.send("PONG :tmi.twitch.tv")
                        continue
                    if "Login authentication failed" in line:
                        print("[chat] authentication failed")
                        self.stop_event.set()
                        return

                    m = re.match(r"^@([^ ]+) :([^!]+)![^ ]+ PRIVMSG #[^ ]+ :(.*)$", line)
                    if not m:
                        continue

                    tags = {k: unescape_irc_tag(v) for k, v in parse_irc_tags(m.group(1)).items()}
                    login = m.group(2)
                    text = m.group(3)
                    ts = now_ts()
                    sent_ts = tags.get("tmi-sent-ts")
                    if sent_ts and sent_ts.isdigit():
                        ts = int(sent_ts) / 1000.0

                    append_jsonl(self.out_path, {
                        "ts": ts,
                        "iso": iso(ts),
                        "login": login,
                        "display_name": tags.get("display-name") or login,
                        "text": text,
                        "badges": tags.get("badges", ""),
                        "msg_id": tags.get("id", ""),
                    })
        finally:
            try:
                self.sock.close()
            except Exception:
                pass


def build_live_audio_streamlink_command(channel_url):
    return [
        "streamlink",
        "--ringbuffer-size",
        "4M",
        "--stdout",
        channel_url,
        LIVE_AUDIO_QUALITY,
    ]


def build_live_audio_ffmpeg_command(chunk_dir, segment_seconds):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", "pipe:0",
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        str(chunk_dir / "chunk_%06d.wav"),
    ]


class AudioOnlyCapture:
    def __init__(self, channel_url, chunk_dir, segment_seconds):
        self.channel_url = channel_url
        self.chunk_dir = chunk_dir
        self.segment_seconds = segment_seconds
        self.streamlink = None
        self.ffmpeg = None
        self.started_at = None

    def start(self):
        if not shutil.which("streamlink"):
            raise RuntimeError("streamlink が見つかりません。pip install streamlink を実行してください。")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg が PATH にありません。")

        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.streamlink = subprocess.Popen(
            build_live_audio_streamlink_command(self.channel_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.ffmpeg = subprocess.Popen(
            build_live_audio_ffmpeg_command(
                self.chunk_dir, self.segment_seconds
            ),
            stdin=self.streamlink.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.streamlink.stdout.close()
        first_chunk = self.chunk_dir / "chunk_000000.wav"
        ready_deadline = time.monotonic() + 30
        while not first_chunk.exists():
            if self.ffmpeg.poll() is not None:
                error = self.ffmpeg.stderr.read().strip() if self.ffmpeg.stderr else ""
                raise RuntimeError(f"ffmpeg failed to start capture: {error}")
            if time.monotonic() >= ready_deadline:
                raise RuntimeError("audio_onlyの受信開始を30秒待ちましたが、データが届きませんでした。")
            time.sleep(0.1)
        self.started_at = first_chunk.stat().st_ctime
        print("[media] audio_only capture started")

    def stop(self):
        # Stop the source first. Its closed pipe lets ffmpeg finish and write a
        # valid header for the final partial WAV segment.
        if self.streamlink and self.streamlink.poll() is None:
            try:
                self.streamlink.terminate()
                self.streamlink.wait(timeout=5)
            except Exception:
                try:
                    self.streamlink.kill()
                except Exception:
                    pass

        if self.ffmpeg and self.ffmpeg.poll() is None:
            try:
                self.ffmpeg.wait(timeout=5)
            except Exception:
                try:
                    self.ffmpeg.terminate()
                    self.ffmpeg.wait(timeout=5)
                except Exception:
                    try:
                        self.ffmpeg.kill()
                    except Exception:
                        pass


class RuntimeSpeechGap:
    """Read the web-controlled speech gap without restarting a live probe."""

    def __init__(self, settings_file, default):
        self.path = Path(settings_file) if settings_file else None
        self.default = float(default)
        self._mtime_ns = None
        self._value = self.default
        self._gap_follow_up_count = 1
        self._gap_preceding_count = 0
        self._tail_gap_seconds = 1.0
        self._vad_threshold = 0.5
        self._vad_min_speech_seconds = 0.15
        self._vad_min_silence_seconds = 0.5
        self._clip_margin_seconds = 1.0
        self._lock = threading.Lock()

    def value(self):
        if self.path is None:
            return self.default
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            return self._value
        with self._lock:
            if mtime_ns == self._mtime_ns:
                return self._value
            self._mtime_ns = mtime_ns
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                value = float(payload.get("settings", {}).get("utterance_gap_seconds"))
                if 0.5 <= value <= 10:
                    self._value = value
                    print(f"[control] speech gap updated to {value:g}s", flush=True)
                follow_up_count = int(
                    payload.get("settings", {}).get("gap_follow_up_count", 1)
                )
                if 0 <= follow_up_count <= 100:
                    self._gap_follow_up_count = follow_up_count
                preceding_count = int(
                    payload.get("settings", {}).get("gap_preceding_count", 0)
                )
                if 0 <= preceding_count <= 100:
                    self._gap_preceding_count = preceding_count
                tail_gap_seconds = float(
                    payload.get("settings", {}).get("tail_gap_seconds", 1)
                )
                if 0.1 <= tail_gap_seconds <= 10:
                    self._tail_gap_seconds = tail_gap_seconds
                vad_threshold = float(
                    payload.get("settings", {}).get("vad_threshold", 0.5)
                )
                if 0.1 <= vad_threshold <= 0.9:
                    self._vad_threshold = vad_threshold
                vad_min_speech_seconds = float(
                    payload.get("settings", {}).get("vad_min_speech_seconds", 0.15)
                )
                if 0.05 <= vad_min_speech_seconds <= 2:
                    self._vad_min_speech_seconds = vad_min_speech_seconds
                vad_min_silence_seconds = float(
                    payload.get("settings", {}).get("vad_min_silence_seconds", 0.5)
                )
                if 0.1 <= vad_min_silence_seconds <= 3:
                    self._vad_min_silence_seconds = vad_min_silence_seconds
                clip_margin_seconds = float(
                    payload.get("settings", {}).get("clip_margin_seconds", 1)
                )
                if 0 <= clip_margin_seconds <= 10:
                    self._clip_margin_seconds = clip_margin_seconds
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            return self._value

    def gap_follow_up_count(self):
        self.value()
        return self._gap_follow_up_count

    def gap_preceding_count(self):
        self.value()
        return self._gap_preceding_count

    def tail_gap_seconds(self):
        self.value()
        return self._tail_gap_seconds

    def vad_min_speech_seconds(self):
        self.value()
        return self._vad_min_speech_seconds

    def vad_threshold(self):
        self.value()
        return self._vad_threshold

    def vad_min_silence_seconds(self):
        self.value()
        return self._vad_min_silence_seconds

    def clip_margin_seconds(self):
        self.value()
        return self._clip_margin_seconds


class SpeechDetector(threading.Thread):
    def __init__(self, chunk_dir, out_path, audio_started_at, segment_seconds,
                 utterance_gap_seconds, stop_event, runtime_speech_gap=None):
        super().__init__(daemon=True)
        self.chunk_dir = chunk_dir
        self.out_path = out_path
        self.audio_started_at = audio_started_at
        self.segment_seconds = segment_seconds
        self.utterance_gap_seconds = utterance_gap_seconds
        self.runtime_speech_gap = runtime_speech_gap
        self.stop_event = stop_event
        self.next_idx = 0
        self.pending = None
        self.segments = []
        self.raw_segments = []
        self.known_offset = 0.0
        self._state_lock = threading.RLock()

    def _emit_pending(self):
        with self._state_lock:
            if self.pending is None:
                return
            row = dict(self.pending)
            self.pending = None
            self.segments.append(row)
        append_jsonl(self.out_path, row)
        print(
            f"[vad {hhmmss(row['ts_start'])}] speech "
            f"{row['offset_start']:.2f}-"
            f"{row['offset_end']:.2f}s"
        )

    def _add_speech(self, row):
        with self._state_lock:
            self.raw_segments.append(dict(row))
            if self.pending is None:
                self.pending = row
                return
            gap = row["offset_start"] - self.pending["offset_end"]
            if gap <= self.current_gap_seconds():
                self.pending["offset_end"] = max(
                    self.pending["offset_end"], row["offset_end"]
                )
                self.pending["ts_end"] = max(self.pending["ts_end"], row["ts_end"])
                self.pending["iso_end"] = iso(self.pending["ts_end"])
                self.pending["end_chunk"] = row["end_chunk"]
                self.pending["segment_end"] = row["segment_end"]
                return
            self._emit_pending()
            self.pending = row

    def current_gap_seconds(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.value()
        return self.utterance_gap_seconds

    def gap_follow_up_count(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.gap_follow_up_count()
        return 1

    def gap_preceding_count(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.gap_preceding_count()
        return 0

    def tail_gap_seconds(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.tail_gap_seconds()
        return 1.0

    def vad_options(self):
        return VadOptions(
            threshold=self.vad_threshold(),
            min_speech_duration_ms=round(self.vad_min_speech_seconds() * 1000),
            min_silence_duration_ms=round(self.vad_min_silence_seconds() * 1000),
            speech_pad_ms=150,
        )

    def vad_threshold(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.vad_threshold()
        return 0.5

    def vad_min_speech_seconds(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.vad_min_speech_seconds()
        return 0.15

    def vad_min_silence_seconds(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.vad_min_silence_seconds()
        return 0.5

    def clip_margin_seconds(self):
        if self.runtime_speech_gap is not None:
            return self.runtime_speech_gap.clip_margin_seconds()
        return 1.0

    def detect_file(self, wav, idx):
        audio = read_vad_audio(wav)
        chunk_offset = idx * self.segment_seconds
        chunk_base = self.audio_started_at + chunk_offset
        speeches = get_speech_timestamps(
            audio, self.vad_options(), sampling_rate=16000
        )
        for speech in speeches:
            segment_start = speech["start"] / 16000.0
            segment_end = speech["end"] / 16000.0
            self._add_speech({
                "ts_start": chunk_base + segment_start,
                "ts_end": chunk_base + segment_end,
                "iso_start": iso(chunk_base + segment_start),
                "iso_end": iso(chunk_base + segment_end),
                "offset_start": chunk_offset + segment_start,
                "offset_end": chunk_offset + segment_end,
                "chunk": idx,
                "end_chunk": idx,
                "segment_start": segment_start,
                "segment_end": segment_end,
            })
        known_offset = chunk_offset + len(audio) / 16000.0
        with self._state_lock:
            self.known_offset = max(self.known_offset, known_offset)
            should_emit = (
                self.pending is not None
                and known_offset - self.pending["offset_end"]
                >= self.current_gap_seconds()
            )
        if should_emit:
            self._emit_pending()

    def snapshot(self):
        with self._state_lock:
            rows = [dict(row) for row in self.segments]
            if self.pending is not None:
                rows.append(dict(self.pending))
            return rows, self.known_offset

    def raw_snapshot(self):
        with self._state_lock:
            return [dict(row) for row in self.raw_segments], self.known_offset

    def run(self):
        while not self.stop_event.is_set():
            current = self.chunk_dir / f"chunk_{self.next_idx:06d}.wav"
            following = self.chunk_dir / f"chunk_{self.next_idx + 1:06d}.wav"
            if current.exists() and following.exists():
                try:
                    self.detect_file(current, self.next_idx)
                except Exception as e:
                    print(f"[vad] chunk {self.next_idx} failed: {e}")
                try:
                    current.unlink()
                except OSError:
                    pass
                self.next_idx += 1
            else:
                time.sleep(0.4)

    def flush_remaining(self):
        while True:
            wav = self.chunk_dir / f"chunk_{self.next_idx:06d}.wav"
            if not wav.exists():
                break
            try:
                self.detect_file(wav, self.next_idx)
            except Exception as e:
                print(f"[vad] final chunk {self.next_idx} failed: {e}")
            try:
                wav.unlink()
            except OSError:
                pass
            self.next_idx += 1
        self._emit_pending()


def normalize_reaction_text(text):
    text = re.sub(r"[wｗ]{4,}", "www", text, flags=re.I)
    text = re.sub(r"(草){3,}", "草草", text)
    return text


def render_cards(transcripts, chats, reaction_start, reaction_end):
    cards = []
    for t in transcripts:
        utterance = t["text"].strip()
        if len(utterance) < 2:
            continue
        base = t["ts_end"]
        reactions = [c for c in chats if base + reaction_start <= c["ts"] <= base + reaction_end]

        grouped, order = {}, []
        for c in reactions:
            key = normalize_reaction_text(c["text"].strip())
            if not key:
                continue
            if key not in grouped:
                grouped[key] = {"text": key, "count": 0, "examples": [], "first_ts": c["ts"]}
                order.append(key)
            grouped[key]["count"] += 1
            if len(grouped[key]["examples"]) < 3:
                grouped[key]["examples"].append(c["display_name"])

        reaction_html = ""
        for key in order:
            g = grouped[key]
            badge = f'<span class="count">×{g["count"]}</span>' if g["count"] > 1 else ""
            names = ", ".join(html.escape(x) for x in g["examples"])
            reaction_html += (
                '<div class="reaction">'
                f'<span class="rtime">{hhmmss(g["first_ts"])}</span>'
                f'<span class="rtext">{html.escape(g["text"])}</span>'
                f'{badge}<span class="names">{names}</span></div>'
            )
        if not reaction_html:
            reaction_html = '<div class="empty">（この時間帯にチャットなし）</div>'

        clip_html = ""
        clip_src = t.get("audio_clip")
        if clip_src:
            clip_html = (
                '<div class="audio-label">該当音声</div>'
                '<audio class="clip-player" controls preload="none" '
                f'src="{html.escape(clip_src, quote=True)}">音声を再生できません。</audio>'
            )

        cards.append(
            '<section class="card">'
            f'<div class="utterance-time">{hhmmss(t["ts_start"])}–{hhmmss(t["ts_end"])}</div>'
            f'<div class="utterance">{html.escape(utterance)}</div>'
            f'{clip_html}'
            f'<div class="arrow">↓ 直後 {reaction_start:g}〜{reaction_end:g}秒</div>'
            f'<div class="reactions">{reaction_html}</div></section>'
        )

    return "".join(cards) or '<div class="empty">この30秒間に書き起こしはありません。</div>'


def build_comparison_html(highlights, chats, output, reaction_start, reaction_end, channel):
    highlight_sections = []
    for item in highlights:
        video_path = output.parent / item["video"]
        video_html = ""
        if video_path.exists():
            video_html = (
                f'<video controls preload="metadata" playsinline src="{html.escape(item["video"], quote=True)}">'
                '映像を再生できません。</video>'
            )
        cards_html = render_cards(item["transcripts"], chats, reaction_start, reaction_end)
        highlight_sections.append(
            f'<section class="highlight" data-rank="{rank}" '
            f'data-start-seconds="{timeline_start:.3f}">'
            f'<h2>{html.escape(item["title"])}</h2>'
            f'{video_html}'
            f'<div class="highlight-meta">収録開始から {item["start"]:.1f}〜{item["start"] + item["duration"]:.1f}秒・{html.escape(item["metric"])}</div>'
            f'<div class="cards">{cards_html}</div>'
            '</section>'
        )

    doc = f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(channel)} reaction probe</title>
<style>
body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:#0e0e10; color:#efeff1; }}
main {{ width:min(1380px,96vw); margin:32px auto 80px; }}
.dashboard {{ color:#bf94ff; text-decoration:none; display:inline-block; margin-bottom:12px; }}
h1 {{ font-size:24px; margin-bottom:6px; }}
h2 {{ font-size:19px; margin:4px 4px 12px; }}
.meta {{ color:#adadb8; margin-bottom:26px; }}
.card {{ background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:18px; margin:14px 0; }}
.comparison {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; align-items:start; }}
.highlight {{ background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:12px; }}
.highlight video {{ display:block; width:100%; max-height:72vh; background:#000; border-radius:8px; }}
.highlight-meta {{ color:#adadb8; font-size:12px; margin:9px 4px 2px; }}
.highlight .card {{ background:#101012; }}
.clip-player {{ width:100%; height:38px; margin:2px 0 8px; }}
.audio-label {{ color:#adadb8; font-size:12px; margin-bottom:3px; }}
.utterance-time,.rtime {{ color:#adadb8; font-size:12px; font-variant-numeric:tabular-nums; }}
.utterance {{ font-size:21px; font-weight:650; line-height:1.55; margin:5px 0 10px; }}
.arrow {{ color:#bf94ff; font-size:13px; margin:8px 0; }}
.reaction {{ display:grid; grid-template-columns:72px 1fr auto; gap:8px 12px; align-items:center; padding:7px 0; border-top:1px solid #26262c; }}
.rtext {{ line-height:1.45; }}
.count {{ background:#9147ff; padding:2px 7px; border-radius:999px; font-size:12px; }}
.names {{ grid-column:2/4; color:#777783; font-size:11px; margin-top:-5px; }}
.empty {{ color:#777783; padding:8px 0; }}
@media (max-width:900px) {{ .comparison {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>{html.escape(channel)} — 30秒ハイライト比較</h1>
<div class="meta">同じ3分収録から「発話時間が最長」と「チャット投稿数が最多」の30秒を比較。発言後 {reaction_start:g}〜{reaction_end:g} 秒のチャットも表示。</div>
<div class="comparison">{''.join(highlight_sections)}</div>
</main></body></html>'''
    output.write_text(doc, encoding="utf-8")


def build_html(output, channel, highlights, duration=HIGHLIGHT_SECONDS,
               preroll_seconds=CLIP_MARGIN_SECONDS,
               display_limit=TOP_HIGHLIGHT_COUNT, video_prefix="highlight_chat",
               live=False, timeline_offset_seconds=0.0):
    cards = []
    for rank, item in enumerate(highlights, 1):
        video_name = item.get("video_name", f"{video_prefix}_{rank}.mp4")
        item_duration = float(item.get("duration_seconds") or duration)
        video_html = ""
        video_path = output.parent / video_name
        video_status = item.get("video_status", "ready" if video_path.exists() else "waiting_vod")
        if video_status == "ready" and video_path.exists():
            version = video_path.stat().st_mtime_ns
            video_html = (
                f'<video controls preload="metadata" playsinline '
                f'src="{video_name}?v={version}">'
                '映像を再生できません。</video>'
            )
        else:
            status_text = {
                "waiting_clip_end": "発話終了の確定を待っています。",
                "waiting_vod": "VODへの反映を待っています。",
                "generating": "VODから動画を生成中です。",
                "unavailable": "この配信のVODは利用できません。",
                "failed": "動画生成に失敗しました。再試行します。",
            }.get(video_status, "動画候補を準備中です。")
            video_html = f'<div class="waiting">{status_text}</div>'
        if "offset_seconds" in item:
            timeline_start = max(0.0, float(item["offset_seconds"]))
        else:
            timeline_start = max(
                0.0,
                float(item["trigger_start"]) + timeline_offset_seconds,
            )
        trigger_speech_label = ""
        if item.get("trigger_speech_video_seconds") is not None:
            trigger_speech_label = (
                "・直前の発話: 動画内 "
                f'{float(item["trigger_speech_video_seconds"]):.1f}秒'
            )
        linked_utterance_label = ""
        if (item.get("linked_preceding_count") is not None
                or item.get("linked_follow_up_count") is not None):
            linked_utterance_label = (
                "・連結: 前方 "
                f'{int(item.get("linked_preceding_count", 0) or 0)}個'
                "／後方 "
                f'{int(item.get("linked_follow_up_count", 0) or 0)}個'
            )
        decision_label = ""
        if item.get("decision_speech_gap_seconds") is not None:
            decision_label = (
                "判定: Speech gap "
                f'{float(item["decision_speech_gap_seconds"]):g}秒・'
                "後方間隔 "
                f'{float(item.get("decision_tail_gap_seconds", 1)) :g}秒未満・'
                "余白 "
                f'{float(item.get("decision_clip_margin_seconds", 1)) :g}秒'
            )
            if item.get("decision_vad_min_speech_seconds") is not None:
                decision_label += (
                    "・VAD 閾値 "
                    f'{float(item.get("decision_vad_threshold", 0.5)):g}'
                    "・最短発話 "
                    f'{float(item["decision_vad_min_speech_seconds"]):g}秒'
                    "・最短無音 "
                    f'{float(item.get("decision_vad_min_silence_seconds", 0.5)):g}秒'
                )
            front_reason = str(item.get("linked_preceding_reason", ""))
            back_reason = str(item.get("linked_follow_up_reason", ""))
            if front_reason:
                decision_label += f"／前方: {front_reason}"
            if back_reason:
                decision_label += f"／後方: {back_reason}"
        cards.append(
            f'<section class="highlight" data-rank="{rank}" '
            f'data-start-seconds="{timeline_start:.3f}" '
            f'data-video-status="{html.escape(video_status)}">'
            f'<h2>{rank}位</h2>'
            f'{video_html}'
            f'<div class="highlight-meta">'
            f'{format_elapsed(timeline_start)}〜'
            f'{format_elapsed(timeline_start + item_duration)}・'
            f'チャット {item["chat_count"]}件'
            f'<span class="decision-info">{trigger_speech_label}</span></div>'
            f'<div class="highlight-meta decision-info">{linked_utterance_label}</div>'
            f'<div class="highlight-meta decision-info">'
            f'{html.escape(decision_label)}</div>'
            '</section>'
        )
    if not cards:
        cards.append('<div class="empty">候補を収集中です。</div>')
    update_token = str(time.time_ns())
    live_script = ""
    if live:
        live_script = '''<script>
const currentToken = () => document.body.dataset.updateToken;
async function refreshHighlights() {
  if (location.protocol === "file:") return;
  if ([...document.querySelectorAll("video")].some(video => !video.paused && !video.ended)) return;
  try {
    const response = await fetch(`reactions.html?_=${Date.now()}`, {cache: "no-store"});
    const source = await response.text();
    const next = new DOMParser().parseFromString(source, "text/html");
    if (next.body.dataset.updateToken === currentToken()) return;
    document.querySelector(".highlights").innerHTML = next.querySelector(".highlights").innerHTML;
    document.body.dataset.updateToken = next.body.dataset.updateToken;
  } catch (_error) {}
}
if (location.protocol === "file:") {
  setTimeout(() => location.reload(), 30000);
} else {
  setInterval(refreshHighlights, 10000);
}
</script>'''
    doc = f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(channel)} chat highlight</title>
<style>
body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:#0e0e10; color:#efeff1; }}
main {{ width:min(1380px,96vw); margin:16px auto 60px; }}
h2 {{ font-size:20px; margin:4px 4px 12px; }}
.highlights {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; align-items:start; }}
.highlight {{ background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:12px; }}
.highlight video {{ display:block; width:100%; max-height:78vh; background:#000; border-radius:8px; }}
.highlight-meta {{ color:#adadb8; font-size:13px; margin:10px 4px 3px; }}
.waiting,.empty {{ color:#adadb8; background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:24px; }}
@media (max-width:1000px) {{ .highlights {{ grid-template-columns:1fr; }} }}
</style></head><body data-update-token="{update_token}"><main>
<div class="highlights">{''.join(cards)}</div>
</main>{live_script}</body></html>'''
    temp_path = output.with_suffix(output.suffix + ".tmp")
    temp_path.write_text(doc, encoding="utf-8")
    temp_path.replace(output)


def generate_comparison_outputs(out_dir, channel, recording_started_at,
                                segment_seconds, recording_seconds,
                                reaction_start, reaction_end):
    transcript_path = out_dir / "transcript.jsonl"
    chat_path = out_dir / "chat.jsonl"
    media_path = out_dir / "capture.ts"
    html_path = out_dir / "reactions.html"
    speech_video_path = out_dir / "highlight_speech.mp4"
    chat_video_path = out_dir / "highlight_chat.mp4"
    speech_transcript_path = out_dir / "highlight_speech_transcript.jsonl"
    chat_transcript_path = out_dir / "highlight_chat_transcript.jsonl"

    transcripts = load_jsonl(transcript_path)
    chats = load_jsonl(chat_path)
    highlight_seconds = 30.0
    speech_start, speech_seconds = select_dense_window(
        transcripts, segment_seconds, highlight_seconds, recording_seconds
    )
    chat_start, chat_count = select_chat_window(
        chats, recording_started_at, highlight_seconds, recording_seconds
    )
    speech_transcripts = transcripts_in_window(
        transcripts, segment_seconds, speech_start, highlight_seconds
    )
    chat_transcripts = transcripts_in_window(
        transcripts, segment_seconds, chat_start, highlight_seconds
    )
    write_jsonl(speech_transcript_path, speech_transcripts)
    write_jsonl(chat_transcript_path, chat_transcripts)

    speech_video_ok = speech_video_path.exists() and speech_video_path.stat().st_size > 0
    if not speech_video_ok:
        speech_video_ok = create_video_highlight(
            media_path, speech_video_path, speech_start, highlight_seconds
        )
    chat_video_ok = chat_video_path.exists() and chat_video_path.stat().st_size > 0
    if not chat_video_ok:
        chat_video_ok = create_video_highlight(
            media_path, chat_video_path, chat_start, highlight_seconds
        )
    if speech_video_ok and chat_video_ok:
        try:
            media_path.unlink()
        except OSError:
            pass

    build_comparison_html(
        [
            {
                "title": "発話密度トップ30秒",
                "video": speech_video_path.name,
                "start": speech_start,
                "duration": highlight_seconds,
                "metric": f"発話 {speech_seconds:.1f}秒",
                "transcripts": speech_transcripts,
            },
            {
                "title": "チャット盛り上がりトップ30秒",
                "video": chat_video_path.name,
                "start": chat_start,
                "duration": highlight_seconds,
                "metric": f"チャット {chat_count}件",
                "transcripts": chat_transcripts,
            },
        ],
        chats, html_path, reaction_start, reaction_end, channel,
    )
    return {
        "speech_start": speech_start,
        "speech_seconds": speech_seconds,
        "chat_start": chat_start,
        "chat_count": chat_count,
        "speech_video": speech_video_path,
        "chat_video": chat_video_path,
    }


def generate_chat_trigger_output(out_dir, channel, candidates, window_seconds,
                                 preroll_seconds,
                                 top_count, timeline_offset_seconds=0.0):
    highlights = candidates[:top_count]
    video_results = []
    for rank, item in enumerate(highlights, 1):
        video_results.append(create_video_highlight(
            item["source_path"], out_dir / f"highlight_chat_{rank}.mp4",
            item["source_offset"], window_seconds,
        ))
    build_html(
        out_dir / "reactions.html", channel, highlights, window_seconds,
        preroll_seconds, top_count,
        timeline_offset_seconds=timeline_offset_seconds,
    )
    return highlights


def generate_live_preview_output(out_dir, channel, candidates, window_seconds,
                                 preroll_seconds,
                                 top_count, preview_state,
                                 timeline_offset_seconds=0.0):
    highlights = candidates[:top_count]
    display_highlights = []
    active_keys = set()
    for item in highlights:
        candidate_key = re.sub(
            r"[^A-Za-z0-9_-]", "_", Path(item["source_path"]).stem
        )
        active_keys.add(candidate_key)
        video_name = f"preview_{candidate_key}.mp4"
        signature = (
            str(item["source_path"]), round(item["source_offset"], 3),
            round(window_seconds, 3),
        )
        output_path = out_dir / video_name
        if preview_state.get(candidate_key) != signature or not output_path.exists():
            temp_path = out_dir / f"preview_{candidate_key}.new.mp4"
            if create_preview_highlight(
                    item["source_path"], temp_path, item["source_offset"],
                    window_seconds):
                temp_path.replace(output_path)
                preview_state[candidate_key] = signature
            elif temp_path.exists():
                temp_path.unlink()
        display_item = dict(item)
        display_item["video_name"] = video_name
        display_highlights.append(display_item)

    for candidate_key in list(preview_state):
        if candidate_key not in active_keys:
            preview_state.pop(candidate_key, None)
            path = out_dir / f"preview_{candidate_key}.mp4"
            if path.exists():
                path.unlink()
    build_html(
        out_dir / "reactions.html", channel, display_highlights, window_seconds,
        preroll_seconds, top_count,
        video_prefix="preview_chat", live=True,
        timeline_offset_seconds=timeline_offset_seconds,
    )
    print(f"[preview] HTML updated: {len(highlights)} candidate(s)")


def main():
    parser = argparse.ArgumentParser(description="Twitch streamer utterance -> chat reaction probe")
    parser.add_argument(
        "--channel",
        default="yaritaiji",
        help="Twitch streamer ID (default: yaritaiji)",
    )
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--device", help=argparse.SUPPRESS)
    parser.add_argument("--compute-type", help=argparse.SUPPRESS)
    parser.add_argument("--segment-seconds", type=int, default=8)
    parser.add_argument("--reaction-start", type=float, default=1.5)
    parser.add_argument("--reaction-end", type=float, default=9.0)
    parser.add_argument(
        "--utterance-gap-seconds",
        type=float,
        default=UTTERANCE_GAP_SECONDS,
        help="merge speech segments separated by this many seconds (default: 3.5)",
    )
    parser.add_argument(
        "--runtime-settings-file",
        default="",
        help="JSON settings file watched for live speech-gap updates",
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=0.0,
        help="stop after this many minutes; use 0 to run until the stream ends (default: 0)",
    )
    parser.add_argument(
        "--highlight-seconds",
        type=float,
        default=HIGHLIGHT_SECONDS,
        help="chat ranking window in seconds (default: 30)",
    )
    parser.add_argument(
        "--clip-min-seconds",
        type=float,
        default=CLIP_MIN_SECONDS,
        help="minimum output clip length in seconds (default: 30)",
    )
    parser.add_argument(
        "--clip-max-seconds",
        type=float,
        default=CLIP_MAX_SECONDS,
        help="maximum output clip length in seconds (default: 140)",
    )
    parser.add_argument(
        "--clip-margin-seconds",
        type=float,
        default=CLIP_MARGIN_SECONDS,
        help="seconds before and after the clip speech (default: 1)",
    )
    parser.add_argument(
        "--top-count",
        type=int,
        default=TOP_HIGHLIGHT_COUNT,
        help="number of highlights to keep and display (default: 10)",
    )
    parser.add_argument(
        "--preview-interval-minutes",
        type=float,
        default=PREVIEW_INTERVAL_SECONDS / 60.0,
        help="live HTML update interval in minutes (default: 1)",
    )
    parser.add_argument(
        "--no-preview-server",
        action="store_true",
        help="generate preview files without starting the built-in HTTP server",
    )
    parser.add_argument(
        "--stream-started-at-epoch",
        type=float,
        default=0.0,
        help="Twitch stream start as Unix seconds for display timestamps",
    )
    parser.add_argument("--stream-id", default="")
    parser.add_argument("--stream-user-id", default="")
    parser.add_argument("--stream-started-at", default="")
    parser.add_argument(
        "--vod-poll-seconds",
        type=float,
        default=60.0,
        help="seconds between Twitch VOD availability checks (default: 60)",
    )
    parser.add_argument(
        "--vod-ready-margin-seconds",
        type=float,
        default=10.0,
        help="extra published VOD duration required after each clip (default: 10)",
    )
    parser.add_argument(
        "--vod-finalize-minutes",
        type=float,
        default=15.0,
        help="VOD retry time after a natural stream end (default: 15)",
    )
    parser.add_argument(
        "--preserve-published-on-start",
        action="store_true",
        help="keep the current HTML and highlight videos during a worker restart",
    )
    parser.add_argument("--out", default="reaction_session")
    args = parser.parse_args()

    if args.duration_minutes is not None and args.duration_minutes < 0:
        parser.error("--duration-minutes must be 0 or greater")
    if args.duration_minutes == 0:
        args.duration_minutes = None
    if args.highlight_seconds <= 0:
        parser.error("--highlight-seconds must be greater than 0")
    if args.clip_min_seconds <= 0:
        parser.error("--clip-min-seconds must be greater than 0")
    if args.clip_max_seconds < args.clip_min_seconds:
        parser.error("--clip-max-seconds must be at least --clip-min-seconds")
    if args.clip_margin_seconds < 0:
        parser.error("--clip-margin-seconds must be 0 or greater")
    if (args.duration_minutes is not None
            and max(args.highlight_seconds, args.clip_min_seconds)
            > args.duration_minutes * 60):
        parser.error("ranking/minimum clip length cannot exceed recording duration")
    if args.utterance_gap_seconds < 0:
        parser.error("--utterance-gap-seconds must be 0 or greater")
    if args.top_count <= 0:
        parser.error("--top-count must be greater than 0")
    if args.preview_interval_minutes <= 0:
        parser.error("--preview-interval-minutes must be greater than 0")
    if args.stream_started_at_epoch < 0:
        parser.error("--stream-started-at-epoch must be 0 or greater")
    if args.vod_poll_seconds < 30:
        parser.error("--vod-poll-seconds must be at least 30")
    if args.vod_ready_margin_seconds < 0:
        parser.error("--vod-ready-margin-seconds must be 0 or greater")
    if args.vod_finalize_minutes < 0:
        parser.error("--vod-finalize-minutes must be 0 or greater")

    if not args.channel:
        try:
            args.channel = input("Twitch配信者IDを入力してください: ")
        except EOFError:
            parser.error("--channel または配信者IDの入力が必要です")
    try:
        args.channel = normalize_channel(args.channel)
    except ValueError as exc:
        parser.error(str(exc))

    nick = os.environ.get("TWITCH_NICK", "").strip()
    token = os.environ.get("TWITCH_OAUTH_TOKEN", "").strip()
    if not nick or not token:
        print("TWITCH_NICK と TWITCH_OAUTH_TOKEN が必要です。Tokenには chat:read scope が必要です。", file=sys.stderr)
        sys.exit(2)

    print("[vad] speech detection enabled (no transcription)")

    out_dir = Path(args.out)
    chunk_dir = out_dir / "audio_chunks"
    chat_path = out_dir / "chat.jsonl"
    speech_path = out_dir / "speech_segments.jsonl"
    transcript_path = out_dir / "transcript.jsonl"
    detailed_transcript_path = out_dir / "transcript.detailed.jsonl"
    html_path = out_dir / "reactions.html"
    manifest_path = out_dir / "highlights.json"
    legacy_media_path = out_dir / "capture.ts"
    video_segment_dir = out_dir / "video_buffer"
    candidate_dir = out_dir / "candidate_buffer"
    legacy_highlight_path = out_dir / "highlight.mp4"
    speech_highlight_path = out_dir / "highlight_speech.mp4"
    chat_highlight_path = out_dir / "highlight_chat.mp4"
    speech_transcript_path = out_dir / "highlight_speech_transcript.jsonl"
    chat_transcript_path = out_dir / "highlight_chat_transcript.jsonl"
    clip_dir = out_dir / "utterance_clips"
    legacy_recording_path = out_dir / "recording.wav"
    out_dir.mkdir(parents=True, exist_ok=True)

    reset_paths = [
        chat_path, speech_path, transcript_path, detailed_transcript_path,
        legacy_media_path, legacy_highlight_path, speech_highlight_path,
        chat_highlight_path, speech_transcript_path, chat_transcript_path,
        legacy_recording_path,
    ]
    if not args.preserve_published_on_start:
        reset_paths.extend([html_path, manifest_path])
    for p in reset_paths:
        if p.exists():
            p.unlink()
    if not args.preserve_published_on_start:
        for pattern in ("highlight_chat_[0-9]*.mp4", "preview_*.mp4"):
            for path in out_dir.glob(pattern):
                path.unlink()
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    if video_segment_dir.exists():
        shutil.rmtree(video_segment_dir)
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)

    preview_server = None
    if not args.no_preview_server:
        preview_server = PreviewServer(out_dir)
        preview_server.start()
    initial_timeline_offset = (
        max(0.0, now_ts() - args.stream_started_at_epoch)
        if args.stream_started_at_epoch else 0.0
    )
    if not (args.preserve_published_on_start and html_path.is_file()):
        build_html(
            html_path, args.channel, [], args.highlight_seconds,
            args.clip_margin_seconds, args.top_count, video_prefix="preview_vod", live=True,
            timeline_offset_seconds=initial_timeline_offset,
        )

    stop_event = threading.Event()
    chat = TwitchChatRecorder(args.channel, nick, token, chat_path, stop_event)
    chat.start()

    capture = AudioOnlyCapture(
        f"https://www.twitch.tv/{args.channel}",
        chunk_dir,
        args.segment_seconds,
    )
    speech_detector = None
    highlight_manager = None
    vod_manager = None
    render_rankings = None
    stream_ended = False

    try:
        capture.start()
        timeline_offset_seconds = (
            max(0.0, capture.started_at - args.stream_started_at_epoch)
            if args.stream_started_at_epoch else 0.0
        )
        print(f"[DEBUG OFFSET] capture.started_at={capture.started_at}")
        print(f"[DEBUG OFFSET] stream_started_at_epoch={args.stream_started_at_epoch}")
        print(f"[DEBUG OFFSET] timeline_offset_seconds={timeline_offset_seconds}")
        if args.duration_minutes is None:
            deadline = None
            recording_seconds = float("inf")
        else:
            deadline = time.monotonic() + args.duration_minutes * 60
            recording_seconds = args.duration_minutes * 60
        buffer_seconds = args.highlight_seconds + max(
            BUFFER_SAFETY_SECONDS,
            args.clip_margin_seconds + BUFFER_SEGMENT_SAFETY_SECONDS,
        )
        runtime_speech_gap = RuntimeSpeechGap(
            args.runtime_settings_file,
            args.utterance_gap_seconds,
        )
        speech_detector = SpeechDetector(
            chunk_dir, speech_path, capture.started_at, args.segment_seconds,
            args.utterance_gap_seconds, stop_event, runtime_speech_gap
        )
        highlight_manager = RealtimeHighlightManager(
            chat_path, speech_path, capture.started_at,
            args.segment_seconds, recording_seconds,
            timeline_offset_seconds=timeline_offset_seconds,
            stream_id=args.stream_id,
            highlight_seconds=args.highlight_seconds,
            preroll_seconds=args.clip_margin_seconds,
            buffer_seconds=buffer_seconds,
            candidate_limit=max(30, args.top_count * 3),
            speech_provider=speech_detector,
            clip_min_seconds=args.clip_min_seconds,
            clip_max_seconds=args.clip_max_seconds,
            clip_end_silence_seconds=1.0,
            clip_end_margin_seconds=args.clip_margin_seconds,
        )
        stream_started_at = args.stream_started_at
        if not stream_started_at and args.stream_started_at_epoch:
            stream_started_at = datetime.fromtimestamp(
                args.stream_started_at_epoch, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        vod_manager = VodClipManager(
            out_dir,
            args.channel,
            args.stream_id,
            args.stream_user_id,
            stream_started_at,
            os.environ.get("TWITCH_CLIENT_ID", "").strip(),
            token,
            args.clip_min_seconds,
            poll_seconds=args.vod_poll_seconds,
            ready_margin_seconds=args.vod_ready_margin_seconds,
        )
        render_lock = threading.Lock()

        def render_rankings():
            with render_lock:
                build_html(
                    html_path,
                    args.channel,
                    vod_manager.rankings(),
                    args.highlight_seconds,
                    args.clip_margin_seconds,
                    args.top_count,
                    video_prefix="preview_vod",
                    live=True,
                    timeline_offset_seconds=timeline_offset_seconds,
                )

        vod_manager.on_change = render_rankings
        vod_manager.start()
        speech_detector.start()
        print("\n=== recording ===")
        if args.duration_minutes is None:
            print("配信終了まで継続します（途中終了は Ctrl+C）")
        else:
            print(f"{args.duration_minutes:g}分後に自動終了します（途中終了は Ctrl+C）")
        print(f"output: {out_dir.resolve()}\n")
        if preview_server:
            print(f"PC preview   : {preview_server.url}")
            print(f"phone preview: {preview_server.phone_url}\n")
        else:
            print("preview files: external web service\n")
        next_ranking_check = time.monotonic()
        next_preview_update = time.monotonic() + args.preview_interval_minutes * 60
        while not stop_event.is_set():
            if ((capture.streamlink and capture.streamlink.poll() is not None)
                    or (capture.ffmpeg and capture.ffmpeg.poll() is not None)):
                print("\n[main] stream ended or disconnected; stopping...")
                stream_ended = True
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                print("\n[main] recording duration reached; stopping...")
                break
            if time.monotonic() >= next_ranking_check:
                changed = highlight_manager.evaluate(
                    now_ts() - capture.started_at
                )
                if changed:
                    vod_manager.sync(
                        highlight_manager.top_non_overlapping(args.top_count)
                    )
                    render_rankings()
                next_ranking_check = time.monotonic() + 1.0
            if time.monotonic() >= next_preview_update:
                vod_manager.sync(
                    highlight_manager.top_non_overlapping(args.top_count)
                )
                render_rankings()
                next_preview_update = (
                    time.monotonic() + args.preview_interval_minutes * 60
                )
            time.sleep(0.5 if remaining is None else min(0.5, remaining))
    except KeyboardInterrupt:
        print("\n[main] stopping...")
    finally:
        stop_event.set()
        capture.stop()
        if speech_detector:
            speech_detector.join(timeout=15)
            try:
                speech_detector.flush_remaining()
            except Exception as e:
                print(f"[vad] flush failed: {e}")
        chat.join(timeout=3)
        highlights = []
        if highlight_manager:
            final_offset = now_ts() - capture.started_at
            if args.duration_minutes is not None:
                final_offset = min(args.duration_minutes * 60, final_offset)
            highlight_manager.evaluate(final_offset, force=True)
            highlights = highlight_manager.top_non_overlapping(args.top_count)
        if vod_manager:
            vod_manager.sync(highlights)
            if render_rankings:
                render_rankings()
            if stream_ended and args.vod_finalize_minutes:
                finalize_deadline = (
                    time.monotonic() + args.vod_finalize_minutes * 60
                )
                while time.monotonic() < finalize_deadline:
                    pending = [
                        item for item in vod_manager.rankings()
                        if item.get("video_status") != "ready"
                    ]
                    if not pending:
                        break
                    remaining_finalize = finalize_deadline - time.monotonic()
                    if remaining_finalize <= 0:
                        break
                    time.sleep(min(2.0, remaining_finalize))
            vod_manager.stop()
            vod_manager.join(timeout=330)
            vod_manager.mark_unavailable()
            if render_rankings:
                render_rankings()
        if chunk_dir.exists():
            shutil.rmtree(chunk_dir)
        print(f"[done] speech     : {speech_path}")
        if vod_manager:
            for item in vod_manager.rankings():
                if item.get("video_status") == "ready":
                    print(f"[done] highlight  : {item.get('video_path', '')}")
        print(f"[done] chat       : {chat_path}")
        print(f"[done] ranking    : {manifest_path}")
        print(f"[done] HTML       : {html_path}")
        if preview_server:
            preview_server.stop()


if __name__ == "__main__":
    main()
