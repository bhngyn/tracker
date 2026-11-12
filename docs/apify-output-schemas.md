# Apify Scraper Output Schemas

Reference for the raw item shapes each Apify actor returns, per platform.

---

## Twitter/X — `kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest`

**Input shape:** `{ "from": "<handle>", "maxItems": 20, "queryType": "Latest" }` — `from` is the Twitter handle without `@`.

**Pagination caveat:** the actor returns ~20 tweets per call and pagination is unreliable; do not request `maxItems > 20`. Use per-source `poll_interval` to keep up with high-volume accounts.

```json
{
  "id": "1806763776881860918",
  "url": "https://x.com/elonmusk/status/1806763776881860918",
  "text": "Which #memecoin can do #1000x this year?",
  "createdAt": "Fri Jun 28 18:57:07 +0000 2024",
  "lang": "en",
  "source": "Twitter Web App",

  "likeCount": 104121,
  "retweetCount": 11311,
  "replyCount": 6526,
  "quoteCount": 2915,
  "bookmarkCount": 18500,
  "viewCount": 2500000,

  "conversationId": "1806763776881860918",
  "isReply": false,
  "inReplyToId": null,
  "isQuote": false,
  "isRetweet": false,

  "author": {
    "id": "44196397",
    "userName": "elonmusk",
    "name": "Elon Musk",
    "description": "CEO of Tesla, SpaceX",
    "location": "Earth",
    "followers": 175000000,
    "following": 780,
    "statusesCount": 42000,
    "isVerified": true,
    "isBlueVerified": true,
    "createdAt": "2007-04-17T01:46:27.000Z",
    "profilePicture": "https://pbs.twimg.com/profile_images/...",
    "bannerUrl": "https://pbs.twimg.com/profile_banners/..."
  },

  "entities": {
    "hashtags": [
      { "text": "memecoin", "indices": [6, 15] }
    ],
    "urls": [
      {
        "url": "https://t.co/abc12345xyz",
        "expanded_url": "https://example.com/article",
        "display_url": "example.com/article"
      }
    ],
    "user_mentions": [
      {
        "screen_name": "cryptodev",
        "name": "Crypto Dev",
        "id": 123456789
      }
    ],
    "media": [
      {
        "id": 1806763776881860918,
        "media_url_https": "https://pbs.twimg.com/media/example.jpg",
        "type": "photo"
      }
    ]
  }
}
```

### Normalizer Mapping — Twitter

| Apify Field | → DB Column | Notes |
|---|---|---|
| `id` | `platform_post_id` | |
| `url` | `post_url` | |
| `text` or `full_text` | `text_content` | |
| `createdAt` | `post_timestamp` | Twitter legacy format (`Wed May 06 11:09:39 +0000 2026`); normalizer also parses ISO 8601 |
| `author.userName` | `source_username` | Match to source |
| `likeCount` | `engagement.likes` | |
| `retweetCount` | `engagement.retweets` | |
| `replyCount` | `engagement.replies` | |
| `quoteCount` | `engagement.quotes` | |
| `bookmarkCount` | `engagement.bookmarks` | |
| `viewCount` | `engagement.views` | |
| `conversationId` | `platform_data.conversation_id` | |
| `inReplyToId` | `platform_data.reply_to_id` | New actor; legacy fallback: `in_reply_to_status_id` |
| `isReply` | `platform_data.is_reply` | New actor; legacy derived from `in_reply_to_status_id != null` |
| `isQuote` | `platform_data.is_quote` | New actor; legacy fallback: `is_quote_status` |
| `isRetweet` | `platform_data.is_retweet` | |
| `lang`, `source` | `platform_data.{language, source_app}` | |
| `entities` (hashtags, urls, mentions) | `platform_data.{hashtags, mentions, urls}` | JSONB |
| `entities.media[]` | → `media_files` rows | Download each |
| Full response | `raw_metadata` | JSONB |

---

## Instagram — `apify/instagram-scraper`

```json
{
  "type": "post",
  "id": "2987654321098765432",
  "shortCode": "AbCdEfGhIjK",
  "url": "https://www.instagram.com/p/AbCdEfGhIjK/",
  "productType": "feed",
  "caption": "Beautiful sunset #sunset #citylife @friend1",
  "timestamp": "2026-03-15T14:22:11.000Z",
  "likesCount": 12450,
  "commentCount": 312,
  "videoViewCount": null,
  "ownerUsername": "naturephotographer",
  "ownerFullName": "Nature Photographer",
  "ownerId": "98765432",
  "displayUrl": "https://scontent.cdninstagram.com/...",
  "dimensionsHeight": 1080,
  "dimensionsWidth": 1080,
  "isVideo": false,
  "videoUrl": null,
  "hashtags": ["sunset", "citylife"],
  "mentions": ["friend1"],
  "isSponsored": false,
  "locationName": "Golden Gate Bridge",
  "locationId": "213999620",
  "usertags": [
    {
      "username": "friend1",
      "userId": "123456789",
      "position": [0.45, 0.35]
    }
  ],
  "childPosts": [
    {
      "id": "2987654321098765432_1",
      "displayUrl": "https://scontent.cdninstagram.com/...",
      "type": "image",
      "dimensionsHeight": 1080,
      "dimensionsWidth": 1080
    },
    {
      "id": "2987654321098765432_2",
      "displayUrl": "https://scontent.cdninstagram.com/...",
      "type": "video",
      "videoUrl": "https://scontent.cdninstagram.com/...",
      "dimensionsHeight": 1080,
      "dimensionsWidth": 1080
    }
  ]
}
```

