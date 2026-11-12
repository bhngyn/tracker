from datetime import datetime, timezone


def normalize_twitter(item: dict, source) -> dict | None:
    """Normalize Apify Twitter/X scraper output.

    Supports kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest
    (current) and apidojo/tweet-scraper (legacy). Handles both camelCase and
    snake_case field names.
    """
    post_id = item.get("id") or item.get("id_str") or item.get("tweetId")
    if not post_id:
        return None

    text = item.get("full_text") or item.get("text") or item.get("contentText") or ""

    # Author — Apify nests in "author" obj; legacy API uses "user"
    author = item.get("author") or item.get("user") or {}
    username = (
        author.get("userName")
        or author.get("screen_name")
        or source.username
    )

    # Timestamp — Apify: ISO "createdAt"; legacy: Twitter format "created_at"
    timestamp = _parse_timestamp(
        item.get("createdAt") or item.get("created_at")
    )

    # URL
    post_url = (
        item.get("url")
        or item.get("twitterUrl")
        or f"https://x.com/{username}/status/{post_id}"
    )

    # Engagement — try Apify camelCase first, fallback to legacy snake_case
    likes = (
        item.get("likeCount")
        or item.get("favorite_count")
        or (item.get("engagement") or {}).get("likeCount")
        or 0
    )
    retweets = (
        item.get("retweetCount")
        or item.get("retweet_count")
        or (item.get("engagement") or {}).get("retweetCount")
        or 0
    )
    replies = (
        item.get("replyCount")
        or item.get("reply_count")
        or (item.get("engagement") or {}).get("replyCount")
        or 0
    )
    quotes = (
        item.get("quoteCount")
        or item.get("quote_count")
        or (item.get("engagement") or {}).get("quoteCount")
        or 0
    )
    bookmarks = (
        item.get("bookmarkCount")
        or item.get("bookmark_count")
        or (item.get("engagement") or {}).get("bookmarkCount")
        or 0
    )
    raw_views = item.get("views")
    views = (
        item.get("viewCount")
        or (raw_views.get("count") if isinstance(raw_views, dict) else raw_views)
        or (item.get("engagement") or {}).get("viewCount")
        or 0
    )

    # Entities
    entities = item.get("entities") or {}
    hashtags = [h.get("text", "") for h in entities.get("hashtags", [])]
    mentions = [m.get("screen_name", "") for m in entities.get("user_mentions", [])]
    urls = [
        {
            "short_url": u.get("url", ""),
            "expanded_url": u.get("expanded_url", ""),
            "display_url": u.get("display_url", ""),
        }
        for u in entities.get("urls", [])
    ]

    # Conversation / threading
    conversation_id = item.get("conversationId") or item.get("conversation_id")
    in_reply_to = (
        item.get("inReplyToId")
        or item.get("in_reply_to_status_id")
        or item.get("in_reply_to_status_id_str")
    )
    in_reply_to_user = item.get("in_reply_to_screen_name")
    is_quote = item.get("is_quote_status") or item.get("isQuote", False)
    is_retweet = (
        item.get("retweeted_status") is not None
        or item.get("isRetweet", False)
    )
    is_reply = item.get("isReply", in_reply_to is not None)

    return {
        "platform": "twitter",
        "platform_post_id": str(post_id),
        "post_url": post_url,
        "text_content": text,
        "post_timestamp": timestamp,
        "source_username": username,
        "engagement": {
            "likes": _int(likes),
            "retweets": _int(retweets),
            "replies": _int(replies),
            "quotes": _int(quotes),
            "bookmarks": _int(bookmarks),
            "views": _int(views),
        },
        "platform_data": {
            "is_retweet": is_retweet,
            "is_quote": is_quote,
            "is_reply": is_reply,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "reply_to_id": str(in_reply_to) if in_reply_to else None,
            "reply_to_user": in_reply_to_user,
            "hashtags": hashtags,
            "mentions": mentions,
            "urls": urls,
            "language": item.get("lang"),
            "source_app": item.get("source"),
            "possibly_sensitive": item.get("possibly_sensitive", False),
        },
        "raw_metadata": item,
    }


def _parse_timestamp(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, str):
        # ISO 8601 (Apify format). Do NOT sniff for "T" — Twitter's legacy
        # format contains "Tue"/"Thu", which would take this branch and fail.
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
        # Twitter's legacy format: "Fri Jun 28 18:57:07 +0000 2024"
        try:
            return datetime.strptime(val, "%a %b %d %H:%M:%S %z %Y")
        except (ValueError, TypeError):
            return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None
    return None


def _int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
