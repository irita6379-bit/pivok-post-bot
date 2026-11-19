"""
PÎVOK Post Constructor Bot — Reset-Proof Standalone Cloud & Local App (RU + TR)
- Pre-configured with @pivoklaleli + PÎVOK WhatsApp, Instagram, MY CLARISSA, ONS LINE
- Never resets buttons/text on edits or Render server sleep (auto-syncs live pinned post from @pivoklaleli!)
"""

import asyncio
import collections
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    Update,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.utils.web_app import safe_parse_webapp_init_data
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pivok-post-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SECRET_PASSWORD = os.environ.get("BOT_SECRET_PASSWORD", "pivok").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
SPACE_HOST = os.environ.get("SPACE_HOST", "").strip()
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    or RENDER_EXTERNAL_URL
    or (f"https://{SPACE_HOST}" if SPACE_HOST else "")
)
LOCAL_PORT = int(os.environ.get("PORT", "8085"))
WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN + ":pivok_post_wh").encode()).hexdigest()[:32] if BOT_TOKEN else "pivok"

DEFAULT_CHANNEL = os.environ.get("PIVOK_CHANNEL", "@pivoklaleli").strip()

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "post_bot_state.json")

_TUNNEL_URL: Optional[str] = None
_SEEN_UPDATES = collections.deque(maxlen=300)

# ==========================================
# OFFICIAL PÎVOK DEFAULT POST & LINKS
# ==========================================

DEFAULT_PIVOK_TEMPLATE_TEXT = (
    "🛍 Добро пожаловать в PÎVOK!\n\n"
    "Оптовая женская одежда из Лалели, Стамбул.\n"
    "✨ Эксклюзивные коллекции • 🆕 Новинки каждый день\n\n"
    "Выбирайте коллекции, уточняйте наличие и оформляйте заказ через WhatsApp или Telegram. 🚀\n\n"
    "🛍 Welcome to PÎVOK!\n\n"
    "Wholesale women's clothing from Laleli, Istanbul.\n"
    "✨ Exclusive collections • 🆕 New arrivals every day\n\n"
    "Browse our collections, check availability and place your order via WhatsApp or Telegram. 🚀\n\n"
    "🛍 مرحباً بكم في PÎVOK!\n\n"
    "ملابس نسائية بالجملة من لالالي، إسطنبول.\n"
    "✨ مجموعات حصرية • 🆕 موديلات جديدة يومياً\n\n"
    "تصفحوا مجموعاتنا، تحققوا من التوفر واطلبوا عبر واتساب أو تيليجرام. 🚀"
)

DEFAULT_PIVOK_TEMPLATE_ROWS = [
    [
        {
            "text": "WHATSAPP",
            "url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
            "style": "success",
        },
        {
            "text": "TELEGRAM",
            "url": "https://t.me/pivoklaleli",
            "style": "primary",
        },
    ],
    [
        {
            "text": "📱 INSTAGRAM",
            "url": "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ==",
            "style": None,
        },
    ],
    [
        {
            "text": "MY CLARISSA",
            "url": "https://t.me/MYCLARISSA",
            "style": None,
        },
        {
            "text": "ONS LINE",
            "url": "https://t.me/onslinestore",
            "style": None,
        },
    ],
]


def get_active_webapp_base_url() -> str:
    if PUBLIC_BASE_URL.startswith("https://"):
        return PUBLIC_BASE_URL
    if _TUNNEL_URL and _TUNNEL_URL.startswith("https://"):
        return _TUNNEL_URL
    return f"http://localhost:{LOCAL_PORT}"


def _load_store() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_store(store: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


STORE = _load_store()
_env_ids = [
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "7042397101").split(",")
    if x.strip().isdigit()
]
ALLOWED_USERS: list[int] = list(set(STORE.get("allowed_users", []) + _env_ids))
DRAFTS: Dict[str, dict] = STORE.setdefault("drafts", {})
USER_LANGS: Dict[str, str] = STORE.setdefault("user_langs", {})


def persist_store():
    STORE["allowed_users"] = ALLOWED_USERS
    STORE["drafts"] = DRAFTS
    STORE["user_langs"] = USER_LANGS
    _save_store(STORE)


# ==========================================
# RU + TR LOCALIZATION
# ==========================================

