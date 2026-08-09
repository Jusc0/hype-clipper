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
                    command = vps_worker.build_probe_command("yaritaiji")
        self.assertIn("--no-preview-server", command)
        self.assertEqual(command[command.index("--channel") + 1], "yaritaiji")
        self.assertEqual(command[command.index("--highlight-seconds") + 1], "30")
        self.assertEqual(command[command.index("--preroll-seconds") + 1], "5")
        self.assertEqual(command[command.index("--top-count") + 1], "10")

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
        self.output = Path(self.temporary.name)
        self.app = hype_web.create_app(self.output)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def test_waiting_page_and_public_status_hide_device_code(self):
        (self.output / "service_status.json").write_text(
            json.dumps(
                {
                    "state": "waiting_for_twitch_authorization",
                    "channel": "yaritaiji",
                    "user_code": "SECRET-CODE",
                }
            ),
            encoding="utf-8",
        )
        page = self.client.get("/reactions.html")
        status = self.client.get("/api/status")
        self.assertEqual(page.status_code, 200)
        self.assertIn("waiting_for_twitch_authorization", page.get_data(as_text=True))
        self.assertNotIn("SECRET-CODE", status.get_data(as_text=True))
        self.assertEqual(status.json["channel"], "yaritaiji")

    def test_video_supports_byte_range_requests(self):
        media = bytes(range(100))
        (self.output / "highlight_chat_1.mp4").write_bytes(media)
        response = self.client.get(
            "/highlight_chat_1.mp4", headers={"Range": "bytes=10-19"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, media[10:20])
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/100")
        response.close()

    def test_non_highlight_files_are_not_public(self):
        (self.output / "chat.jsonl").write_text("secret", encoding="utf-8")
        self.assertEqual(self.client.get("/chat.jsonl").status_code, 404)


if __name__ == "__main__":
    unittest.main()
