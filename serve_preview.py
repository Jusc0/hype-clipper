import argparse
import threading
from pathlib import Path

from twitch_reaction_probe import PreviewServer


def main():
    parser = argparse.ArgumentParser(description="Serve Hype Clipper results on the LAN")
    parser.add_argument("--out", default="reaction_session")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    output_dir = Path(args.out)
    html_path = output_dir / "reactions.html"
    if not html_path.exists():
        parser.error(f"preview HTML not found: {html_path}")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    server = PreviewServer(output_dir, preferred_port=args.port)
    server.start()
    print(f"PC preview   : {server.url}", flush=True)
    print(f"phone preview: {server.phone_url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
