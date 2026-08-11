import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import hype_web
import highlight_compiler
import twitch_reaction_probe
import vod_clip_manager
import vps_worker


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class WorkerTests(unittest.TestCase):
    def test_build_probe_command_uses_vps_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(vps_worker, "DATA_DIR", Path(temporary)):
                with mock.patch.dict(
                    os.environ,
                    {
                        "HIGHLIGHT_SECONDS": "30",
                        "CLIP_MIN_SECONDS": "30",
                        "CLIP_MAX_SECONDS": "90",
                        "CLIP_MARGIN_SECONDS": "1",
                        "UTTERANCE_GAP_SECONDS": "2.5",
                        "TOP_COUNT": "10",
                    },
                    clear=False,
                ):
                    command = vps_worker.build_probe_command(
                        "yaritaiji",
                        Path(temporary) / "yaritaiji",
                        1234.5,
                        preserve_published=True,
                        stream_id="stream-123",
                        stream_user_id="user-456",
                        stream_started_at="2026-08-09T01:02:03Z",
                    )
        self.assertIn("--no-preview-server", command)
        self.assertIn("--preserve-published-on-start", command)
        self.assertEqual(command[command.index("--channel") + 1], "yaritaiji")
        self.assertEqual(command[command.index("--highlight-seconds") + 1], "30")
        self.assertEqual(command[command.index("--clip-min-seconds") + 1], "30")
        self.assertEqual(command[command.index("--clip-max-seconds") + 1], "90")
        self.assertEqual(
            command[command.index("--clip-margin-seconds") + 1], "1"
        )
        self.assertEqual(
            command[command.index("--utterance-gap-seconds") + 1], "2.5"
        )
        self.assertEqual(command[command.index("--top-count") + 1], "10")
        self.assertEqual(
            command[command.index("--stream-started-at-epoch") + 1],
            "1234.5",
        )
        self.assertEqual(
            command[command.index("--out") + 1],
            str(Path(temporary) / "yaritaiji"),
        )
        self.assertEqual(command[command.index("--stream-id") + 1], "stream-123")
        self.assertEqual(
            command[command.index("--stream-user-id") + 1], "user-456"
        )

    def test_stream_start_comes_from_twitch_helix(self):
        response = FakeResponse(
            {
                "data": [
                    {
                        "id": "stream-123",
                        "user_id": "user-456",
                        "started_at": "2026-08-09T01:02:03Z",
                    }
                ]
            }
        )
        with mock.patch.object(
            vps_worker.requests, "get", return_value=response
        ) as request:
            epoch = vps_worker.get_stream_started_at_epoch(
                "yaritaiji", "token", "client"
            )
        self.assertGreater(epoch, 0)
        self.assertEqual(
            request.call_args.kwargs["params"], {"user_login": "yaritaiji"}
        )

    def test_stream_info_keeps_vod_correlation_fields(self):
        response = FakeResponse(
            {
                "data": [
                    {
                        "id": "stream-123",
                        "user_id": "user-456",
                        "started_at": "2026-08-09T01:02:03Z",
                    }
                ]
            }
        )
        with mock.patch.object(vps_worker.requests, "get", return_value=response):
            info = vps_worker.get_stream_info("yaritaiji", "token", "client")
        self.assertEqual(info["stream_id"], "stream-123")
        self.assertEqual(info["user_id"], "user-456")
        self.assertGreater(info["started_at_epoch"], 0)

    def test_purge_is_limited_to_selected_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "channels"
            selected = root / "yaritaiji"
            other = root / "other"
            selected.mkdir(parents=True)
            other.mkdir()
            (selected / "old.mp4").write_bytes(b"old")
            (other / "keep.mp4").write_bytes(b"keep")
            with mock.patch.object(vps_worker, "CHANNELS_ROOT", root):
                vps_worker.purge_channel_data("yaritaiji")
            self.assertTrue(selected.is_dir())
            self.assertFalse((selected / "old.mp4").exists())
            self.assertTrue((other / "keep.mp4").exists())

    def test_refresh_rotates_and_saves_refresh_token(self):
        response = FakeResponse(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "scope": ["chat:read"],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "tokens.json"
            with mock.patch.object(vps_worker, "TOKEN_FILE", token_file):
                with mock.patch.object(
                    vps_worker.requests, "post", return_value=response
                ) as request:
                    tokens = vps_worker.refresh_tokens(
                        {"refresh_token": "old-refresh"}, "client", "secret"
                    )
            stored = json.loads(token_file.read_text(encoding="utf-8"))
        self.assertEqual(tokens["access_token"], "new-access")
        self.assertEqual(stored["refresh_token"], "new-refresh")
        self.assertEqual(request.call_args.kwargs["data"]["client_secret"], "secret")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "data"
        self.control = self.root / "control"
        self.output.mkdir()
        self.control.mkdir()
        self.app = hype_web.create_app(self.output, self.control)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def add_channel(self, channel):
        return self.client.post("/api/channels", json={"channel": channel})

    def channel_output(self, channel):
        path = self.output / "channels" / channel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_dashboard_adds_three_channels_and_rejects_fourth(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("<h1>Hype Clipper</h1>", page.get_data(as_text=True))
        self.assertNotIn("最大2配信者を720pで同時監視", page.get_data(as_text=True))
        self.assertIn('role="tablist"', page.get_data(as_text=True))
        self.assertNotIn("<iframe", page.get_data(as_text=True))
        self.assertIn('id="rankings"', page.get_data(as_text=True))
        self.assertIn("ランキング順", page.get_data(as_text=True))
        self.assertIn("新着順", page.get_data(as_text=True))
        self.assertNotIn("結果を消して再収集", page.get_data(as_text=True))
        self.assertNotIn("別タブで開く", page.get_data(as_text=True))
        self.assertEqual(self.add_channel("yaritaiji").status_code, 202)
        self.assertEqual(self.add_channel("SHAKA").status_code, 202)
        self.assertEqual(self.add_channel("third_channel").status_code, 202)
        fourth = self.add_channel("fourth_channel")
        self.assertEqual(fourth.status_code, 409)
        channels = self.client.get("/api/channels").json["channels"]
        self.assertEqual(
            [item["channel"] for item in channels],
            ["yaritaiji", "shaka", "third_channel"],
        )

    def test_dashboard_accepts_twitch_channel_url(self):
        added = self.add_channel("https://twitch.tv/xhalli4x")
        self.assertEqual(added.status_code, 202)
        self.assertEqual(added.json["channel"], "xhalli4x")

    def test_delete_removes_channel(self):
        self.add_channel("yaritaiji")
        deleted = self.client.delete("/api/channels/yaritaiji")
        self.assertEqual(deleted.status_code, 202)
        self.assertEqual(self.client.get("/api/channels").json["channels"], [])

    def test_channel_status_hides_device_code(self):
        self.add_channel("yaritaiji")
        channel_dir = self.channel_output("yaritaiji")
        (channel_dir / "service_status.json").write_text(
            json.dumps(
                {
                    "state": "waiting_for_twitch_authorization",
                    "channel": "yaritaiji",
                    "user_code": "SECRET-CODE",
                }
            ),
            encoding="utf-8",
        )
        page = self.client.get("/channels/yaritaiji/reactions.html")
        status = self.client.get("/api/channels")
        self.assertEqual(page.status_code, 200)
        self.assertIn("waiting_for_twitch_authorization", page.get_data(as_text=True))
        self.assertNotIn("SECRET-CODE", status.get_data(as_text=True))
        self.assertEqual(
            status.json["channels"][0]["status"]["channel"], "yaritaiji"
        )

    def test_video_supports_byte_range_requests(self):
        media = bytes(range(100))
        channel_dir = self.channel_output("yaritaiji")
        (channel_dir / "highlight_chat_1.mp4").write_bytes(media)
        response = self.client.get(
            "/channels/yaritaiji/highlight_chat_1.mp4",
            headers={"Range": "bytes=10-19"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, media[10:20])
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/100")
        response.close()

    def test_manifest_counts_ranked_candidates_before_videos_exist(self):
        self.add_channel("yaritaiji")
        channel_dir = self.channel_output("yaritaiji")
        (channel_dir / "highlights.json").write_text(
            json.dumps({"highlights": [{"rank": 1}, {"rank": 2}]}),
            encoding="utf-8",
        )
        payload = self.client.get("/api/channels").json
        self.assertEqual(payload["channels"][0]["highlight_count"], 2)

    def test_non_highlight_files_are_not_public(self):
        channel_dir = self.channel_output("yaritaiji")
        (channel_dir / "chat.jsonl").write_text("secret", encoding="utf-8")
        self.assertEqual(
            self.client.get("/channels/yaritaiji/chat.jsonl").status_code,
            404,
        )


class RealtimeProbeTests(unittest.TestCase):
    def test_dynamic_clip_end_waits_for_silence_and_extends_past_minimum(self):
        speech = [{"offset_start": 28.0, "offset_end": 35.0}]
        waiting = twitch_reaction_probe.resolve_dynamic_clip_duration(
            speech, 0.0, 36.0,
        )
        ready = twitch_reaction_probe.resolve_dynamic_clip_duration(
            speech, 0.0, 39.0,
        )
        self.assertIsNone(waiting[0])
        self.assertEqual(waiting[1], "waiting_silence")
        self.assertEqual(ready, (36.0, "natural", 35.0))

    def test_dynamic_clip_end_uses_minimum_and_hard_maximum(self):
        minimum = twitch_reaction_probe.resolve_dynamic_clip_duration(
            [{"offset_start": 5.0, "offset_end": 20.0}],
            0.0,
            30.0,
        )
        maximum = twitch_reaction_probe.resolve_dynamic_clip_duration(
            [{"offset_start": 28.0, "offset_end": 95.0}],
            0.0,
            90.0,
            maximum_seconds=90.0,
        )
        self.assertEqual(minimum, (30.0, "natural_at_minimum", 20.0))
        self.assertEqual(maximum, (90.0, "hard_max", 95.0))

    def test_dynamic_clip_end_stays_at_minimum_when_speech_stops_at_minimum(self):
        speech = [{"offset_start": 28.0, "offset_end": 30.0}]
        result = twitch_reaction_probe.resolve_dynamic_clip_duration(
            speech, 0.0, 34.0,
        )
        self.assertEqual(result, (30.0, "natural_at_minimum", 30.0))

    def test_live_capture_commands_are_audio_only(self):
        streamlink_command = (
            twitch_reaction_probe.build_live_audio_streamlink_command(
                "https://www.twitch.tv/yaritaiji"
            )
        )
        ffmpeg_command = twitch_reaction_probe.build_live_audio_ffmpeg_command(
            Path("audio_chunks"), 8
        )
        self.assertEqual(streamlink_command[-1], "audio_only")
        self.assertNotIn("720p", " ".join(streamlink_command))
        self.assertIn("-vn", ffmpeg_command)
        self.assertNotIn("0:v:0", ffmpeg_command)
        self.assertNotIn("video_buffer", " ".join(map(str, ffmpeg_command)))

    def test_existing_trigger_and_preroll_logic_becomes_stream_offset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chat_path = root / "chat.jsonl"
            speech_path = root / "speech.jsonl"
            speech_rows = [
                {"offset_start": 20.0, "offset_end": 21.0},
                {"offset_start": 25.0, "offset_end": 26.0},
            ]
            speech_path.write_text(
                "".join(json.dumps(row) + "\n" for row in speech_rows),
                encoding="utf-8",
            )
            chat_path.write_text(
                "".join(
                    json.dumps({"ts": 1000.0 + offset}) + "\n"
                    for offset in (16.0, 22.0, 30.0)
                ),
                encoding="utf-8",
            )
            manager = twitch_reaction_probe.RealtimeHighlightManager(
                chat_path,
                speech_path,
                1000.0,
                8,
                float("inf"),
                timeline_offset_seconds=100.0,
                stream_id="stream-123",
                highlight_seconds=30.0,
                preroll_seconds=1.0,
            )
            changed = manager.evaluate(60.0)
            duplicate_changed = manager.evaluate(65.0)
            item = manager.top_non_overlapping(1)[0]
        self.assertTrue(changed)
        self.assertFalse(duplicate_changed)
        self.assertEqual(len(manager.candidates), 1)
        # The 4-second gap is a new utterance: only 1 second preroll applies.
        self.assertEqual(item["trigger_start"], 24.0)
        self.assertEqual(item["offset_seconds"], 124.0)
        self.assertEqual(item["score"], 1)

    def test_non_overlapping_uses_variable_clip_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = twitch_reaction_probe.RealtimeHighlightManager(
                Path(temporary) / "chat.jsonl",
                Path(temporary) / "speech.jsonl",
                1000.0,
                8,
                float("inf"),
                clip_min_seconds=30.0,
                clip_max_seconds=140.0,
            )
            manager.candidates = [
                {"trigger_start": 0.0, "duration_seconds": 100.0,
                 "clip_end_status": "ready", "chat_count": 20},
                {"trigger_start": 30.0, "duration_seconds": 30.0,
                 "clip_end_status": "ready", "chat_count": 10},
            ]
            self.assertEqual(len(manager.top_non_overlapping(10)), 1)

    def test_waiting_vod_is_rendered_without_blocking_ranking(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reactions.html"
            twitch_reaction_probe.build_html(
                output,
                "yaritaiji",
                [
                    {
                        "offset_seconds": 115.0,
                        "chat_count": 20,
                        "video_status": "waiting_vod",
                    }
                ],
            )
            page = output.read_text(encoding="utf-8")
        self.assertIn("VODへの反映を待っています。", page)
        self.assertIn('data-start-seconds="115.000"', page)

    def test_waiting_clip_end_is_rendered(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reactions.html"
            twitch_reaction_probe.build_html(
                output,
                "yaritaiji",
                [{
                    "offset_seconds": 115.0,
                    "chat_count": 20,
                    "video_status": "waiting_clip_end",
                    "duration_seconds": 0.0,
                }],
            )
            page = output.read_text(encoding="utf-8")
        self.assertIn("発話終了の確定を待っています。", page)


class VodClipTests(unittest.TestCase):
    def test_candidate_duration_is_stored_per_highlight(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = vod_clip_manager.VodClipManager(
                Path(temporary),
                "yaritaiji",
                "stream-123",
                "user-456",
                "2026-08-09T01:02:03Z",
                "client",
                "token",
                30.0,
            )
            rankings = manager.sync([{
                "candidate_id": "candidate-1",
                "offset_seconds": 120.0,
                "duration_seconds": 42.5,
                "clip_end_status": "ready",
                "chat_count": 50,
                "score": 50,
            }])
            with mock.patch.object(
                manager,
                "_lookup_vod",
                return_value={
                    "id": "vod-1",
                    "url": "https://www.twitch.tv/videos/1",
                    "duration_seconds": 1000.0,
                    "match_method": "stream_id",
                },
            ), mock.patch.object(
                vod_clip_manager,
                "generate_vod_clip",
                return_value=(True, "", False),
            ) as generate:
                manager.process_once()
        self.assertEqual(rankings[0]["duration_seconds"], 42.5)
        self.assertEqual(rankings[0]["video_status"], "waiting_vod")
        self.assertEqual(generate.call_args.args[-1], 42.5)

    def test_unconfirmed_clip_does_not_start_vod_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = vod_clip_manager.VodClipManager(
                Path(temporary),
                "yaritaiji",
                "stream-123",
                "user-456",
                "2026-08-09T01:02:03Z",
                "client",
                "token",
                30.0,
            )
            manager.sync([{
                "candidate_id": "candidate-1",
                "offset_seconds": 120.0,
                "duration_seconds": 0.0,
                "clip_end_status": "waiting",
                "chat_count": 50,
                "score": 50,
            }])
            with mock.patch.object(manager, "_lookup_vod") as lookup:
                manager.process_once()
        lookup.assert_not_called()

    def test_twitch_duration_and_range_availability(self):
        self.assertEqual(vod_clip_manager.parse_twitch_duration("2h3m4s"), 7384)
        vod = {"duration_seconds": 200.0}
        self.assertTrue(vod_clip_manager.vod_has_range(vod, 160, 30, 10))
        self.assertFalse(vod_clip_manager.vod_has_range(vod, 161, 30, 10))

    def test_hls_command_fetches_only_segments_around_offset(self):
        command, trim_seconds = vod_clip_manager.build_vod_streamlink_command(
            "https://www.twitch.tv/videos/123", 123.0, 30.0
        )
        self.assertIn("--stdout", command)
        self.assertNotIn("--stream-url", command)
        self.assertEqual(
            command[command.index("--hls-start-offset") + 1], "111.000s"
        )
        self.assertEqual(
            command[command.index("--stream-segmented-duration") + 1],
            "42.000s",
        )
        self.assertEqual(command[-1], "720p60,720p,best")
        self.assertEqual(trim_seconds, 12.0)

    def test_vod_is_matched_by_stream_id_not_latest_video(self):
        response = FakeResponse(
            {
                "data": [
                    {
                        "id": "latest-wrong",
                        "stream_id": "other-stream",
                        "duration": "4h",
                        "url": "https://www.twitch.tv/videos/1",
                    },
                    {
                        "id": "correct",
                        "stream_id": "stream-123",
                        "duration": "1h2m3s",
                        "url": "https://www.twitch.tv/videos/2",
                    },
                ]
            }
        )
        with mock.patch.object(
            vod_clip_manager.requests, "get", return_value=response
        ) as request:
            vod = vod_clip_manager.find_matching_vod(
                "user-456",
                "stream-123",
                "2026-08-09T01:02:03Z",
                "client",
                "token",
            )
        self.assertEqual(vod["id"], "correct")
        self.assertEqual(vod["match_method"], "stream_id")
        self.assertEqual(request.call_args.kwargs["params"]["type"], "archive")

    def test_missing_stream_metadata_does_not_select_an_arbitrary_vod(self):
        response = FakeResponse(
            {
                "data": [
                    {
                        "id": "latest",
                        "stream_id": "",
                        "created_at": "2026-08-09T01:02:03Z",
                        "duration": "4h",
                        "url": "https://www.twitch.tv/videos/1",
                    }
                ]
            }
        )
        with mock.patch.object(
            vod_clip_manager.requests, "get", return_value=response
        ):
            vod = vod_clip_manager.find_matching_vod(
                "user-456", "", "", "client", "token"
            )
        self.assertIsNone(vod)

    def test_missing_user_id_skips_vod_api(self):
        with mock.patch.object(vod_clip_manager.requests, "get") as request:
            vod = vod_clip_manager.find_matching_vod(
                "", "stream-123", "2026-08-09T01:02:03Z", "client", "token"
            )
        self.assertIsNone(vod)
        request.assert_not_called()

    def test_vod_lookup_reloads_shared_access_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "tokens.json"
            token_file.write_text(
                json.dumps({"access_token": "fresh-token"}), encoding="utf-8"
            )
            manager = vod_clip_manager.VodClipManager(
                Path(temporary),
                "yaritaiji",
                "stream-123",
                "user-456",
                "2026-08-09T01:02:03Z",
                "client",
                "stale-token",
                30,
            )
            with mock.patch.dict(
                os.environ, {"TWITCH_TOKEN_FILE": str(token_file)}
            ), mock.patch.object(
                vod_clip_manager, "find_matching_vod", return_value=None
            ) as lookup:
                manager._lookup_vod(force=True)
        self.assertEqual(lookup.call_args.args[-1], "fresh-token")


class HighlightCompilerTests(unittest.TestCase):
    def test_youtube_chapters_use_each_variable_clip_duration(self):
        chapters = highlight_compiler.build_youtube_chapters([
            {"rank": 1, "duration_seconds": 30.0},
            {"rank": 2, "duration_seconds": 45.0},
            {"rank": 3, "duration_seconds": 35.0},
        ])
        self.assertEqual(
            chapters.splitlines(),
            ["00:00 1位", "00:30 2位", "01:15 3位"],
        )


if __name__ == "__main__":
    unittest.main()
