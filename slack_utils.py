"""Slack utility functions shared across modules."""

import logging
import re
import time

logger = logging.getLogger(__name__)

# Member lists change over time, so their cache expires. Channel and user info
# are effectively immutable for our purposes and are cached for the process life.
MEMBER_EMAIL_CACHE_TTL = 600  # seconds

# Cache: channel_id -> ChannelInfo
_channel_info_cache: dict[str, dict] = {}
# Cache: user_id -> (display_name, username, email)
_user_info_cache: dict[str, tuple[str, str, str]] = {}
# Cache: channel_id -> (cached_at_monotonic, list of member emails)
_member_emails_cache: dict[str, tuple[float, list[str]]] = {}


def install_retry_handlers(client):
    """Make a Slack client retry on rate limits and connection errors.

    The SDK honours the Retry-After header, which is far more accurate than
    sprinkling fixed sleeps through the collection loops.
    """
    from slack_sdk.http_retry.builtin_handlers import (
        ConnectionErrorRetryHandler,
        RateLimitErrorRetryHandler,
    )

    installed = {type(h) for h in client.retry_handlers}
    for handler_cls in (ConnectionErrorRetryHandler, RateLimitErrorRetryHandler):
        if handler_cls not in installed:
            client.retry_handlers.append(handler_cls(max_retry_count=3))


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


def resolve_mentions(client, text: str) -> str:
    """Turn Slack's raw markup into what a reader sees in Slack.

    Message text arrives with mentions encoded as ids — <@U012ABC>, <#C012ABC>,
    <!here> — so a log of it is unreadable without expanding them.
    """
    if not text:
        return text

    def user(match: "re.Match") -> str:
        user_id, _, label = match.group(1).partition("|")
        if label:
            return f"@{label}"
        _, username, _ = get_user_info(client, user_id)
        return f"@{username}"

    def channel(match: "re.Match") -> str:
        channel_id, _, label = match.group(1).partition("|")
        if label:
            return f"#{label}"
        return f"#{get_channel_info(client, channel_id)['name']}"

    def special(match: "re.Match") -> str:
        # <!here>, <!channel>, <!subteam^S123|@group>, <!date^...^fallback>
        body = match.group(1)
        if "|" in body:
            label = body.split("|", 1)[1]
            return label if label.startswith("@") else f"@{label}"
        return f"@{body.split('^')[0]}"

    def link(match: "re.Match") -> str:
        url, _, label = match.group(1).partition("|")
        return label or url

    text = re.sub(r"<@([^>]+)>", user, text)
    text = re.sub(r"<#([^>]+)>", channel, text)
    text = re.sub(r"<!([^>]+)>", special, text)
    text = re.sub(r"<((?:https?|mailto):[^>]+)>", link, text)

    # Slack escapes these three in message text
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def invalidate_members(channel_id: str):
    """Drop the cached member list so the next read reflects a join or leave."""
    _member_emails_cache.pop(channel_id, None)


def get_member_emails(client, channel_id: str) -> list[str]:
    """Get Google-compatible email addresses for all members of a channel.

    Cached with a TTL so that members who join later still get access granted.
    """
    cached = _member_emails_cache.get(channel_id)
    if cached and (time.monotonic() - cached[0]) < MEMBER_EMAIL_CACHE_TTL:
        return cached[1]

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
            # Keep serving the stale list rather than silently dropping members
            # from the share list on a transient API failure.
            if cached:
                return cached[1]
            break

    emails = []
    for uid in member_ids:
        _, _, email = get_user_info(client, uid)
        if email:
            emails.append(email)
        else:
            logger.warning(f"No email for user {uid}, skipping permission grant")

    _member_emails_cache[channel_id] = (time.monotonic(), emails)
    return emails