### Normalizer Mapping — Instagram

| Apify Field | → DB Column | Notes |
|---|---|---|
| `id` | `platform_post_id` | |
| `url` | `post_url` | |
| `caption` | `text_content` | |
| `timestamp` | `post_timestamp` | ISO 8601 |
| `ownerUsername` | `source_username` | |
| `likesCount` | `engagement.likes` | -1 means hidden |
| `commentCount` | `engagement.comments` | |
| `videoViewCount` | `engagement.views` | null if image |
| `productType`, `shortCode`, `isVideo`, `isSponsored`, `locationName`, `locationId`, `hashtags`, `mentions`, `usertags` | `platform_data` | JSONB |
| `displayUrl` | → `media_files` row | Download; type=image |
| `videoUrl` | → `media_files` row | Download; type=video |
| `childPosts[]` | → multiple `media_files` rows | Carousel items |
| Full response | `raw_metadata` | JSONB |

---

## Facebook — `apify/facebook-posts-scraper`

```json
{
  "postId": "123456789_987654321",
  "postUrl": "https://www.facebook.com/pagename/posts/987654321",
  "postText": "Check out our latest collection! 🎉",
  "postType": "status",
  "postDate": "2024-02-18T15:35:51.000Z",
  "authorId": "123456789",
  "authorName": "Example Brand",
  "authorUrl": "https://www.facebook.com/pagename",
  "authorProfilePic": "https://platform-lookaside.fbsbx.com/...",
  "isGroupPost": false,
  "groupUrl": null,
  "groupName": null,
  "postStats": {
    "comments": 87,
    "shares": 34,
    "reactions": 256
  },
  "reactionsCount": {
    "like": 180,
    "love": 48,
    "haha": 15,
    "wow": 8,
    "sad": 3,
    "angry": 2
  },
  "postImages": [
    {
      "link": "https://www.facebook.com/photo/?fbid=987654321",
      "image": "https://platform-lookaside.fbsbx.com/...",
      "width": 1200,
      "height": 800
    }
  ],
  "postLinks": [
    {
      "link": "https://example.com/shop",
      "linkTitle": "Shop Our Collection",
      "linkDescription": "Discover our latest products",
      "linkImage": "https://example.com/og-image.jpg"
    }
  ],
  "postVideos": [
    {
      "videoUrl": "https://www.facebook.com/video/?v=987654321",
      "videoLength": 45,
      "thumbnailUrl": "https://platform-lookaside.fbsbx.com/..."
    }
  ],
  "commentsCount": 87,
  "sharesCount": 34
}
```

### Normalizer Mapping — Facebook

| Apify Field | → DB Column | Notes |
|---|---|---|
| `postId` | `platform_post_id` | |
| `postUrl` | `post_url` | |
| `postText` | `text_content` | |
| `postDate` | `post_timestamp` | ISO 8601 |
| `authorName` | `source_username` | |
| `reactionsCount.like` + `love` + ... | `engagement.reactions` | Store full breakdown |
| `postStats.comments` | `engagement.comments` | |
| `postStats.shares` | `engagement.shares` | |
| `postType`, `isGroupPost`, `groupName`, `groupUrl`, `authorId`, `authorUrl`, `postLinks`, `reactionsCount` | `platform_data` | JSONB |
| `postImages[]` | → `media_files` rows | Download each .image |
| `postVideos[]` | → `media_files` rows | Download each .videoUrl |
| Full response | `raw_metadata` | JSONB |

---

## TikTok — `clockworks/tiktok-scraper`