STRINGS = {
    "ru": {
        "btn_current": "📌 Открыть текущий пост PÎVOK",
        "btn_reset_tpl": "🔄 Сбросить к базовому шаблону",
        "btn_lang": "🇹🇷 Türkçe / Язык",
        "btn_cancel": "❌ Отмена",
        "btn_open_editor": "✏️ Редактировать пост и кнопки",
        "btn_edit_text": "📝 Изменить текст в чате",
        "btn_update_existing": "🔄 Обновить закреп в {ch}",
        "btn_send_pin": "📌 Опубликовать заново и закрепить",
        "btn_send_channel": "📨 Просто отправить в {ch}",
        "btn_set_channel": "📡 Канал: {ch} (сменить)",
        "welcome": (
            "🌸 **Конструктор закрепки PÎVOK (`@pivoklaleli`)**\n\n"
            "Все ваши ссылки (WhatsApp, Telegram, Instagram, MY CLARISSA, ONS LINE) уже привязаны и **не сбрасываются**!\n\n"
            "• Нажмите **«📌 Открыть текущий пост PÎVOK»**, чтобы отредактировать текущую закрепку канала.\n"
            "• Любые изменения сохраняются поверх текущего поста — ничего настраивать заново не нужно."
        ),
        "lang_prompt": "🌐 **Выберите язык / Lütfen dil seçin:**",
        "lang_switched": "🇷🇺 Язык переключён на **Русский**!",
        "preview_hint": (
            "👆 **Текущий пост PÎVOK.**\n"
            "Нажмите **«✏️ Редактировать пост и кнопки»**, внесите изменения (добавьте фирму, включите значок 🆕 или поправьте текст) и нажмите **«🔄 Обновить закреп»**!"
        ),
        "ask_new_text": "📝 Отправьте новый текст поста — **все ваши кнопки и ссылки сохранятся**!",
        "ask_channel": (
            "📡 **Укажите канал для публикации:**\n"
            "Отправьте `@username` канала (по умолчанию `@pivoklaleli`) или перешлите сообщение из него."
        ),
        "channel_saved": "✅ Канал сохранён: **{ch}**!",
        "draft_saved_chat": "✅ Изменения сохранены! Проверьте предпросмотр выше и нажмите **«🔄 Обновить закреп»** (или опубликуйте заново).",
        "sent_ok": "✅ Пост отправлен в канал **{ch}**!",
        "sent_pinned_ok": "📌 Пост опубликован и **закреплён** в канале **{ch}**!",
        "updated_ok": "✅ Закреплённый пост в канале **{ch}** успешно обновлён (без переотправки и без сброса)!",
        "confirm_reset": "⚠️ Точно вернуть базовый стартовый шаблон PÎVOK?",
        "yes_reset": "✅ Да, вернуть базовый шаблон",
        "cancelled": "👌 Отменено.",
        "send_err": (
            "❌ Ошибка работы с каналом **{ch}**.\n"
            "Проверьте, что этот бот добавлен в **Администраторы** канала `@pivoklaleli` (с правами на публикацию, редактирование и закрепление сообщений).\n\n"
            "Детали: `{err}`"
        ),
    },
    "tr": {
        "btn_current": "📌 Mevcut PÎVOK Postunu Aç",
        "btn_reset_tpl": "🔄 Varsayılan Şablona Dön",
        "btn_lang": "🇷🇺 Русский / Dil",
        "btn_cancel": "❌ İptal",
        "btn_open_editor": "✏️ Postu ve Butonları Düzenle",
        "btn_edit_text": "📝 Metni Sohbette Değiştir",
        "btn_update_existing": "🔄 {ch} Sabit Postu Güncelle",
        "btn_send_pin": "📌 Yeni Yayınla ve Sabitle",
        "btn_send_channel": "📨 {ch} Kanalına Gönder",
        "btn_set_channel": "📡 Kanal: {ch} (değiştir)",
        "welcome": (
            "🌸 **PÎVOK Sabit Post Tasarımcısı (`@pivoklaleli`)**\n\n"
            "Tüm linkleriniz (WhatsApp, Telegram, Instagram, MY CLARISSA, ONS LINE) tanımlandı ve **asla sıfırlanmaz**!\n\n"
            "• Mevcut sabit postu düzenlemek için **«📌 Mevcut PÎVOK Postunu Aç»** butonuna basın."
        ),
        "lang_prompt": "🌐 **Lütfen dil seçin / Выберите язык:**",
        "lang_switched": "🇹🇷 Dil **Türkçe** olarak değiştirildi!",
        "preview_hint": (
            "👆 **Mevcut PÎVOK postu.**\n"
            "**«✏️ Postu ve Butonları Düzenle»** butonuna basarak değişikliklerinizi yapın (firma ekleyin, 🆕 açın veya metni düzenleyin), ardından **«🔄 Sabit Postu Güncelle»** tuşuna basın!"
        ),
        "ask_new_text": "📝 Yeni post metnini gönderin — **tüm butonlarınız ve linkleriniz korunacaktır**!",
        "ask_channel": "📡 **Kanal belirtin:** `@kullaniciadi` yazın (varsayılan `@pivoklaleli`) veya kanaldan mesaj iletin.",
        "channel_saved": "✅ Kanal kaydedildi: **{ch}**!",
        "draft_saved_chat": "✅ Değişiklikler kaydedildi! Yukarıdaki önizlemeyi kontrol edip **«🔄 Sabit Postu Güncelle»** butonuna basın.",
        "sent_ok": "✅ Post **{ch}** kanalına gönderildi!",
        "sent_pinned_ok": "📌 Post **{ch}** kanalında yayınlandı ve **sabitlendi**!",
        "updated_ok": "✅ **{ch}** kanalındaki sabit post sıfırlanmadan başarıyla güncellendi!",
        "confirm_reset": "⚠️ Varsayılan PÎVOK başlangıç şablonuna dönülsün mü?",
        "yes_reset": "✅ Evet, şablona dön",
        "cancelled": "👌 İptal edildi.",
        "send_err": (
            "❌ **{ch}** kanal hatası.\n"
            "Botun `@pivoklaleli` kanalında **Yönetici** olduğundan (mesaj gönderme, düzenleme ve sabitleme yetkisiyle) emin olun.\n\n"
            "Detay: `{err}`"
        ),
    },
}

