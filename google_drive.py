"""Google Drive handler for uploading Slack attachments."""

import io
import logging
import requests

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import config
from google_auth import load_credentials

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"

# Channel folders live under this, so the root folder shows the spreadsheets
# rather than one folder per channel mixed in among them.
ATTACHMENTS_FOLDER_NAME = "添付ファイル"


class DriveHandler:
    def __init__(self):
        self.service = build("drive", "v3", credentials=load_credentials())
        self.root_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        # Cache: channel_name -> folder_id
        self._channel_folders: dict[str, str] = {}
        self._attachments_root: str | None = None

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

    def _find_folder(self, name: str, parent: str) -> str | None:
        query = (
            f"name = '{name}' and '{parent}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        results = self.service.files().list(
            q=query, fields="files(id, name)", pageSize=1
        ).execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None

    def _get_attachments_root(self) -> str:
        """The folder that holds the per-channel folders."""
        if self._attachments_root:
            return self._attachments_root

        folder_id = self._find_folder(ATTACHMENTS_FOLDER_NAME, self.root_folder_id)
        if not folder_id:
            created = self.service.files().create(
                body={
                    "name": ATTACHMENTS_FOLDER_NAME,
                    "mimeType": FOLDER_MIME,
                    "parents": [self.root_folder_id],
                },
                fields="id",
            ).execute()
            folder_id = created["id"]
            logger.info(f"Created attachments folder: {ATTACHMENTS_FOLDER_NAME}")

        self._attachments_root = folder_id
        return folder_id

    def _get_or_create_channel_folder(
        self, channel_name: str, is_private: bool = False, member_emails: list[str] | None = None
    ) -> str:
        """Get or create the channel's folder under the attachments folder."""
        if channel_name in self._channel_folders:
            return self._channel_folders[channel_name]

        attachments_root = self._get_attachments_root()
        folder_name = f"#{channel_name}"
        folder_id = self._find_folder(folder_name, attachments_root)

        if not folder_id:
            # Channel folders used to sit directly under the root folder. Adopt
            # one if it is still there, so uploads keep landing in the folder
            # that already holds this channel's files.
            legacy_id = self._find_folder(folder_name, self.root_folder_id)
            if legacy_id:
                self.service.files().update(
                    fileId=legacy_id,
                    addParents=attachments_root,
                    removeParents=self.root_folder_id,
                    fields="id",
                ).execute()
                folder_id = legacy_id
                logger.info(f"Moved {folder_name} under {ATTACHMENTS_FOLDER_NAME}")

        if not folder_id:
            folder = self.service.files().create(
                body={
                    "name": folder_name,
                    "mimeType": FOLDER_MIME,
                    "parents": [attachments_root],
                },
                fields="id",
            ).execute()
            folder_id = folder["id"]

            # Set permissions based on channel type
            if is_private and member_emails:
                self._share_with_emails(folder_id, member_emails)
                logger.info(f"Created private Drive folder: {folder_name} (shared with {len(member_emails)} members)")
            else:
                self._share_with_anyone(folder_id)
                logger.info(f"Created public Drive folder: {folder_name}")

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
            io.BytesIO(file_bytes), mimetype=mime_type, resumable=False
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