```json
{
  "id": "7619450446956006686",
  "text": "wise words from @CharliePuth...",
  "textLanguage": "en",
  "createTime": "1742504363",
  "createTimeISO": "2026-03-20T21:19:23.000Z",
  "webVideoUrl": "https://www.tiktok.com/@tiktok/video/7619450446956006686",
  "locationCreated": "US",
  "isAd": false,
  "isPinned": false,
  "isSlideshow": false,

  "diggCount": 1263,
  "shareCount": 44,
  "commentCount": 285,
  "playCount": 45300,
  "collectCount": 147,

  "coverUrl": "https://p19-sign.tiktokcdn.com/...",
  "dynamicCover": "https://p19-sign.tiktokcdn.com/...",

  "hashtags": [
    { "id": "1621844", "name": "musicproduction", "title": "Music Production" }
  ],
  "mentions": [],
  "effectStickers": [
    { "id": "123456", "name": "Effect Name" }
  ],

  "videoMeta": {
    "width": 1080,
    "height": 1920,
    "ratio": "9:16",
    "duration": 15,
    "downloadAddr": "https://v16-web.tiktok.com/..."
  },

  "musicMeta": {
    "musicId": "1234567890123456",
    "musicName": "Original Sound",
    "musicAuthor": "Charlie Puth",
    "musicOriginal": true,
    "musicAlbum": "Album Name",
    "playUrl": "https://sf16-ies-music-va.tiktokcdn.com/...",
    "coverUrl": "https://p16-sg.tiktokcdn.com/..."
  },

  "authorMeta": {
    "id": "123456789",
    "name": "Charlie Puth",
    "nickName": "charlieputh",
    "profileUrl": "https://www.tiktok.com/@charlieputh",
    "verified": true,
    "signature": "Grammy Award Winner",
    "avatar": "https://p16-sg.tiktokcdn.com/...",
    "fans": 45600000,
    "following": 15200000,
    "heart": 1200000000,
    "video": 450
  }
}
```

### Normalizer Mapping — TikTok

| Apify Field | → DB Column | Notes |
|---|---|---|
| `id` | `platform_post_id` | |
| `webVideoUrl` | `post_url` | |
| `text` | `text_content` | |
| `createTimeISO` | `post_timestamp` | Prefer ISO; fallback `createTime` (unix) |
| `authorMeta.nickName` or `.name` | `source_username` | |
| `diggCount` | `engagement.likes` | |
| `shareCount` | `engagement.shares` | |
| `commentCount` | `engagement.comments` | |
| `playCount` | `engagement.views` | |
| `collectCount` | `engagement.saves` | |
| `textLanguage`, `locationCreated`, `isAd`, `isPinned`, `isSlideshow`, `hashtags`, `mentions`, `effectStickers`, `musicMeta` | `platform_data` | JSONB |
| `videoMeta.downloadAddr` | → `media_files` row | type=video; use yt-dlp for better quality |
| `coverUrl` | → `media_files` row | type=image (thumbnail) |
| Full response | `raw_metadata` | JSONB |

---

## YouTube — `streamers/youtube-scraper`

```json
{
  "id": "dQw4w9WgXcQ",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "title": "Never Gonna Give You Up",
  "description": "Official music video for Rick Astley...",
  "uploadDate": "2009-10-25T06:57:33Z",

  "viewCount": 1234567890,
  "likeCount": 12500000,
  "commentCount": 1250000,

  "duration": "PT3M32S",
  "durationSeconds": 212,

  "channel": {
    "id": "UCuAXFkgsw1L7xaCfnd5J6xw",
    "name": "Rick Astley Official",
    "url": "https://www.youtube.com/@RickAstley",
    "handle": "@RickAstley"
  },
  "channelSubscribers": 18500000,

  "thumbnailUrl": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
  "thumbnails": [
    { "url": "https://i.ytimg.com/vi/.../default.jpg", "width": 120, "height": 90 },
    { "url": "https://i.ytimg.com/vi/.../sddefault.jpg", "width": 640, "height": 480 },
    { "url": "https://i.ytimg.com/vi/.../maxresdefault.jpg", "width": 1280, "height": 720 }
  ],

  "tags": ["Rick Astley", "music video", "80s pop"],
  "category": "Music",
  "categoryId": "10",

  "isLiveVideo": false,
  "isShortsVideo": false,
  "isUpcoming": false,
  "isMonetized": true,
  "isAgeRestricted": false,
  "allowComments": true,
  "hasSubtitles": true,
  "subtitlesLanguages": ["en", "es", "fr"],

  "chaptersData": [
    { "title": "Intro", "startTime": 0, "duration": 12 },
    { "title": "Main Performance", "startTime": 12, "duration": 180 }
  ]
}
```

### Normalizer Mapping — YouTube

| Apify Field | → DB Column | Notes |
|---|---|---|
| `id` | `platform_post_id` | |
| `url` | `post_url` | |
| `title` + `\n\n` + `description` | `text_content` | Concat title + description |
| `uploadDate` | `post_timestamp` | ISO 8601 |
| `channel.name` or `channel.handle` | `source_username` | |
| `viewCount` | `engagement.views` | |
| `likeCount` | `engagement.likes` | |
| `commentCount` | `engagement.comments` | |
| `duration`, `durationSeconds`, `category`, `tags`, `isLiveVideo`, `isShortsVideo`, `isMonetized`, `isAgeRestricted`, `chaptersData`, `channel`, `channelSubscribers` | `platform_data` | JSONB |
| Video URL (from `url`) | → `media_files` row | Use yt-dlp to download |
| `thumbnailUrl` | → `media_files` row | type=image (thumbnail) |
| Full response | `raw_metadata` | JSONB |