CURRENT_POST_TEXTS = {STRINGS["ru"]["btn_current"], STRINGS["tr"]["btn_current"]}
RESET_TPL_TEXTS = {STRINGS["ru"]["btn_reset_tpl"], STRINGS["tr"]["btn_reset_tpl"]}
LANG_TEXTS = {STRINGS["ru"]["btn_lang"], STRINGS["tr"]["btn_lang"]}
CANCEL_TEXTS = {"❌ Отмена", "Отмена", "❌ İptal", "İptal", "iptal", "/cancel", "/stop"}


def get_lang(uid: Optional[int], tg_code: Optional[str] = None) -> str:
    if uid is not None:
        saved = USER_LANGS.get(str(uid))
        if saved in ("ru", "tr"):
            return saved
    if tg_code and tg_code.lower().startswith("tr"):
        return "tr"
    return "ru"


def set_lang(uid: int, lang: str):
    if lang in ("ru", "tr"):
        USER_LANGS[str(uid)] = lang
        persist_store()


def _t(lang: str, key: str, **kwargs) -> str:
    s = STRINGS.get(lang, STRINGS["ru"]).get(key, STRINGS["ru"][key])
    return s.format(**kwargs) if kwargs else s


def reply_kb(items: list[str], cols: int = 2):
    b = ReplyKeyboardBuilder()
    for it in items:
        b.add(KeyboardButton(text=it))
    b.adjust(cols)
    return b.as_markup(resize_keyboard=True)


def main_menu_kb(lang: str):
    return reply_kb([_t(lang, "btn_current"), _t(lang, "btn_lang"), _t(lang, "btn_reset_tpl")], 2)


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru"),
                InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="setlang_tr"),
            ]
        ]
    )


class EditorStates(StatesGroup):
    editing_post = State()
    editing_text = State()
    waiting_for_channel = State()


class SecurityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        if uid and uid not in ALLOWED_USERS:
            if hasattr(event, "text") and event.text:
                allowed_pw = {SECRET_PASSWORD.lower()}
                if SECRET_PASSWORD.lower() == "pivok":
                    allowed_pw.add("пивок")
                if event.text.strip().lower() in allowed_pw:
                    ALLOWED_USERS.append(uid)
                    lang = get_lang(uid, getattr(user, "language_code", None))
                    set_lang(uid, lang)
                    await event.answer(
                        "✅ **Доступ открыт! / Erişim açıldı!**",
                        reply_markup=lang_inline_kb(),
                        parse_mode="Markdown",
                    )
                    await event.answer(
                        _t(lang, "welcome"),
                        reply_markup=main_menu_kb(lang),
                        parse_mode="Markdown",
                    )
                    return
                await event.answer(
                    "🔒 Бот защищён. Введите пароль:\n"
                    "🔒 Bot korumalıdır. Lütfen şifreyi girin:"
                )
                return
            if hasattr(event, "data") and event.data:
                await event.answer("🔒 Введите пароль / Şifreyi girin", show_alert=True)
                return
            return
        return await handler(event, data)


bot = Bot(token=BOT_TOKEN) if (BOT_TOKEN and "PASTE_YOUR" not in BOT_TOKEN) else None
dp = Dispatcher()
dp.message.outer_middleware(SecurityMiddleware())
dp.callback_query.outer_middleware(SecurityMiddleware())


def get_saved_channel(uid: int) -> tuple[Any, str]:
    d = DRAFTS.get(str(uid), {})
    ch_id = d.get("target_channel") or STORE.get("default_channel") or DEFAULT_CHANNEL
    ch_lbl = d.get("target_channel_label") or STORE.get("default_channel_label") or str(ch_id)
    return ch_id, ch_lbl


def set_saved_channel(uid: int, ch_id: Any, ch_lbl: str):
    STORE["default_channel"] = ch_id
    STORE["default_channel_label"] = ch_lbl
    if str(uid) in DRAFTS:
        DRAFTS[str(uid)]["target_channel"] = ch_id
        DRAFTS[str(uid)]["target_channel_label"] = ch_lbl
    persist_store()


