import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import hype_web
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
                        "PREROLL_SECONDS": "5",
                        "TOP_COUNT": "10",
                    },
                    clear=False,
                ):
                    command = vps_worker.build_probe_command(
                        "yaritaiji", Path(temporary) / "yaritaiji", 1234.5
                    )
        self.assertIn("--no-preview-server", command)
        self.assertEqual(command[command.index("--channel") + 1], "yaritaiji")
        self.assertEqual(command[command.index("--highlight-seconds") + 1], "30")
        self.assertEqual(command[command.index("--preroll-seconds") + 1], "5")
        self.assertEqual(command[command.index("--top-count") + 1], "10")
        self.assertEqual(
            command[command.index("--stream-started-at-epoch") + 1],
            "1234.5",
        )
        self.assertEqual(
            command[command.index("--out") + 1],
            str(Path(temporary) / "yaritaiji"),
        )

    def test_stream_start_comes_from_twitch_helix(self):
        response = FakeResponse(
            {"data": [{"started_at": "2026-08-09T01:02:03Z"}]}
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

    def test_dashboard_adds_two_channels_and_rejects_third(self):
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
        third = self.add_channel("third_channel")
        self.assertEqual(third.status_code, 409)
        channels = self.client.get("/api/channels").json["channels"]
        self.assertEqual(
            [item["channel"] for item in channels], ["yaritaiji", "shaka"]
        )

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

    def test_non_highlight_files_are_not_public(self):
        channel_dir = self.channel_output("yaritaiji")
        (channel_dir / "chat.jsonl").write_text("secret", encoding="utf-8")
        self.assertEqual(
            self.client.get("/channels/yaritaiji/chat.jsonl").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
