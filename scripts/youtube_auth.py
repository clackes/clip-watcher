"""One-time YouTube login: turns a Google OAuth client file into a reusable token.

    python scripts/youtube_auth.py secrets/client_secret.json secrets/youtube_token.json

Opens a browser; sign in with the Google account that owns the ClipVault channel and pick that channel.
Then set YOUTUBE_CREDENTIALS=secrets/youtube_token.json in .env (copy both files to the NAS too).
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def main() -> None:
    client, out = Path(sys.argv[1]), Path(sys.argv[2])
    creds = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES).run_local_server(port=0)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(creds.to_json())
    out.chmod(0o600)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