def extract_rows_from_markup(markup: Optional[InlineKeyboardMarkup]) -> list[list[dict]]:
    extracted = []
    if markup and markup.inline_keyboard:
        for row in markup.inline_keyboard:
            row_btns = []
            for btn in row:
                if btn.url:
                    style = getattr(btn, "style", None)
                    if not style and getattr(btn, "model_extra", None):
                        style = btn.model_extra.get("style")
                    row_btns.append({"text": btn.text, "url": btn.url, "style": style or None})
            if row_btns:
                extracted.append(row_btns)
    return extracted


def extract_forward_channel_info(message: Message) -> tuple[Optional[Any], Optional[str], Optional[int]]:
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "type", None) == "channel" and getattr(origin, "chat", None):
        ch = origin.chat
        ch_ref = f"@{ch.username}" if getattr(ch, "username", None) else ch.id
        ch_title = f"@{ch.username}" if getattr(ch, "username", None) else (ch.title or str(ch.id))
        return ch_ref, ch_title, getattr(origin, "message_id", None)

    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat and getattr(fwd_chat, "type", None) == "channel":
        ch_ref = f"@{fwd_chat.username}" if getattr(fwd_chat, "username", None) else fwd_chat.id
        ch_title = f"@{fwd_chat.username}" if getattr(fwd_chat, "username", None) else (fwd_chat.title or str(fwd_chat.id))
        return ch_ref, ch_title, getattr(message, "forward_from_message_id", None)

    return None, None, None


async def fetch_live_pinned_from_channel(ch_id: Any) -> Optional[dict]:
    """
    Reads the live pinned post directly from @pivoklaleli via Telegram API so that even if
    the cloud server restarted after sleep, the current text, buttons, and colors NEVER reset!
    """
    if not bot or not ch_id:
        return None
    try:
        chat_obj = await bot.get_chat(ch_id)
        pinned = getattr(chat_obj, "pinned_message", None)
        if not pinned:
            return None
        rows = extract_rows_from_markup(pinned.reply_markup)
        txt = pinned.text or pinned.caption
        if not rows and not txt:
            return None
        return {
            "text": txt or DEFAULT_PIVOK_TEMPLATE_TEXT,
            "rows": rows if rows else copy.deepcopy(DEFAULT_PIVOK_TEMPLATE_ROWS),
            "pinned_message_id": pinned.message_id,
        }
    except Exception as e:
        logger.info(f"Could not read pinned_message from {ch_id}: {e}")
        return None


async def ensure_user_draft(
    uid: int,
    chat_id: Optional[int] = None,
    preview_msg_id: Optional[int] = None,
    force_sync_channel: bool = False,
) -> dict:
    """
    Guarantees the user ALWAYS has an active, populated PÎVOK draft (never blank/reset!).
    Priority:
      1. Existing non-empty draft in memory/disk (unless force_sync_channel=True)
      2. Live pinned message fetched straight from @pivoklaleli
      3. Built-in PÎVOK template with @pivoklaleli, WhatsApp, Instagram, MY CLARISSA, ONS LINE
    """
    existing = DRAFTS.get(str(uid))
    ch_id, ch_lbl = get_saved_channel(uid)

    if existing and existing.get("rows") and not force_sync_channel:
        if chat_id:
            existing["chat_id"] = chat_id
        if preview_msg_id:
            existing["preview_message_id"] = preview_msg_id
        return existing

    live_pinned = await fetch_live_pinned_from_channel(ch_id)
    if live_pinned:
        draft = {
            "chat_id": chat_id or (existing.get("chat_id") if existing else uid),
            "template_text": live_pinned["text"],
            "original_message_id": None,
            "rows": live_pinned["rows"],
            "auto_sort": existing.get("auto_sort", True) if existing else True,
            "origin_channel_id": ch_id,
            "origin_message_id": live_pinned["pinned_message_id"],
            "target_channel": ch_id,
            "target_channel_label": ch_lbl,
            "preview_message_id": preview_msg_id or (existing.get("preview_message_id") if existing else None),
        }
    elif existing and existing.get("rows"):
        if chat_id:
            existing["chat_id"] = chat_id
        if preview_msg_id:
            existing["preview_message_id"] = preview_msg_id
        return existing
    else:
        draft = {
            "chat_id": chat_id or (existing.get("chat_id") if existing else uid),
            "template_text": (existing.get("template_text") if existing and existing.get("template_text") else DEFAULT_PIVOK_TEMPLATE_TEXT),
            "original_message_id": existing.get("original_message_id") if existing else None,
            "rows": copy.deepcopy(DEFAULT_PIVOK_TEMPLATE_ROWS),
            "auto_sort": True,
            "origin_channel_id": existing.get("origin_channel_id") if existing else None,
            "origin_message_id": existing.get("origin_message_id") if existing else None,
            "target_channel": ch_id,
            "target_channel_label": ch_lbl,
            "preview_message_id": preview_msg_id or (existing.get("preview_message_id") if existing else None),
        }

    DRAFTS[str(uid)] = draft
    persist_store()
    return draft


