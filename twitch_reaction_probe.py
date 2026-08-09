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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# The Xet downloader can stall on some Windows environments. Use the regular
# Hugging Face HTTP downloader unless the user explicitly configured otherwise.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from faster_whisper.vad import VadOptions, get_speech_timestamps

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
HIGHLIGHT_SECONDS = 60.0
BUFFER_SAFETY_SECONDS = 30.0
BUFFER_SEGMENT_SAFETY_SECONDS = 10.0
ROLLING_BUFFER_SECONDS = HIGHLIGHT_SECONDS + BUFFER_SAFETY_SECONDS
PREVIOUS_UTTERANCE_LOOKBACK_SECONDS = 20.0
CLIP_PREROLL_SECONDS = 3.0
TOP_HIGHLIGHT_COUNT = 10
PREVIEW_INTERVAL_SECONDS = 60.0


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
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds")


def hhmmss(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M:%S")


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


def find_chat_trigger(transcripts, segment_seconds, peak_start, max_lookback=12.0,
                      previous_lookback_seconds=PREVIOUS_UTTERANCE_LOOKBACK_SECONDS):
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

    target_start, _target_end, target_row = target
    previous = [item for item in intervals if item[1] <= target_start]
    if previous:
        previous_start, _previous_end, previous_row = max(
            previous, key=lambda item: item[1]
        )
        if target_start - previous_start <= previous_lookback_seconds:
            return previous_start, previous_row
    return target_start, target_row


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
            max(0.0, trigger_start - CLIP_PREROLL_SECONDS), max_start
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
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        print(f"[video] highlight failed: {result.stderr.strip()}")
        return False
    print(f"[video] highlight: {start_seconds:.1f}-{start_seconds + duration_seconds:.1f}s")
    return True


def create_preview_highlight(source_path, output_path, start_seconds, duration_seconds):
    if not source_path.exists() or source_path.stat().st_size == 0:
        return False
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


class RollingHighlightManager:
    def __init__(self, segment_dir, candidate_dir, chat_path, transcript_path,
                 recording_started_at, segment_seconds, recording_seconds,
                 highlight_seconds=HIGHLIGHT_SECONDS,
                 preroll_seconds=CLIP_PREROLL_SECONDS,
                 previous_lookback_seconds=PREVIOUS_UTTERANCE_LOOKBACK_SECONDS,
                 buffer_seconds=ROLLING_BUFFER_SECONDS, candidate_limit=8):
        self.segment_dir = segment_dir
        self.candidate_dir = candidate_dir
        self.chat_path = chat_path
        self.transcript_path = transcript_path
        self.recording_started_at = recording_started_at
        self.segment_seconds = segment_seconds
        self.recording_seconds = recording_seconds
        self.highlight_seconds = highlight_seconds
        self.preroll_seconds = preroll_seconds
        self.previous_lookback_seconds = previous_lookback_seconds
        self.buffer_seconds = buffer_seconds
        self.candidate_limit = candidate_limit
        self.candidates = []
        self.last_window_end = None
        self.next_candidate_id = 0
        self.chat_tail = JsonlTail(chat_path)
        self.speech_tail = JsonlTail(transcript_path)
        self.chats = []
        self.speech_segments = []
        self.candidate_dir.mkdir(parents=True, exist_ok=True)

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

    def _preserve_window(self, trigger_start):
        start_ts = self.recording_started_at + trigger_start
        end_ts = start_ts + self.highlight_seconds
        segments = []
        for path in sorted(self.segment_dir.glob("segment_*.ts")):
            created = path.stat().st_ctime
            if created <= end_ts + 6 and created + 7 >= start_ts:
                segments.append(path)
        if not segments:
            return None, 0.0

        candidate_id = self.next_candidate_id
        self.next_candidate_id += 1
        list_path = self.candidate_dir / f"candidate_{candidate_id:04d}.txt"
        output_path = self.candidate_dir / f"candidate_{candidate_id:04d}.ts"
        lines = []
        for path in segments:
            safe_path = path.resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe_path}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", "-f", "mpegts", str(output_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        try:
            list_path.unlink()
        except OSError:
            pass
        if result.returncode != 0 or not output_path.exists():
            print(f"[buffer] candidate copy failed: {result.stderr.strip()}")
            return None, 0.0
        source_offset = max(0.0, start_ts - segments[0].stat().st_ctime)
        return output_path, source_offset

    def _delete_candidate(self, item):
        try:
            item["source_path"].unlink()
        except OSError:
            pass

    def _prune_segments(self, current_offset):
        cutoff = self.recording_started_at + current_offset - self.buffer_seconds
        segments = sorted(self.segment_dir.glob("segment_*.ts"))
        for path in segments[:-2]:
            if path.stat().st_ctime < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def evaluate(self, current_offset, force=False):
        safe_end = current_offset if force else current_offset - 5.0
        if safe_end < self.highlight_seconds:
            self._prune_segments(current_offset)
            return
        window_end = safe_end if force else math.floor(safe_end / 5.0) * 5.0
        window_end = min(window_end, self.recording_seconds)
        if self.last_window_end is not None and window_end <= self.last_window_end:
            self._prune_segments(current_offset)
            return
        self.last_window_end = window_end
        peak_start = max(0.0, window_end - self.highlight_seconds)

        self._refresh_events(current_offset)
        chats = self.chats
        transcripts = self.speech_segments
        trigger_start, trigger_row = find_chat_trigger(
            transcripts, self.segment_seconds, peak_start,
            previous_lookback_seconds=self.previous_lookback_seconds,
        )
        trigger_start = min(
            max(0.0, trigger_start - self.preroll_seconds),
            max(0.0, self.recording_seconds - self.highlight_seconds),
        )
        chat_count = count_chats_in_window(
            chats, self.recording_started_at, trigger_start,
            self.highlight_seconds,
        )

        nearby = next(
            (item for item in self.candidates
             if abs(item["trigger_start"] - trigger_start) < 20.0),
            None,
        )
        if nearby and chat_count <= nearby["chat_count"]:
            self._prune_segments(current_offset)
            return
        if (not nearby and len(self.candidates) >= self.candidate_limit
                and chat_count <= min(item["chat_count"] for item in self.candidates)):
            self._prune_segments(current_offset)
            return

        source_path, source_offset = self._preserve_window(trigger_start)
        if source_path is None:
            self._prune_segments(current_offset)
            return
        item = {
            "trigger_start": trigger_start,
            "trigger_text": trigger_row.get("text", "") if trigger_row else "",
            "chat_count": chat_count,
            "source_path": source_path,
            "source_offset": source_offset,
        }
        if nearby:
            self._delete_candidate(nearby)
            self.candidates.remove(nearby)
        self.candidates.append(item)
        while len(self.candidates) > self.candidate_limit:
            loser = min(self.candidates, key=lambda row: row["chat_count"])
            self._delete_candidate(loser)
            self.candidates.remove(loser)
        print(f"[buffer] candidate: {trigger_start:.1f}s / {chat_count} chats")
        self._prune_segments(current_offset)

    def top_non_overlapping(self, limit=TOP_HIGHLIGHT_COUNT):
        selected = []
        for item in sorted(self.candidates, key=lambda row: row["chat_count"], reverse=True):
            start = item["trigger_start"]
            end = start + self.highlight_seconds
            if any(not (end <= chosen["trigger_start"]
                           or start >= chosen["trigger_start"] + self.highlight_seconds)
                   for chosen in selected):
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected


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


