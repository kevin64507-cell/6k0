"""QR Code Telegram bot: text, links, Wi-Fi, contacts, scanning, and style settings."""
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from qr_utils import (
    COLORS,
    MAX_TEXT_LENGTH,
    decode_qr,
    make_qr,
    vcard_payload,
    wifi_payload,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("qrbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")

WIFI_SSID, WIFI_PASS, CONTACT_NAME, CONTACT_PHONE, CONTACT_EMAIL = range(5)

SIZES = {"small": 6, "medium": 10, "large": 16}

WELCOME = (
    "👋 *Welcome to QR Code Bot!*\n\n"
    "Generate QR codes instantly from text, links, and messages. "
    "Fast, simple, and easy to use anytime. 📱⚡️\n\n"
    "✍️ *Just send me any text or link* and I'll turn it into a QR code.\n\n"
    "Or pick a feature below:"
)

HELP = (
    "📖 *How to use*\n\n"
    "• Send any text or link → get a QR code\n"
    "• /wifi → QR code to join a Wi-Fi network\n"
    "• /contact → QR code that saves a contact\n"
    "• /scan → send a QR image and I'll read it\n"
    "• /settings → change QR color and size\n"
    "• /cancel → stop the current action"
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📶 Wi-Fi QR", callback_data="menu:wifi"),
                InlineKeyboardButton("👤 Contact QR", callback_data="menu:contact"),
            ],
            [
                InlineKeyboardButton("🔍 Scan QR", callback_data="menu:scan"),
                InlineKeyboardButton("🎨 Settings", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    colors = [
        InlineKeyboardButton(name.title(), callback_data=f"color:{name}")
        for name in COLORS
    ]
    sizes = [
        InlineKeyboardButton(name.title(), callback_data=f"size:{name}")
        for name in SIZES
    ]
    return InlineKeyboardMarkup([colors[:3], colors[3:], sizes])


async def send_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str, caption: str | None = None):
    message = update.effective_message
    if len(data) > MAX_TEXT_LENGTH:
        await message.reply_text(
            f"⚠️ That's too long. Please keep it under {MAX_TEXT_LENGTH} characters."
        )
        return
    color = context.user_data.get("color", "black")
    size = SIZES[context.user_data.get("size", "medium")]
    try:
        image = make_qr(data, color=color, box_size=size)
    except Exception:
        logger.exception("QR generation failed")
        await message.reply_text("❌ Couldn't create that QR code. Try shorter text.")
        return
    await message.reply_photo(
