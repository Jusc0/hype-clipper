import pickle
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

CLIENT_SECRETS_FILE = Path("client_secret.json")
TOKEN_FILE = Path("token.pickle")


def main():
    if not CLIENT_SECRETS_FILE.exists():
        raise FileNotFoundError(
            f"{CLIENT_SECRETS_FILE} が見つかりません"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRETS_FILE),
        SCOPES,
    )

    creds = flow.run_local_server(
        host="localhost",
        port=0,
        open_browser=True,
    )

    with TOKEN_FILE.open("wb") as f:
        pickle.dump(creds, f)

    print()
    print(f"作成完了: {TOKEN_FILE.resolve()}")


if __name__ == "__main__":
    main()