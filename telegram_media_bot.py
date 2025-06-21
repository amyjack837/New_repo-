import os
import logging
import re
import json
import requests
import yt_dlp
import io
import zipfile
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp.utils

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Set this in your environment variables
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
COOKIES_FILE = "cookies.txt"  # Place your exported YouTube cookies here

# --- Logger ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Storage ---
user_state = {}  # chat_id -> MediaInfo

# --- MediaInfo Model ---
class MediaInfo:
    def __init__(self, platform, title, formats=None, items=None, thumbnail=None, caption=None):
        self.platform = platform
        self.title = title
        self.formats = formats or []       # [(label, url), ...]
        self.items = items or []           # direct media URLs for photos/carousels
        self.thumbnail = thumbnail         # thumbnail URL for video
        self.caption = caption             # text caption

# --- Utils ---
"
"def extract_video_formats(url: str) -> MediaInfo:
"
"    # Use yt-dlp without cookies; login-required videos will error
"
"    opts = {"skip_download": True, "quiet": True}
"
"    try:
"
"        with yt_dlp.YoutubeDL(opts) as ydl:
"
"            info = ydl.extract_info(url, download=False)
"
"    except Exception as e:
"
"        # Could be age-restricted or login-required
"
"        raise ValueError("This video cannot be downloaded without login/cookies.")
"
"    formats = []
"
"    for f in info.get("formats", []):
"
"        if f.get("vcodec") and f.get("url"):
"
"            size = f.get("filesize") or f.get("filesize_approx") or 0
"
"            mb = round(size / (1024*1024), 1) if size else None
"
"            note = f.get("format_note") or f.get("height")
"
"            label = f"{note}p ({mb}MB)" if mb else f"{note}p"
"
"            formats.append((label, f["url"]))
"
"    return MediaInfo(
"
"        platform="video",
"
"        title=info.get("title"),
"
"        formats=formats,
"
"        thumbnail=info.get("thumbnail"),
"
"    )

# --- Downloaders ---
async def youtube_metadata(url: str) -> MediaInfo:
    return extract_video_formats(url)

async def instagram_metadata(url: str) -> MediaInfo:
    resp = requests.get(url, headers=HEADERS)
    m = re.search(r"window\\._sharedData = (.*?);</script>", resp.text)
    data = json.loads(m.group(1))
    media = data["entry_data"]["PostPage"][0]["graphql"]["shortcode_media"]
    title = media.get("accessibility_caption") or "Instagram Media"
    cap_nodes = media.get("edge_media_to_caption", {}).get("edges", [])
    caption = cap_nodes[0]["node"]["text"] if cap_nodes else None

    # Carousel
    if media.get("__typename") == "GraphSidecar":
        items = [edge["node"].get("video_url") or edge["node"].get("display_url")
                 for edge in media["edge_sidecar_to_children"]["edges"]]
        return MediaInfo(platform="instagram", title=title, items=items, caption=caption)
    # Single
    if media.get("is_video"):
        return extract_video_formats(url)
    return MediaInfo(
        platform="instagram",
        title=title,
        items=[media.get("display_url")],
        caption=caption,
    )

async def facebook_metadata(url: str) -> MediaInfo:
    info = extract_video_formats(url)
    if info.formats:
        return info
    mobile = url.replace("www.facebook.com", "mbasic.facebook.com")
    resp = requests.get(mobile, headers=HEADERS)
    urls = re.findall(r'<img src="(https://lookaside\\.fbsbx\\.com/[^"]+)"', resp.text)
    return MediaInfo(platform="facebook", title="Facebook Photos", items=urls)

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a YouTube, Instagram, or Facebook link to download content."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id
    try:
        if 'youtu' in url:
            info = await youtube_metadata(url)
        elif 'instagram.com' in url:
            info = await instagram_metadata(url)
        elif 'facebook.com' in url:
            info = await facebook_metadata(url)
        else:
            return await update.message.reply_text("Invalid link.")
    except ValueError as e:
        return await update.message.reply_text(str(e))
    except Exception as e:
        logger.error("Error fetching media: %s", e)
        return await update.message.reply_text("Failed to fetch media.")

    # Photos or Carousel
    if info.items:
        if len(info.items) > 1:
            media_group = [
                InputMediaVideo(m) if m.endswith('.mp4') else InputMediaPhoto(m)
                for m in info.items
            ]
            await context.bot.send_media_group(chat_id, media_group)
        else:
            m = info.items[0]
            if m.endswith('.mp4'):
                await context.bot.send_video(chat_id, m)
            else:
                await context.bot.send_photo(chat_id, m)
        if info.caption:
            await context.bot.send_message(chat_id, info.caption)
        return

    # Videos: choose format
    user_state[chat_id] = info
    buttons = [InlineKeyboardButton(label, callback_data=label) for label, _ in info.formats]
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"*{info.title}*\nSelect quality:"
    await update.message.reply_markdown(text, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    choice = query.data
    info = user_state.get(chat_id)
    if not info:
        return await query.edit_message_text("Session expired.")
    for label, media_url in info.formats:
        if label == choice:
            await query.edit_message_text(f"Downloading *{choice}*...", parse_mode='Markdown')
            if media_url.endswith('.mp3') or 'audio' in label.lower():
                await context.bot.send_audio(chat_id, media_url)
            else:
                await context.bot.send_video(chat_id, media_url)
            break
    user_state.pop(chat_id, None)

# --- Main ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()

if __name__ == '__main__':
    main()