def build_post_keyboard(uid: int, lang: str, include_controls: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    d = DRAFTS.get(str(uid), {})
    for row_data in d.get("rows", []):
        row_btns = []
        for btn in row_data:
            txt = (btn.get("text") or "").strip()
            url = (btn.get("url") or "").strip()
            if not txt or not url:
                continue
            kwargs: Dict[str, Any] = {"text": txt, "url": url}
            if btn.get("style"):
                kwargs["style"] = btn["style"]
            row_btns.append(InlineKeyboardButton(**kwargs))
        if row_btns:
            builder.row(*row_btns)

    if include_controls:
        _, ch_lbl = get_saved_channel(uid)
        base_url = get_active_webapp_base_url()
        if base_url.startswith("https://"):
            cid = d.get("chat_id") or uid
            pmid = d.get("preview_message_id") or ""
            ed_url = f"{base_url}/?lang={lang}&uid={uid}&cid={cid}&pmid={pmid}&v={int(time.time())}"
            builder.row(
                InlineKeyboardButton(text=_t(lang, "btn_open_editor"), web_app=WebAppInfo(url=ed_url))
            )

        if d.get("origin_message_id"):
            builder.row(
                InlineKeyboardButton(
                    text=_t(lang, "btn_update_existing", ch=ch_lbl),
                    callback_data="pc_update_inplace",
                )
            )

        builder.row(InlineKeyboardButton(text=_t(lang, "btn_send_pin"), callback_data="pc_send_pin"))
        builder.row(
            InlineKeyboardButton(text=_t(lang, "btn_edit_text"), callback_data="pc_edit_text"),
            InlineKeyboardButton(text=_t(lang, "btn_send_channel", ch=ch_lbl), callback_data="pc_send_new"),
        )
        builder.row(
            InlineKeyboardButton(
                text=_t(lang, "btn_set_channel", ch=ch_lbl), callback_data="pc_change_channel"
            )
        )

    return builder.as_markup()


async def render_draft_preview(chat_id: int, uid: int, lang: str):
    if not bot:
        return
    d = await ensure_user_draft(uid, chat_id=chat_id)

    old_prev = d.get("preview_message_id")
    if old_prev:
        try:
            await bot.delete_message(chat_id, old_prev)
        except Exception:
            pass

    # Send first so we know preview_message_id and embed it in the WebApp URL
    kb_initial = build_post_keyboard(uid, lang, include_controls=True)
    if d.get("template_text") is not None or not d.get("original_message_id"):
        text_body = d.get("template_text") or DEFAULT_PIVOK_TEMPLATE_TEXT
        prev_msg = await bot.send_message(
            chat_id, text_body, reply_markup=kb_initial, disable_web_page_preview=True
        )
    else:
        prev_msg = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=d["chat_id"],
            message_id=d["original_message_id"],
            reply_markup=kb_initial,
        )

    d["preview_message_id"] = prev_msg.message_id
    d["chat_id"] = chat_id
    persist_store()

    # Refresh control URL with exact pmid so WebApp can edit this preview message directly
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=prev_msg.message_id,
            reply_markup=build_post_keyboard(uid, lang, include_controls=True),
        )
    except Exception:
        pass


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(EditorStates.editing_post)
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)
    await ensure_user_draft(uid, chat_id=message.chat.id, force_sync_channel=True)
    await message.answer(_t(lang, "welcome"), reply_markup=main_menu_kb(lang), parse_mode="Markdown")
    await render_draft_preview(message.chat.id, uid, lang)
    await message.answer(_t(lang, "preview_hint"), parse_mode="Markdown")


@dp.message(StateFilter("*"), F.text.in_(CURRENT_POST_TEXTS))
async def cmd_open_current_post(message: Message, state: FSMContext):
    await state.set_state(EditorStates.editing_post)
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)
    # Sync live pinned message from @pivoklaleli if pinned on channel; otherwise preserve draft
    await ensure_user_draft(uid, chat_id=message.chat.id, force_sync_channel=True)
    await render_draft_preview(message.chat.id, uid, lang)
    await message.answer(
        _t(lang, "preview_hint"), reply_markup=main_menu_kb(lang), parse_mode="Markdown"
    )


@dp.message(StateFilter("*"), F.text.in_(RESET_TPL_TEXTS))
async def cmd_ask_reset_template(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(lang, "yes_reset"), callback_data="pc_confirm_reset")],
            [InlineKeyboardButton(text=_t(lang, "btn_cancel"), callback_data="pc_cancel_reset")],
        ]
    )
    await message.answer(_t(lang, "confirm_reset"), reply_markup=kb)


