from __future__ import annotations

import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


class YouTubeUploader:
    def __init__(
        self,
        client_secrets_file: str | Path,
        token_file: str | Path,
    ):
        self.client_secrets_file = Path(client_secrets_file)
        self.token_file = Path(token_file)

    def get_authenticated_service(self):
        creds = None

        if self.token_file.exists() and self.token_file.stat().st_size > 0:
            with self.token_file.open("rb") as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.client_secrets_file.exists():
                    raise FileNotFoundError(
                        f"YouTube client secrets not found: "
                        f"{self.client_secrets_file}"
                    )

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secrets_file),
                    YOUTUBE_SCOPES,
                )

                creds = flow.run_local_server(port=0)

            self.token_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self.token_file.open("wb") as token:
                pickle.dump(creds, token)

        return build(
            "youtube",
            "v3",
            credentials=creds,
        )

    def find_playlist(
        self,
        youtube,
        title: str,
    ) -> str | None:
        request = youtube.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50,
        )

        while request is not None:
            response = request.execute()

            for item in response.get("items", []):
                snippet = item.get("snippet", {})

                if snippet.get("title") == title:
                    playlist_id = str(item["id"])

                    print(
                        f"[youtube] playlist found: "
                        f"{title} ({playlist_id})",
                        flush=True,
                    )

                    return playlist_id

            request = youtube.playlists().list_next(
                request,
                response,
            )

        return None

    def create_playlist(
        self,
        youtube,
        title: str,
        privacy_status: str = "unlisted",
    ) -> str:
        response = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                },
                "status": {
                    "privacyStatus": privacy_status,
                },
            },
        ).execute()

        playlist_id = str(response["id"])

        print(
            f"[youtube] playlist created: "
            f"{title} ({playlist_id})",
            flush=True,
        )

        return playlist_id

    def get_or_create_playlist(
        self,
        youtube,
        title: str,
    ) -> str:
        playlist_id = self.find_playlist(
            youtube,
            title,
        )

        if playlist_id:
            return playlist_id

        return self.create_playlist(
            youtube,
            title,
            privacy_status="unlisted",
        )

    def add_video_to_playlist(
        self,
        youtube,
        playlist_id: str,
        video_id: str,
    ) -> None:
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                },
            },
        ).execute()

        print(
            f"[youtube] added video to playlist: "
            f"{playlist_id}",
            flush=True,
        )

    def upload(
        self,
        mp4_path: str | Path,
        title: str,
        description: str = "",
        privacy_status: str = "unlisted",
        playlist_title: str | None = None,
    ) -> str:
        mp4_path = Path(mp4_path)

        if not mp4_path.is_file():
            raise FileNotFoundError(
                f"Upload file not found: {mp4_path}"
            )

        if privacy_status not in {
            "private",
            "unlisted",
            "public",
        }:
            raise ValueError(
                f"Invalid privacy status: {privacy_status}"
            )

        youtube = self.get_authenticated_service()

        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": privacy_status,
                },
            },
            media_body=MediaFileUpload(
                str(mp4_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=8 * 1024 * 1024,
            ),
        )

        print(
            f"[youtube] uploading: {mp4_path.name}",
            flush=True,
        )

        response = None

        while response is None:
            status, response = request.next_chunk()

            if status is not None:
                progress = status.progress() * 100

                print(
                    f"[youtube] upload {progress:.1f}%",
                    flush=True,
                )

        video_id = str(
            response.get("id", "")
        ).strip()

        if not video_id:
            raise RuntimeError(
                "YouTube upload completed but no video ID was returned"
            )

        print(
            f"[youtube] upload complete: {video_id}",
            flush=True,
        )

        if playlist_title:
            playlist_id = self.get_or_create_playlist(
                youtube,
                playlist_title,
            )

            self.add_video_to_playlist(
                youtube,
                playlist_id,
                video_id,
            )

        return video_id