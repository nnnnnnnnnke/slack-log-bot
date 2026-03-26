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
        self.root_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        # Cache: channel_name -> folder_id
        self._channel_folders: dict[str, str] = {}

    def _share_with_anyone(self, file_id: str):
        """Make a file/folder readable by anyone with the link."""
        self.service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

    def _share_with_emails(self, file_id: str, emails: list[str]):
        """Share a file/folder with specific email addresses."""
        for email in emails:
            try:
                self.service.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": "reader", "emailAddress": email},
                    sendNotificationEmail=False,
                ).execute()
            except Exception as e:
                logger.warning(f"Failed to share with {email}: {e}")

    def _get_or_create_channel_folder(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None
    ) -> str:
        """Get or create a subfolder for the channel under the root folder."""
        if channel_name in self._channel_folders:
            return self._channel_folders[channel_name]

        # Search for existing folder
        query = (
            f"name = '#{channel_name}' and "
            f"'{self.root_folder_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false"
        )
        results = self.service.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute()
        files = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
        else:
            # Create new folder
            folder_metadata = {
                "name": f"#{channel_name}",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [self.root_folder_id],
            }
            folder = self.service.files().create(
                body=folder_metadata, fields="id"
            ).execute()
            folder_id = folder["id"]

            # Set permissions based on channel type
            if is_private and member_emails:
                self._share_with_emails(folder_id, member_emails)
                logger.info(f"Created private Drive folder: #{channel_name} (shared with {len(member_emails)} members)")
            else:
                self._share_with_anyone(folder_id)
                logger.info(f"Created public Drive folder: #{channel_name}")

        self._channel_folders[channel_name] = folder_id
        return folder_id

    def upload_file(
        self,
        file_name: str,
        file_bytes: bytes,
        mime_type: str,
        channel_name: str,
        is_private: bool = False,
        member_emails: list[str] | None = None,
    ) -> str:
        """Upload a file to the channel's folder and return its shareable link."""
        folder_id = self._get_or_create_channel_folder(channel_name, is_private, member_emails)

        file_metadata = {
            "name": file_name,
            "parents": [folder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes), mimetype=mime_type, resumable=True
        )
        uploaded = (
            self.service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )

        # Set permissions based on channel type
        if is_private and member_emails:
            self._share_with_emails(uploaded["id"], member_emails)
        else:
            self._share_with_anyone(uploaded["id"])

        logger.info(f"Uploaded to Drive: #{channel_name}/{file_name} -> {uploaded['webViewLink']}")
        return uploaded["webViewLink"]

    def download_from_slack_and_upload(
        self,
        file_info: dict,
        slack_token: str,
        channel_name: str,
        is_private: bool = False,
        member_emails: list[str] | None = None,
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
            return self.upload_file(
                file_name, resp.content, mime_type,
                channel_name, is_private, member_emails,
            )
        except Exception as e:
            logger.error(f"Failed to upload to Drive: {e}")
            return None