@dp.callback_query(F.data.in_({"pc_confirm_reset", "pc_cancel_reset"}))
async def cb_handle_reset(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    lang = get_lang(uid, callback.from_user.language_code)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if callback.data == "pc_cancel_reset":
        await callback.message.answer(_t(lang, "cancelled"), reply_markup=main_menu_kb(lang))
        return

    ch_id, ch_lbl = get_saved_channel(uid)
    existing_origin_msg = DRAFTS.get(str(uid), {}).get("origin_message_id")
    DRAFTS[str(uid)] = {
        "chat_id": callback.message.chat.id,
        "template_text": DEFAULT_PIVOK_TEMPLATE_TEXT,
        "original_message_id": None,
        "rows": copy.deepcopy(DEFAULT_PIVOK_TEMPLATE_ROWS),
        "auto_sort": True,
        "origin_channel_id": ch_id if existing_origin_msg else None,
        "origin_message_id": existing_origin_msg,
        "target_channel": ch_id,
        "target_channel_label": ch_lbl,
    }
    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(callback.message.chat.id, uid, lang)


@dp.message(Command("lang", "language", "dil"))
@dp.message(StateFilter("*"), F.text.in_(LANG_TEXTS))
async def cmd_language(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(_t(lang, "lang_prompt"), reply_markup=lang_inline_kb(), parse_mode="Markdown")


@dp.callback_query(F.data.in_({"setlang_ru", "setlang_tr"}))
async def cb_set_language(callback: CallbackQuery):
    new_lang = "tr" if callback.data.endswith("_tr") else "ru"
    uid = callback.from_user.id
    set_lang(uid, new_lang)
    await callback.answer("🇹🇷 Türkçe" if new_lang == "tr" else "🇷🇺 Русский")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(
        callback.message.chat.id,
        _t(new_lang, "lang_switched"),
        reply_markup=main_menu_kb(new_lang),
        parse_mode="Markdown",
    )
    await render_draft_preview(callback.message.chat.id, uid, new_lang)


@dp.message(Command("cancel", "stop"))
@dp.message(StateFilter("*"), F.text.in_(CANCEL_TEXTS))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.set_state(EditorStates.editing_post)
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(_t(lang, "cancelled"), reply_markup=main_menu_kb(lang))


@dp.message(EditorStates.waiting_for_channel)
async def handle_channel_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)

    fwd_id, fwd_title, _ = extract_forward_channel_info(message)
    if fwd_id is not None:
        ch_id, ch_label = fwd_id, fwd_title or str(fwd_id)
    else:
        raw = (message.text or "").strip()
        if not raw:
            await message.answer(_t(lang, "ask_channel"), parse_mode="Markdown")
            return
        if "t.me/" in raw:
            slug = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0].strip()
            raw = f"@{slug}" if slug and not slug.startswith("+") else raw
        if not raw.startswith("@") and not raw.lstrip("-").isdigit():
            raw = f"@{raw}"
        ch_id = int(raw) if raw.lstrip("-").isdigit() else raw
        ch_label = str(ch_id)

    set_saved_channel(uid, ch_id, ch_label)
    await state.set_state(EditorStates.editing_post)
    await message.answer(
        _t(lang, "channel_saved", ch=ch_label),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )
    await render_draft_preview(message.chat.id, uid, lang)


@dp.message(StateFilter(EditorStates.editing_text, EditorStates.editing_post, None))
async def handle_post_or_text_update(message: Message, state: FSMContext):
    """
    NEVER wipes existing buttons!
    - If user forwards a post that HAS buttons -> imports its text, buttons, and channel origin.
    - If user sends plain text or photo WITHOUT buttons -> updates ONLY the text/media while KEEPING all configured buttons!
    """
    if message.text and message.text.startswith("/"):
        return
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)

    d = await ensure_user_draft(uid, chat_id=message.chat.id)
    incoming_rows = extract_rows_from_markup(message.reply_markup)
    fwd_ch_id, fwd_ch_title, fwd_msg_id = extract_forward_channel_info(message)

    # Preserve existing rows unless the forwarded post brings its own buttons!
    if incoming_rows:
        d["rows"] = incoming_rows

    if fwd_ch_id is not None and fwd_msg_id is not None:
        d["origin_channel_id"] = fwd_ch_id
        d["origin_message_id"] = fwd_msg_id
        set_saved_channel(uid, fwd_ch_id, fwd_ch_title or str(fwd_ch_id))

    if message.text and not message.photo and not message.video and not message.document:
        d["template_text"] = message.text
        d["original_message_id"] = None
    else:
        d["chat_id"] = message.chat.id
        d["original_message_id"] = message.message_id
        d["template_text"] = None

    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid, lang)
    await message.answer(
        _t(lang, "preview_hint"),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "pc_edit_text")
