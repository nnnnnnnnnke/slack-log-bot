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

# Requests that need telling a shared drive exists. A files() or permissions()
# call without supportsAllDrives fails as a 404 on a file that plainly exists,
# and files().list quietly returns nothing at all — the confusing shape of
# failure, so the flags are filled in here rather than at forty call sites
# where the one that gets forgotten is the one that breaks.
_ALL_DRIVES_METHODS = {
    "files": {"get", "list", "create", "update", "copy", "delete"},
    "permissions": {"get", "list", "create", "update", "delete"},
}


class _AllDrives:
    """A Drive collection that passes the shared-drive flags on every call."""

    def __init__(self, inner, methods: set[str], is_files: bool, drive_id: str = ""):
        self._inner = inner
        self._methods = methods
        self._is_files = is_files
        self._drive_id = drive_id

    def __getattr__(self, name):
        method = getattr(self._inner, name)
        if name not in self._methods:
            return method

        def call(**kwargs):
            kwargs.setdefault("supportsAllDrives", True)
            if name == "list" and self._is_files:
                kwargs.setdefault("includeItemsFromAllDrives", True)
                if self._drive_id:
                    # Naming the drive is the documented way to search one.
                    # A parent clause alone runs against the default corpus,
                    # which need not reach into a shared drive — and a search
                    # that comes back empty reads as "not created yet", so the
                    # bot builds a second spreadsheet beside the first.
                    kwargs.setdefault("corpora", "drive")
                    kwargs.setdefault("driveId", self._drive_id)
            return method(**kwargs)

        return call


class _DriveService:
    """The Drive client, wrapping the two collections that need the flags."""

    def __init__(self, service):
        self._service = service
        self._drive_id = ""

    def bind_drive(self, drive_id: str | None):
        """Search inside this shared drive from here on. None for My Drive."""
        self._drive_id = drive_id or ""

    def files(self):
        return _AllDrives(
            self._service.files(), _ALL_DRIVES_METHODS["files"], True, self._drive_id
        )

    def permissions(self):
        return _AllDrives(
            self._service.permissions(), _ALL_DRIVES_METHODS["permissions"], False
        )

    def __getattr__(self, name):
        return getattr(self._service, name)


def drive_service(credentials=None):
    """A Drive client that works in My Drive and in a shared drive alike."""
    return _DriveService(
        build("drive", "v3", credentials=credentials or load_credentials())
    )


def shared_drive_id(service, file_id: str) -> str | None:
    """The shared drive holding this file, or None when it sits in My Drive.

    Which one it is decides who can read the logs. In My Drive the bot shares
    each channel's file with that channel's members and takes the share back
    when they leave. A shared drive answers that question itself: membership
    is the drive's, it cannot be narrowed per file, and a share the bot added
    on top could not be removed again. So the bot stops sharing and lets the
    drive decide.
    """
    try:
        return service.files().get(
            fileId=file_id, fields="driveId"
        ).execute().get("driveId")
    except Exception as e:
        logger.warning(f"Could not tell whether {file_id} is in a shared drive: {e}")
        return None

# Channel folders live under this, so the root folder shows the spreadsheets
# rather than one folder per channel mixed in among them.
ATTACHMENTS_FOLDER_NAME = "添付ファイル"

# Drive appProperty tying a channel folder to its Slack channel, so a rename
# does not strand the files under the old name.
CHANNEL_ID_PROPERTY = "slackChannelId"


# Drive roles, weakest first, so a share is only ever raised.
ROLE_RANK = {
    "reader": 1, "commenter": 2, "writer": 3,
    "fileOrganizer": 4, "organizer": 5, "owner": 6,
}


def sync_permissions(
    service, file_id: str, member_emails: list[str],
    revoke: list[str] | None = None, role: str = "reader",
) -> tuple[int, int]:
    """Bring a file's sharing in line with a channel's membership.

    Returns (granted, revoked). Someone already holding a stronger role than
    `role` keeps it, so a share set up by hand is not quietly downgraded. A
    weaker one is raised, which is how a file shared before the wanted role
    changed catches up without a migration.

    `revoke` is an explicit list rather than "anyone not in member_emails" —
    a member whose email cannot be resolved would otherwise look like someone
    to remove, and the whole channel would lose access over a failed lookup.
    """
    try:
        existing = service.permissions().list(
            fileId=file_id, fields="permissions(id,type,role,emailAddress)"
        ).execute().get("permissions", [])
    except Exception as e:
        logger.warning(f"Could not read permissions for {file_id}: {e}")
        return (0, 0)

    by_email = {
        p.get("emailAddress", "").lower(): p
        for p in existing
        if p.get("type") == "user" and p.get("emailAddress")
    }

    granted = 0
    for email in member_emails:
        current = by_email.get(email.lower())
        try:
            if current is None:
                service.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": role, "emailAddress": email},
                    sendNotificationEmail=False,
                ).execute()
                granted += 1
            elif ROLE_RANK.get(current.get("role"), 0) < ROLE_RANK.get(role, 0):
                service.permissions().update(
                    fileId=file_id, permissionId=current["id"], body={"role": role},
                ).execute()
        except Exception as e:
            logger.warning(f"Failed to share {file_id} with {email}: {e}")

    revoked = 0
    for email in revoke or []:
        permission = by_email.get(email.lower())
        # Whoever left is dropped whatever role they hold, but the owner is in
        # this list too and cannot be removed from their own file.
        if not permission or permission.get("role") == "owner":
            continue
        try:
            service.permissions().delete(
                fileId=file_id, permissionId=permission["id"]
            ).execute()
            revoked += 1
        except Exception as e:
            logger.warning(f"Failed to revoke {email} on {file_id}: {e}")

    return (granted, revoked)


