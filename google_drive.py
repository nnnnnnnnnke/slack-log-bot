"""Google Drive handler for uploading Slack attachments."""

import io
import logging
import requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveHandler:
    def __init__(self):
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        self.service = build("drive", "v3", credentials=creds)
        self.folder_id = config.GOOGLE_DRIVE_FOLDER_ID

    def upload_file(self, file_name: str, file_bytes: bytes, mime_type: str) -> str:
        """Upload a file to Google Drive and return its shareable link."""
        file_metadata = {
            "name": file_name,
            "parents": [self.folder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes), mimetype=mime_type, resumable=True
        )
        uploaded = (
            self.service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )

        # Make file readable by anyone with the link
        self.service.permissions().create(
            fileId=uploaded["id"],
            body={"type": "anyone", "role": "reader"},
        ).execute()

        logger.info(f"Uploaded to Drive: {file_name} -> {uploaded['webViewLink']}")
        return uploaded["webViewLink"]

    def download_from_slack_and_upload(
        self, file_info: dict, slack_token: str
    ) -> str | None:
        """Download a file from Slack and upload it to Google Drive.

        Returns the Drive link or None if failed.
        """
        file_size = file_info.get("size", 0)
        if file_size > config.MAX_FILE_SIZE:
            logger.warning(
                f"Skipping large file: {file_info.get('name')} ({file_size} bytes)"
            )
            return None

        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            logger.warning(f"No download URL for file: {file_info.get('name')}")
            return None

        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {slack_token}"},
                timeout=60,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Failed to download from Slack: {e}")
            return None

        file_name = file_info.get("name", "unknown_file")
        mime_type = file_info.get("mimetype", "application/octet-stream")

        try:
            return self.upload_file(file_name, resp.content, mime_type)
        except Exception as e:
            logger.error(f"Failed to upload to Drive: {e}")
            return None
