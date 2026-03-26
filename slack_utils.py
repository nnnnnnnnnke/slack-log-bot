"""Slack utility functions shared across modules."""

import logging

logger = logging.getLogger(__name__)

# Cache: channel_id -> ChannelInfo
_channel_info_cache: dict[str, dict] = {}
# Cache: user_id -> (display_name, username, email)
_user_info_cache: dict[str, tuple[str, str, str]] = {}
# Cache: channel_id -> list of member emails
_member_emails_cache: dict[str, list[str]] = {}


def get_channel_info(client, channel_id: str) -> dict:
    """Get channel info including name and is_private flag."""
    if channel_id in _channel_info_cache:
        return _channel_info_cache[channel_id]
    try:
        result = client.conversations_info(channel=channel_id)
        ch = result["channel"]
        info = {
            "name": ch["name"],
            "is_private": ch.get("is_private", False) or ch.get("is_group", False),
        }
        _channel_info_cache[channel_id] = info
        return info
    except Exception as e:
        logger.error(f"Failed to get channel info {channel_id}: {e}")
        return {"name": channel_id, "is_private": False}


def get_user_info(client, user_id: str) -> tuple[str, str, str]:
    """Resolve Slack user ID to (display_name, username, email)."""
    if user_id in _user_info_cache:
        return _user_info_cache[user_id]
    try:
        result = client.users_info(user=user_id)
        user = result["user"]
        profile = user.get("profile", {})
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        username = user.get("name", user_id)
        email = profile.get("email", "")
        _user_info_cache[user_id] = (display_name, username, email)
        return (display_name, username, email)
    except Exception as e:
        logger.error(f"Failed to resolve user {user_id}: {e}")
        return (user_id, user_id, "")


def get_member_emails(client, channel_id: str) -> list[str]:
    """Get Google-compatible email addresses for all members of a channel."""
    if channel_id in _member_emails_cache:
        return _member_emails_cache[channel_id]

    member_ids = []
    cursor = None
    while True:
        try:
            resp = client.conversations_members(
                channel=channel_id, limit=200, cursor=cursor
            )
            member_ids.extend(resp.get("members", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        except Exception as e:
            logger.error(f"Failed to get channel members: {e}")
            break

    emails = []
    for uid in member_ids:
        _, _, email = get_user_info(client, uid)
        if email:
            emails.append(email)
        else:
            logger.warning(f"No email for user {uid}, skipping permission grant")

    _member_emails_cache[channel_id] = emails
    return emails