class DriveHandler:
    def __init__(self):
        self.service = drive_service()
        self.root_folder_id = config.GOOGLE_DRIVE_FOLDER_ID
        self.shared_drive_id = shared_drive_id(self.service, self.root_folder_id)
        self.service.bind_drive(self.shared_drive_id)
        if self.shared_drive_id:
            logger.info(
                "Attachments live in a shared drive; who can read them is the "
                "drive's membership, not something the bot sets per channel."
            )
        # Cache: channel_name -> folder_id
        self._channel_folders: dict[str, str] = {}
        self._attachments_root: str | None = None

    def _share_with_emails(self, file_id: str, emails: list[str]) -> int:
        """Share a file/folder with specific email addresses. Returns the count."""
        if self.shared_drive_id:
            return 0
        shared = 0
        for email in emails:
            try:
                self.service.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": "reader", "emailAddress": email},
                    sendNotificationEmail=False,
                ).execute()
                shared += 1
            except Exception as e:
                logger.warning(f"Failed to share with {email}: {e}")
        return shared

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

    def _find_by_channel_id(self, channel_id: str, parent: str) -> dict | None:
        if not channel_id:
            return None
        query = (
            f"appProperties has {{ key='{CHANNEL_ID_PROPERTY}' and value='{channel_id}' }} "
            f"and '{parent}' in parents and trashed = false"
        )
        try:
            files = self.service.files().list(
                q=query, fields="files(id, name)", pageSize=1
            ).execute().get("files", [])
        except Exception as e:
            logger.warning(f"Could not search folders by channel id: {e}")
            return None
        return files[0] if files else None

    def _stamp_channel_id(self, file_id: str, channel_id: str, name: str | None = None):
        body = {"appProperties": {CHANNEL_ID_PROPERTY: channel_id}}
        if name:
            body["name"] = name
        try:
            self.service.files().update(fileId=file_id, body=body, fields="id").execute()
        except Exception as e:
            logger.warning(f"Could not stamp folder {file_id}: {e}")

    def rename_channel(self, channel_id: str, new_name: str) -> bool:
        """Follow a channel rename so uploads keep landing in the same folder."""
        attachments_root = self._get_attachments_root()
        found = self._find_by_channel_id(channel_id, attachments_root)
        if not found:
            return False
        if found["name"] != f"#{new_name}":
            self._stamp_channel_id(found["id"], channel_id, f"#{new_name}")
        self._channel_folders.clear()
        return True

    def _get_or_create_channel_folder(
        self, channel_name: str, member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> str:
        """Get or create the channel's folder under the attachments folder.

        Located by the channel id stamped on it, so a renamed channel keeps
        uploading into the folder that already holds its files.
        """
        if channel_name in self._channel_folders:
            return self._channel_folders[channel_name]

        attachments_root = self._get_attachments_root()
        folder_name = f"#{channel_name}"

        stamped = self._find_by_channel_id(channel_id, attachments_root)
        if stamped:
            if stamped["name"] != folder_name:
                self._stamp_channel_id(stamped["id"], channel_id, folder_name)
            self._channel_folders[channel_name] = stamped["id"]
            return stamped["id"]

        folder_id = self._find_folder(folder_name, attachments_root)
        if folder_id and channel_id:
            self._stamp_channel_id(folder_id, channel_id)

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
                    "appProperties": {CHANNEL_ID_PROPERTY: channel_id} if channel_id else {},
                },
                fields="id",
            ).execute()
            folder_id = folder["id"]

            # Access follows the channel, not whether Slack calls it private:
            # the spreadsheet linking to these files is members-only too.
            shared = self._share_with_emails(folder_id, member_emails or [])
            logger.info(
                f"Created Drive folder: {folder_name} (shared with {shared} members)"
            )

        self._channel_folders[channel_name] = folder_id
        return folder_id

    def sync_channel_access(
        self, channel_name: str, member_emails: list[str],
        revoke: list[str] | None = None, channel_id: str = "",
    ) -> tuple[int, int]:
        """Match the channel folder's readers to the channel's membership."""
        folder_id = self._get_or_create_channel_folder(
            channel_name, member_emails, channel_id
        )
        if self.shared_drive_id:
            return (0, 0)
        return sync_permissions(self.service, folder_id, member_emails, revoke)

    def upload_file(
        self,
        file_name: str,
        file_bytes: bytes,
        mime_type: str,
        channel_name: str,
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> tuple[str, str]:
        """Upload a file to the channel's folder. Returns (file name, link)."""
        folder_id = self._get_or_create_channel_folder(
            channel_name, member_emails, channel_id
        )

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

        # The parent folder's permissions already cover the file; sharing it
        # again would double the Drive calls for no added access.

        logger.info(f"Uploaded to Drive: #{channel_name}/{file_name} -> {uploaded['webViewLink']}")
        return (file_name, uploaded["webViewLink"])

    def download_from_slack_and_upload(
        self,
        file_info: dict,
        slack_token: str,
        channel_name: str,
        member_emails: list[str] | None = None,
        channel_id: str = "",
    ) -> tuple[str, str] | None:
        """Download a file from Slack and upload it to Google Drive.

        Returns (file name, Drive link), or None if it could not be stored.
        The name travels with the link so the sheet can show the filename with
        the URL behind it.
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
                channel_name, member_emails, channel_id,
            )
        except Exception as e:
            logger.error(f"Failed to upload to Drive: {e}")
            return None
