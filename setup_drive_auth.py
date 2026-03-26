"""One-time OAuth2 setup for Google Drive access.

Service accounts have no Drive storage quota, so we need a real Google account
for file uploads. This script runs the OAuth2 flow once and saves the token.

Prerequisites:
  1. Go to Google Cloud Console > APIs & Services > Credentials
  2. Create an OAuth 2.0 Client ID (type: Desktop app)
  3. Download the JSON and save as client_secret.json in this directory

Usage:
    python setup_drive_auth.py
"""

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_CLIENT_FILE = "client_secret.json"
DEFAULT_TOKEN_FILE = "drive_token.json"


def main():
    client_file = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", DEFAULT_CLIENT_FILE)
    token_file = os.environ.get("GOOGLE_DRIVE_TOKEN_FILE", DEFAULT_TOKEN_FILE)

    if not os.path.exists(client_file):
        print(f"Error: {client_file} not found.")
        print()
        print("To create it:")
        print("  1. Go to Google Cloud Console > APIs & Services > Credentials")
        print("  2. Click 'Create Credentials' > 'OAuth client ID'")
        print("  3. Choose 'Desktop app' as the application type")
        print("  4. Download the JSON file")
        print(f"  5. Save it as {client_file} in this directory")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes),
    }
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"Token saved to {token_file}")
    print("Google Drive uploads will now use your account's storage quota.")


if __name__ == "__main__":
    main()