async def cb_edit_text(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    await state.set_state(EditorStates.editing_text)
    await callback.answer()
    await callback.message.answer(
        _t(lang, "ask_new_text"),
        reply_markup=reply_kb([_t(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "pc_change_channel")
async def cb_change_channel(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    await state.set_state(EditorStates.waiting_for_channel)
    await callback.answer()
    await callback.message.answer(
        _t(lang, "ask_channel"),
        reply_markup=reply_kb([_t(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


async def _publish_to_channel(chat_id: int, uid: int, lang: str, pin: bool = False):
    if not bot:
        return
    d = await ensure_user_draft(uid, chat_id=chat_id)
    ch_id, ch_lbl = get_saved_channel(uid)
    clean_kb = build_post_keyboard(uid, lang, include_controls=False)

    try:
        if d.get("template_text") is not None or not d.get("original_message_id"):
            sent = await bot.send_message(
                chat_id=ch_id,
                text=d.get("template_text") or DEFAULT_PIVOK_TEMPLATE_TEXT,
                reply_markup=clean_kb,
                disable_web_page_preview=True,
            )
            sent_msg_id = sent.message_id
        else:
            copied = await bot.copy_message(
                chat_id=ch_id,
                from_chat_id=d["chat_id"],
                message_id=d["original_message_id"],
                reply_markup=clean_kb,
            )
            sent_msg_id = copied.message_id

        # Remember this channel message ID so future edits can update it in-place without re-sending!
        d["origin_channel_id"] = ch_id
        d["origin_message_id"] = sent_msg_id
        persist_store()

        if pin and sent_msg_id:
            try:
                await bot.pin_chat_message(
                    chat_id=ch_id, message_id=sent_msg_id, disable_notification=True
                )
            except Exception:
                pass
            await bot.send_message(chat_id, _t(lang, "sent_pinned_ok", ch=ch_lbl), parse_mode="Markdown")
        else:
            await bot.send_message(chat_id, _t(lang, "sent_ok", ch=ch_lbl), parse_mode="Markdown")

        # Refresh preview controls so "🔄 Обновить закреп" button is shown immediately
        if d.get("preview_message_id"):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=d["preview_message_id"],
                    reply_markup=build_post_keyboard(uid, lang, include_controls=True),
                )
            except Exception:
                pass
    except TelegramAPIError as e:
        await bot.send_message(chat_id, _t(lang, "send_err", ch=ch_lbl, err=str(e)), parse_mode="Markdown")


@dp.callback_query(F.data.in_({"pc_send_new", "pc_send_pin"}))
async def cb_send_to_channel(callback: CallbackQuery):
    uid = callback.from_user.id
    lang = get_lang(uid, callback.from_user.language_code)
    await callback.answer()
    await _publish_to_channel(
        callback.message.chat.id, uid, lang, pin=(callback.data == "pc_send_pin")
    )


@dp.callback_query(F.data == "pc_update_inplace")
async def cb_update_inplace(callback: CallbackQuery):
    if not bot:
        return
    uid = callback.from_user.id
    lang = get_lang(uid, callback.from_user.language_code)
    d = await ensure_user_draft(uid, chat_id=callback.message.chat.id)

    ch_id = d.get("origin_channel_id") or get_saved_channel(uid)[0]
    msg_id = d.get("origin_message_id")

    if not msg_id:
        live = await fetch_live_pinned_from_channel(ch_id)
        if live and live.get("pinned_message_id"):
            msg_id = live["pinned_message_id"]
            d["origin_channel_id"] = ch_id
            d["origin_message_id"] = msg_id
            persist_store()

    await callback.answer()
    if not msg_id:
        # No pinned post yet — publish & pin for the first time
        await _publish_to_channel(callback.message.chat.id, uid, lang, pin=True)
        return

    clean_kb = build_post_keyboard(uid, lang, include_controls=False)
    try:
        if d.get("template_text") is not None and not d.get("original_message_id"):
            try:
                await bot.edit_message_text(
                    chat_id=ch_id,
                    message_id=msg_id,
                    text=d["template_text"],
                    reply_markup=clean_kb,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as te:
                if "message is not modified" in str(te).lower():
                    pass
                else:
                    await bot.edit_message_reply_markup(
                        chat_id=ch_id, message_id=msg_id, reply_markup=clean_kb
                    )
        else:
            await bot.edit_message_reply_markup(
                chat_id=ch_id, message_id=msg_id, reply_markup=clean_kb
            )
        await callback.message.answer(_t(lang, "updated_ok", ch=str(ch_id)), parse_mode="Markdown")
    except TelegramAPIError as e:
        await callback.message.answer(
            _t(lang, "send_err", ch=str(ch_id), err=str(e)), parse_mode="Markdown"
        )


# ==========================================
# FASTAPI + WEBAPP + CLOUD/LOCAL LIFECYCLE
# ==========================================

def _start_local_tunnel_thread():
    global _TUNNEL_URL
    candidates = [
        os.path.join(BASE_DIR, "cloudflared.exe"),
        r"C:\Users\allca\Desktop\08.10.2026\Telegram Channels\post_constructor_bot\cloudflared.exe",
        r"C:\Users\allca\Desktop\08.10.2026\New Sola\Mix\cloudflared.exe",
        shutil.which("cloudflared") or "",
    ]
    cf_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not cf_path:
        return

    def _runner():
        global _TUNNEL_URL
        while True:
            proc = subprocess.Popen(
                [cf_path, "tunnel", "--url", f"http://127.0.0.1:{LOCAL_PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in iter(proc.stdout.readline, ""):
                if "trycloudflare.com" in line:
                    m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                    if m and _TUNNEL_URL != m.group(0):
                        _TUNNEL_URL = m.group(0)
                        logger.info(f"🌐 Cloudflare Tunnel active: {_TUNNEL_URL}")
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            time.sleep(2)

    threading.Thread(target=_runner, daemon=True).start()


_polling_task = None


@asynccontextmanager
async def lifespan(app_inst: FastAPI):
    global _polling_task
    if bot:
        if PUBLIC_BASE_URL.startswith("https://"):
            wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
            await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
            logger.info(f"✅ Cloud Webhook registered at {wh_url}")
        else:
            _start_local_tunnel_thread()
            await bot.delete_webhook(drop_pending_updates=False)
            _polling_task = asyncio.create_task(dp.start_polling(bot))
            logger.info("🌸 Started local polling mode")
    yield
    if _polling_task:
        _polling_task.cancel()
    if bot:
        await bot.session.close()


app = FastAPI(title="PIVOK Post Constructor Bot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/webapp", StaticFiles(directory=os.path.join(BASE_DIR, "webapp")), name="webapp")


@app.get("/")
async def serve_editor_root():
    return FileResponse(os.path.join(BASE_DIR, "webapp", "index.html"))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "PIVOK Post Constructor Bot",
        "version": "v2-persistent-links",
        "channel": DEFAULT_CHANNEL,
        "languages": ["ru", "tr"],
        "webhook_mode": PUBLIC_BASE_URL.startswith("https://"),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        return {"ok": False}
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    payload = await request.json()
    upd_id = payload.get("update_id")
    if upd_id is not None:
        if upd_id in _SEEN_UPDATES:
            return {"ok": True}
        _SEEN_UPDATES.append(upd_id)
    update = Update.model_validate(payload, context={"bot": bot})
    asyncio.create_task(dp.feed_update(bot, update))
    return {"ok": True}


def _resolve_webapp_uid(payload: dict) -> Optional[int]:
    raw_init = payload.get("initData") or ""
    if raw_init and BOT_TOKEN:
        try:
            parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=raw_init)
            if parsed.user and parsed.user.id:
                return int(parsed.user.id)
        except Exception:
            pass
    uid_fb = str(payload.get("uid") or "").strip()
    if uid_fb.isdigit() and int(uid_fb) in ALLOWED_USERS:
        return int(uid_fb)
    return None


@app.post("/api/get_draft")
async def api_get_draft(request: Request):
    data = await request.json()
    uid = _resolve_webapp_uid(data)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    cid = int(data["cid"]) if str(data.get("cid", "")).lstrip("-").isdigit() else uid
    pmid = int(data["pmid"]) if str(data.get("pmid", "")).isdigit() else None

    d = await ensure_user_draft(uid, chat_id=cid, preview_msg_id=pmid)
    is_media = bool(d.get("original_message_id")) and (d.get("template_text") is None)
    return {
        "rows": d.get("rows", []),
        "text": d.get("template_text") or DEFAULT_PIVOK_TEMPLATE_TEXT,
        "is_media": is_media,
        "auto_sort": d.get("auto_sort", True),
    }


@app.post("/api/save_draft")
async def api_save_draft(request: Request):
    data = await request.json()
    uid = _resolve_webapp_uid(data)
    if not uid:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)

    cid = int(data["cid"]) if str(data.get("cid", "")).lstrip("-").isdigit() else uid
    pmid = int(data["pmid"]) if str(data.get("pmid", "")).isdigit() else None

    d = await ensure_user_draft(uid, chat_id=cid, preview_msg_id=pmid)
    new_rows = data.get("rows")
    if isinstance(new_rows, list) and len(new_rows) > 0:
        d["rows"] = new_rows

    new_text = data.get("text")
    if isinstance(new_text, str) and new_text.strip():
        d["template_text"] = new_text.strip()
        d["original_message_id"] = None

    if "auto_sort" in data:
        d["auto_sort"] = bool(data["auto_sort"])

    persist_store()

    lang = get_lang(uid, None)
    target_chat = d.get("chat_id") or cid
    target_pmid = d.get("preview_message_id") or pmid

    if bot and target_chat:
        kb = build_post_keyboard(uid, lang, include_controls=True)
        updated_existing_preview = False
        if target_pmid:
            try:
                if d.get("template_text") is not None and not d.get("original_message_id"):
                    await bot.edit_message_text(
                        chat_id=target_chat,
                        message_id=target_pmid,
                        text=d["template_text"],
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                else:
                    await bot.edit_message_reply_markup(
                        chat_id=target_chat, message_id=target_pmid, reply_markup=kb
                    )
                updated_existing_preview = True
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    updated_existing_preview = True
                else:
                    logger.warning(f"Could not edit existing preview, sending fresh preview: {e}")

        if not updated_existing_preview:
            await render_draft_preview(target_chat, uid, lang)

    return {"success": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LOCAL_PORT)