class AudioCapture:
    def __init__(self, channel_url, chunk_dir, video_segment_dir, segment_seconds):
        self.channel_url = channel_url
        self.chunk_dir = chunk_dir
        self.video_segment_dir = video_segment_dir
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
        self.video_segment_dir.mkdir(parents=True, exist_ok=True)
        self.streamlink = subprocess.Popen(
            ["streamlink", "--stdout", self.channel_url, "480p,480p60,best"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-i", "pipe:0",
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", "-f", "segment",
                "-segment_time", str(self.segment_seconds),
                "-reset_timestamps", "1",
                str(self.chunk_dir / "chunk_%06d.wav"),
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c", "copy", "-f", "segment",
                "-segment_time", "5", "-reset_timestamps", "1",
                "-segment_format", "mpegts",
                str(self.video_segment_dir / "segment_%06d.ts"),
            ],
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
                raise RuntimeError("配信映像の受信開始を30秒待ちましたが、データが届きませんでした。")
            time.sleep(0.1)
        self.started_at = first_chunk.stat().st_ctime
        print("[media] video/audio capture started")

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


class SpeechDetector(threading.Thread):
    def __init__(self, chunk_dir, out_path, audio_started_at, segment_seconds,
                 utterance_gap_seconds, stop_event):
        super().__init__(daemon=True)
        self.chunk_dir = chunk_dir
        self.out_path = out_path
        self.audio_started_at = audio_started_at
        self.segment_seconds = segment_seconds
        self.utterance_gap_seconds = utterance_gap_seconds
        self.stop_event = stop_event
        self.next_idx = 0
        self.pending = None
        self.vad_options = VadOptions(
            threshold=0.5,
            min_speech_duration_ms=250,
            min_silence_duration_ms=300,
            speech_pad_ms=150,
        )

    def _emit_pending(self):
        if self.pending is None:
            return
        append_jsonl(self.out_path, self.pending)
        print(
            f"[vad {hhmmss(self.pending['ts_start'])}] speech "
            f"{self.pending['offset_start']:.2f}-"
            f"{self.pending['offset_end']:.2f}s"
        )
        self.pending = None

    def _add_speech(self, row):
        if self.pending is None:
            self.pending = row
            return
        gap = row["offset_start"] - self.pending["offset_end"]
        if gap <= self.utterance_gap_seconds:
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

    def detect_file(self, wav, idx):
        audio = read_vad_audio(wav)
        chunk_offset = idx * self.segment_seconds
        chunk_base = self.audio_started_at + chunk_offset
        speeches = get_speech_timestamps(
            audio, self.vad_options, sampling_rate=16000
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
        if (self.pending is not None
                and known_offset - self.pending["offset_end"]
                >= self.utterance_gap_seconds):
            self._emit_pending()

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
            '<section class="highlight">'
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
               preroll_seconds=CLIP_PREROLL_SECONDS,
               previous_lookback_seconds=PREVIOUS_UTTERANCE_LOOKBACK_SECONDS,
               display_limit=TOP_HIGHLIGHT_COUNT, video_prefix="highlight_chat",
               live=False):
    cards = []
    for rank, item in enumerate(highlights, 1):
        video_name = item.get("video_name", f"{video_prefix}_{rank}.mp4")
        video_html = ""
        video_path = output.parent / video_name
        if video_path.exists():
            version = video_path.stat().st_mtime_ns
            video_html = (
                f'<video controls preload="metadata" playsinline '
                f'src="{video_name}?v={version}">'
                '映像を再生できません。</video>'
            )
        else:
            video_html = '<div class="waiting">動画候補を準備中です。</div>'
        cards.append(
            '<section class="highlight">'
            f'<h2>{rank}位</h2>'
            f'{video_html}'
            f'<div class="highlight-meta">収録開始から {item["trigger_start"]:.1f}〜{item["trigger_start"] + duration:.1f}秒・チャット {item["chat_count"]}件</div>'
            '</section>'
        )
    if not cards:
        cards.append('<div class="empty">候補を収集中です。</div>')
    status = "暫定" if live else "確定"
    updated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
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
    document.querySelector("h1").textContent = next.querySelector("h1").textContent;
    document.querySelector(".meta").innerHTML = next.querySelector(".meta").innerHTML;
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
main {{ width:min(1380px,96vw); margin:32px auto 80px; }}
h1 {{ font-size:24px; margin-bottom:6px; }}
h2 {{ font-size:20px; margin:4px 4px 12px; }}
.meta {{ color:#adadb8; margin-bottom:22px; }}
.highlights {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; align-items:start; }}
.highlight {{ background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:12px; }}
.highlight video {{ display:block; width:100%; max-height:78vh; background:#000; border-radius:8px; }}
.highlight-meta {{ color:#adadb8; font-size:13px; margin:10px 4px 3px; }}
.waiting,.empty {{ color:#adadb8; background:#18181b; border:1px solid #2f2f35; border-radius:12px; padding:24px; }}
@media (max-width:1000px) {{ .highlights {{ grid-template-columns:1fr; }} }}
</style></head><body data-update-token="{update_token}"><main>
<h1>{html.escape(channel)} — チャット盛り上がり{status}上位{display_limit}件</h1>
<div class="meta">重複しない盛り上がり区間を選び、きっかけの1つ前の発言（{previous_lookback_seconds:g}秒以内）の{preroll_seconds:g}秒前から{duration:g}秒を切り抜き。更新: {updated_at}{'（自動更新）' if live else ''}</div>
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
                                 preroll_seconds, previous_lookback_seconds,
                                 top_count):
    highlights = candidates[:top_count]
    video_results = []
    for rank, item in enumerate(highlights, 1):
        video_results.append(create_video_highlight(
            item["source_path"], out_dir / f"highlight_chat_{rank}.mp4",
            item["source_offset"], window_seconds,
        ))
    build_html(
        out_dir / "reactions.html", channel, highlights, window_seconds,
        preroll_seconds, previous_lookback_seconds, top_count,
    )
    return highlights


def generate_live_preview_output(out_dir, channel, candidates, window_seconds,
                                 preroll_seconds, previous_lookback_seconds,
                                 top_count, preview_state):
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
        preroll_seconds, previous_lookback_seconds, top_count,
        video_prefix="preview_chat", live=True,
    )
    print(f"[preview] HTML updated: {len(highlights)} candidate(s)")


def main():
    parser = argparse.ArgumentParser(description="Twitch streamer utterance -> chat reaction probe")
    parser.add_argument("--channel", help="Twitch streamer ID (prompted when omitted)")
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--device", help=argparse.SUPPRESS)
    parser.add_argument("--compute-type", help=argparse.SUPPRESS)
    parser.add_argument("--segment-seconds", type=int, default=8)
    parser.add_argument("--reaction-start", type=float, default=1.5)
    parser.add_argument("--reaction-end", type=float, default=9.0)
    parser.add_argument(
        "--utterance-gap-seconds",
        type=float,
        default=2.5,
        help="merge speech segments separated by this many seconds (default: 2.5)",
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=None,
        help="stop after this many minutes; use 0 or omit to run until the stream ends",
    )
    parser.add_argument(
        "--highlight-seconds",
        type=float,
        default=HIGHLIGHT_SECONDS,
        help="highlight duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--preroll-seconds",
        type=float,
        default=CLIP_PREROLL_SECONDS,
        help="seconds to include before the selected utterance (default: 3)",
    )
    parser.add_argument(
        "--previous-lookback-seconds",
        type=float,
        default=PREVIOUS_UTTERANCE_LOOKBACK_SECONDS,
        help="maximum distance to use the previous utterance (default: 20)",
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
    parser.add_argument("--out", default="reaction_session")
    args = parser.parse_args()

    if args.duration_minutes is not None and args.duration_minutes < 0:
        parser.error("--duration-minutes must be 0 or greater")
    if args.duration_minutes == 0:
        args.duration_minutes = None
    if args.highlight_seconds <= 0:
        parser.error("--highlight-seconds must be greater than 0")
    if (args.duration_minutes is not None
            and args.highlight_seconds > args.duration_minutes * 60):
        parser.error("--highlight-seconds cannot exceed the recording duration")
    if args.utterance_gap_seconds < 0:
        parser.error("--utterance-gap-seconds must be 0 or greater")
    if args.preroll_seconds < 0:
        parser.error("--preroll-seconds must be 0 or greater")
    if args.previous_lookback_seconds < 0:
        parser.error("--previous-lookback-seconds must be 0 or greater")
    if args.top_count <= 0:
        parser.error("--top-count must be greater than 0")
    if args.preview_interval_minutes <= 0:
        parser.error("--preview-interval-minutes must be greater than 0")

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
    legacy_media_path = out_dir / "capture.ts"
    video_segment_dir = out_dir / "video_buffer"
    candidate_dir = out_dir / "candidate_buffer"
    legacy_highlight_path = out_dir / "highlight.mp4"
    speech_highlight_path = out_dir / "highlight_speech.mp4"
    chat_highlight_path = out_dir / "highlight_chat.mp4"
    ranked_highlight_paths = [
        out_dir / f"highlight_chat_{rank}.mp4"
        for rank in range(1, args.top_count + 1)
    ]
    preview_highlight_paths = [
        out_dir / f"preview_chat_{rank}.mp4"
        for rank in range(1, args.top_count + 1)
    ]
    speech_transcript_path = out_dir / "highlight_speech_transcript.jsonl"
    chat_transcript_path = out_dir / "highlight_chat_transcript.jsonl"
    clip_dir = out_dir / "utterance_clips"
    legacy_recording_path = out_dir / "recording.wav"
    out_dir.mkdir(parents=True, exist_ok=True)

    for p in (chat_path, speech_path, transcript_path, detailed_transcript_path, html_path,
              legacy_media_path, legacy_highlight_path, speech_highlight_path,
              chat_highlight_path, *ranked_highlight_paths,
              *preview_highlight_paths,
              speech_transcript_path, chat_transcript_path,
              legacy_recording_path):
        if p.exists():
            p.unlink()
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
    preview_state = {}
    build_html(
        html_path, args.channel, [], args.highlight_seconds,
        args.preroll_seconds, args.previous_lookback_seconds,
        args.top_count, video_prefix="preview_chat", live=True,
    )

    stop_event = threading.Event()
    chat = TwitchChatRecorder(args.channel, nick, token, chat_path, stop_event)
    chat.start()

    capture = AudioCapture(
        f"https://www.twitch.tv/{args.channel}", chunk_dir,
        video_segment_dir, args.segment_seconds
    )
    speech_detector = None
    highlight_manager = None

    try:
        capture.start()
        if args.duration_minutes is None:
            deadline = None
            recording_seconds = float("inf")
        else:
            deadline = time.monotonic() + args.duration_minutes * 60
            recording_seconds = args.duration_minutes * 60
        buffer_seconds = args.highlight_seconds + max(
            BUFFER_SAFETY_SECONDS,
            args.previous_lookback_seconds + args.preroll_seconds
            + BUFFER_SEGMENT_SAFETY_SECONDS,
        )
        highlight_manager = RollingHighlightManager(
            video_segment_dir, candidate_dir, chat_path, speech_path,
            capture.started_at, args.segment_seconds, recording_seconds,
            highlight_seconds=args.highlight_seconds,
            preroll_seconds=args.preroll_seconds,
            previous_lookback_seconds=args.previous_lookback_seconds,
            buffer_seconds=buffer_seconds,
            candidate_limit=max(30, args.top_count * 3),
        )
        speech_detector = SpeechDetector(
            chunk_dir, speech_path, capture.started_at, args.segment_seconds,
            args.utterance_gap_seconds, stop_event
        )
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
        next_buffer_check = time.monotonic()
        next_preview_update = time.monotonic() + args.preview_interval_minutes * 60
        while not stop_event.is_set():
            if ((capture.streamlink and capture.streamlink.poll() is not None)
                    or (capture.ffmpeg and capture.ffmpeg.poll() is not None)):
                print("\n[main] stream ended or disconnected; stopping...")
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                print("\n[main] recording duration reached; stopping...")
                break
            if time.monotonic() >= next_buffer_check:
                highlight_manager.evaluate(now_ts() - capture.started_at)
                next_buffer_check = time.monotonic() + 1.0
            if time.monotonic() >= next_preview_update:
                generate_live_preview_output(
                    out_dir, args.channel,
                    highlight_manager.top_non_overlapping(args.top_count),
                    args.highlight_seconds, args.preroll_seconds,
                    args.previous_lookback_seconds, args.top_count,
                    preview_state,
                )
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
            highlight_manager.evaluate(
                final_offset, force=True,
            )
            highlights = highlight_manager.top_non_overlapping(args.top_count)
            generate_chat_trigger_output(
                out_dir, args.channel, highlights, args.highlight_seconds,
                args.preroll_seconds, args.previous_lookback_seconds,
                args.top_count,
            )
            completed = sum(path.exists() for path in ranked_highlight_paths)
            if completed >= len(highlights):
                if video_segment_dir.exists():
                    shutil.rmtree(video_segment_dir)
                if candidate_dir.exists():
                    shutil.rmtree(candidate_dir)
        print(f"[done] speech     : {speech_path}")
        for path in ranked_highlight_paths:
            if path.exists():
                print(f"[done] highlight  : {path}")
        print(f"[done] chat       : {chat_path}")
        print(f"[done] HTML       : {html_path}")
        if preview_server:
            preview_server.stop()


if __name__ == "__main__":
    main()
