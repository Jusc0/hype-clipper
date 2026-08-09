"""Small production web surface for Hype Clipper output files."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re

from flask import Flask, Response, abort, jsonify, redirect, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix


MEDIA_NAME_RE = re.compile(
    r"(?:preview_[A-Za-z0-9_-]+|highlight(?:_chat)?(?:_[0-9]+)?)\.mp4"
)
PUBLIC_STATUS_FIELDS = {
    "state",
    "updated_at",
    "channel",
    "exit_code",
    "message",
}


def create_app(data_dir: str | Path | None = None) -> Flask:
    output_dir = Path(
        data_dir or os.environ.get("HYPE_DATA_DIR", "/data")
    ).resolve()
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["HYPE_DATA_DIR"] = output_dir

    def status_payload() -> dict:
        status_file = output_dir / "service_status.json"
        try:
            raw = json.loads(status_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "not_started"}
        except (OSError, json.JSONDecodeError):
            return {"state": "unknown"}
        return {key: raw[key] for key in PUBLIC_STATUS_FIELDS if key in raw}

    @app.after_request
    def cache_policy(response):
        if response.mimetype in {"text/html", "application/json"}:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/")
    def index():
        return redirect("/reactions.html", code=302)

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/status")
    def api_status():
        return jsonify(status_payload())

    @app.get("/api/highlights")
    def api_highlights():
        files = []
        for path in output_dir.glob("*.mp4"):
            if not MEDIA_NAME_RE.fullmatch(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": path.name,
                    "url": f"/{path.name}",
                    "bytes": stat.st_size,
                    "updated_at": stat.st_mtime,
                }
            )
        files.sort(key=lambda item: (-item["updated_at"], item["name"]))
        return jsonify(highlights=files, status=status_payload())

    @app.get("/reactions.html")
    def reactions():
        page = output_dir / "reactions.html"
        if page.is_file():
            return send_from_directory(
                output_dir, page.name, conditional=True, max_age=0
            )
        state = html.escape(str(status_payload().get("state", "not_started")))
        return Response(
            "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"refresh\" content=\"10\"><title>Hype Clipper</title>"
            "<style>body{font-family:system-ui;background:#0e0e10;color:#efeff1;"
            "display:grid;place-items:center;min-height:100vh;margin:0}main{padding:28px;"
            "background:#18181b;border-radius:14px}small{color:#adadb8}</style></head>"
            f"<body><main><h1>Hype Clipper</h1><p>ハイライトを準備中です。</p>"
            f"<small>状態: {state}</small></main></body></html>",
            mimetype="text/html",
        )

    @app.get("/<path:filename>")
    def media(filename: str):
        if "/" in filename or not MEDIA_NAME_RE.fullmatch(filename):
            abort(404)
        path = output_dir / filename
        if not path.is_file():
            abort(404)
        return send_from_directory(
            output_dir,
            filename,
            conditional=True,
            max_age=60,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
