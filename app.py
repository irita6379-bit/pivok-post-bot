"""
PIVOK Post Publishing Bot — English UI (Cloud & Local)
- Supports /start post builder workflow (text, photo, photo+caption, video/document, forwarded posts & channel forwards)
- 6 configurable PIVOK post buttons: WhatsApp, Instagram, WhatsApp Channel, Sale Channel, Video Channel, Address
- Preserves existing pinned-post management (/pinned), channel config (/channel), and link management (/links)
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
from typing import Any, Dict, List, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
_USER_PUBLISH_LOCKS: Dict[int, asyncio.Lock] = {}
_MEDIA_GROUP_BUFFER: Dict[str, dict] = {}

# ==========================================
# PIVOK 6 CONFIGURABLE POST BUTTONS
# Links can be updated via:
#   1) Environment variables (LINK_WHATSAPP, LINK_INSTAGRAM, LINK_WHATSAPP_CHANNEL,
#      LINK_SALE_CHANNEL, LINK_VIDEO_CHANNEL, LINK_ADDRESS)
#   2) /links command in Telegram or inside Open Editor (persisted in data/post_bot_state.json)
#   3) Default values below (no bot logic rewrite required)
# ==========================================

PIVOK_BUTTON_PRESETS: List[Dict[str, Any]] = [
    {
        "key": "whatsapp",
        "text": "💬 WhatsApp",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP",
        "style": "success",
    },
    {
        "key": "instagram",
        "text": "📸 Instagram",
        "default_url": "https://www.instagram.com/pivok_fashion?cnvx=MWRzeWJkaGs1MGpsaQ==",
        "env_var": "LINK_INSTAGRAM",
        "style": None,
    },
    {
        "key": "whatsapp_channel",
        "text": "📢 WhatsApp Channel",
        "default_url": "https://chat.whatsapp.com/Ddh7BKb1atCAhRzogy9l9Y",
        "env_var": "LINK_WHATSAPP_CHANNEL",
        "style": "success",
    },
    {
        "key": "sale_channel",
        "text": "🔥 Sale Channel",
        "default_url": "https://t.me/pivoksale",
        "env_var": "LINK_SALE_CHANNEL",
        "style": "danger",
    },
    {
        "key": "video_channel",
        "text": "📹 Video Channel",
        "default_url": "https://t.me/pivokvideo",
        "env_var": "LINK_VIDEO_CHANNEL",
        "style": "primary",
    },
    {
        "key": "address",
        "text": "📍 Address",
        "default_url": "https://www.google.com/maps?q=41.007667,28.955222",
        "env_var": "LINK_ADDRESS",
        "style": None,
    },
]

PRESET_BY_KEY = {p["key"]: p for p in PIVOK_BUTTON_PRESETS}

WELCOME_START_TEXT = (
    "<b>Welcome to PIVOK Post Builder!</b>\n\n"
    "Send me any text, photo, or forward a message from another Telegram channel "
    "to attach buttons to your post."
)

# Preserved template for /pinned catalog post feature
DEFAULT_PINNED_TEMPLATE_TEXT = (
    "🛍 Welcome to PÎVOK!\n\n"
    "Wholesale women's clothing from Laleli, Istanbul.\n"
    "✨ Exclusive collections • 🆕 New arrivals every day\n\n"
    "Browse our collections, check availability and place your order via WhatsApp or Telegram. 🚀"
)

DEFAULT_PINNED_FIRM_ROWS = [
    [
        {"text": "MY CLARISSA", "url": "https://t.me/MYCLARISSA", "style": None},
        {"text": "ONS LINE", "url": "https://t.me/onslinestore", "style": None},
    ]
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
SAVED_BUTTON_LINKS: Dict[str, str] = STORE.setdefault("button_links", {})


def persist_store():
    STORE["allowed_users"] = ALLOWED_USERS
    STORE["drafts"] = DRAFTS
    STORE["button_links"] = SAVED_BUTTON_LINKS
    _save_store(STORE)


def normalize_url(raw_url: str) -> str:
    s = (raw_url or "").strip()
    if not s:
        return ""
    if s.startswith("@"):
        return f"https://t.me/{s[1:]}"
    if not re.match(r"^(https?://|tg://)", s, flags=re.IGNORECASE):
        return f"https://{s}"
    return s


def get_preset_url(key: str) -> str:
    preset = PRESET_BY_KEY.get(key)
    if not preset:
        return ""
    if key in SAVED_BUTTON_LINKS and SAVED_BUTTON_LINKS[key] is not None:
        saved_val = SAVED_BUTTON_LINKS[key].strip()
        if saved_val:
            return normalize_url(saved_val)
    env_val = os.environ.get(preset["env_var"], "").strip()
    if env_val:
        return normalize_url(env_val)
    return normalize_url(preset["default_url"])


def set_preset_url(key: str, url: str):
    if key in PRESET_BY_KEY:
        SAVED_BUTTON_LINKS[key] = normalize_url(url)
        persist_store()


def get_all_presets_state(selected_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    result = []
    selected_set = set(selected_keys) if selected_keys is not None else None
    for p in PIVOK_BUTTON_PRESETS:
        k = p["key"]
        url = get_preset_url(k)
        is_sel = (k in selected_set) if selected_set is not None else bool(url)
        result.append(
            {
                "key": k,
                "text": p["text"],
                "url": url,
                "style": p["style"],
                "selected": is_sel,
            }
        )
    return result


def build_rows_from_selection(
    selected_keys: List[str], custom_rows: Optional[List[List[dict]]] = None
) -> List[List[dict]]:
    selected_set = set(selected_keys)
    active_btns = []
    for p in PIVOK_BUTTON_PRESETS:
        if p["key"] in selected_set:
            url = get_preset_url(p["key"])
            if url:
                active_btns.append(
                    {
                        "text": p["text"],
                        "url": url,
                        "style": p["style"],
                        "preset_key": p["key"],
                    }
                )
    rows: List[List[dict]] = []
    for i in range(0, len(active_btns), 2):
        rows.append(active_btns[i : i + 2])
    if custom_rows:
        for crow in custom_rows:
            valid_c = [
                {
                    "text": (b.get("text") or "").strip(),
                    "url": normalize_url(b.get("url") or ""),
                    "style": b.get("style") or None,
                }
                for b in crow
                if (b.get("text") or "").strip() and (b.get("url") or "").strip()
            ]
            if valid_c:
                rows.append(valid_c)
    return rows


def default_selected_preset_keys() -> List[str]:
    return [p["key"] for p in PIVOK_BUTTON_PRESETS if get_preset_url(p["key"])]


class EditorStates(StatesGroup):
    waiting_for_post = State()
    editing_post = State()
    editing_text = State()
    waiting_for_channel = State()
    waiting_for_link_url = State()


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
                    persist_store()
                    await event.answer(
                        "✅ <b>Access granted!</b>\n\n" + WELCOME_START_TEXT,
                        parse_mode="HTML",
                        reply_markup=ReplyKeyboardRemove(),
                    )
                    return
                await event.answer(
                    "🔒 This bot is password-protected. Please enter the access password:"
                )
                return
            if hasattr(event, "data") and event.data:
                await event.answer("🔒 Please enter the password in chat first.", show_alert=True)
                return
            return
        return await handler(event, data)


bot = Bot(token=BOT_TOKEN) if (BOT_TOKEN and "PASTE" not in BOT_TOKEN) else None
dp = Dispatcher()
dp.message.outer_middleware(SecurityMiddleware())
dp.callback_query.outer_middleware(SecurityMiddleware())


def get_user_lock(uid: int) -> asyncio.Lock:
    if uid not in _USER_PUBLISH_LOCKS:
        _USER_PUBLISH_LOCKS[uid] = asyncio.Lock()
    return _USER_PUBLISH_LOCKS[uid]


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


def compute_draft_signature(d: dict, ch_id: Any) -> str:
    payload = {
        "channel": str(ch_id),
        "media_type": d.get("media_type"),
        "media_file_id": d.get("media_file_id"),
        "text": (d.get("template_text") or "").strip(),
        "rows": [
            [{"text": b.get("text", ""), "url": b.get("url", ""), "style": b.get("style")} for b in r]
            for r in d.get("rows", [])
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def fetch_live_pinned_from_channel(ch_id: Any) -> Optional[dict]:
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
            "text": txt or DEFAULT_PINNED_TEMPLATE_TEXT,
            "rows": rows,
            "pinned_message_id": pinned.message_id,
        }
    except Exception as e:
        logger.info(f"Could not read pinned_message from {ch_id}: {e}")
        return None


def split_rows_into_presets_and_custom(rows: List[List[dict]]) -> tuple[List[str], List[List[dict]]]:
    preset_by_label = {p["text"].lower(): p["key"] for p in PIVOK_BUTTON_PRESETS}
    selected_keys: List[str] = []
    custom_rows: List[List[dict]] = []
    for r in rows:
        c_row = []
        for b in r:
            p_key = b.get("preset_key") or preset_by_label.get((b.get("text") or "").strip().lower())
            if p_key and p_key in PRESET_BY_KEY:
                if p_key not in selected_keys:
                    selected_keys.append(p_key)
                if b.get("url") and not get_preset_url(p_key):
                    set_preset_url(p_key, b["url"])
            else:
                c_row.append(
                    {
                        "text": b.get("text", ""),
                        "url": b.get("url", ""),
                        "style": b.get("style"),
                    }
                )
        if c_row:
            custom_rows.append(c_row)
    return selected_keys, custom_rows


def create_new_post_draft(
    uid: int,
    chat_id: int,
    text: str,
    media_type: Optional[str] = None,
    media_file_id: Optional[str] = None,
) -> dict:
    ch_id, ch_lbl = get_saved_channel(uid)
    old_draft = DRAFTS.get(str(uid), {})
    sel_keys = default_selected_preset_keys()
    rows = build_rows_from_selection(sel_keys, [])

    draft = {
        "mode": "post",
        "has_post": True,
        "chat_id": chat_id,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "template_text": text,
        "selected_keys": sel_keys,
        "custom_rows": [],
        "rows": rows,
        "auto_sort": False,
        "origin_channel_id": None,
        "origin_message_id": None,
        "target_channel": ch_id,
        "target_channel_label": ch_lbl,
        "preview_message_id": old_draft.get("preview_message_id"),
        "last_published_sig": None,
    }
    DRAFTS[str(uid)] = draft
    persist_store()
    return draft


def build_post_keyboard(uid: int, include_controls: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    d = DRAFTS.get(str(uid), {})

    for row_data in d.get("rows", []):
        row_btns = []
        for btn in row_data:
            txt = (btn.get("text") or "").strip()
            url = normalize_url(btn.get("url") or "")
            if not txt or not url:
                continue
            kwargs: Dict[str, Any] = {"text": txt, "url": url}
            if btn.get("style"):
                kwargs["style"] = btn["style"]
            row_btns.append(InlineKeyboardButton(**kwargs))
        if row_btns:
            builder.row(*row_btns)

    if include_controls and d.get("has_post", True):
        base_url = get_active_webapp_base_url()
        cid = d.get("chat_id") or uid
        pmid = d.get("preview_message_id") or ""

        if base_url.startswith("https://"):
            ed_url = f"{base_url}/?uid={uid}&cid={cid}&pmid={pmid}&v={int(time.time())}"
            editor_btn = InlineKeyboardButton(text="Open Editor", web_app=WebAppInfo(url=ed_url))
        else:
            editor_btn = InlineKeyboardButton(text="Open Editor", callback_data="pc_edit_text")

        send_btn = InlineKeyboardButton(text="Send to Channel", callback_data="pc_send_new")
        builder.row(editor_btn, send_btn)

        if d.get("mode") == "pinned" and d.get("origin_message_id"):
            _, ch_lbl = get_saved_channel(uid)
            builder.row(
                InlineKeyboardButton(
                    text=f"Update Pinned Post in {ch_lbl}",
                    callback_data="pc_update_inplace",
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="Publish & Pin New Post",
                    callback_data="pc_send_pin",
                )
            )

    return builder.as_markup()


async def _send_content_message(
    target_chat_id: Any,
    d: dict,
    reply_markup: InlineKeyboardMarkup,
) -> Message:
    media_type = d.get("media_type")
    media_fid = d.get("media_file_id")
    txt = d.get("template_text")
    caption = txt if (txt and txt.strip()) else None

    if media_type == "photo" and media_fid:
        return await bot.send_photo(
            chat_id=target_chat_id,
            photo=media_fid,
            caption=caption,
            reply_markup=reply_markup,
        )
    if media_type == "video" and media_fid:
        return await bot.send_video(
            chat_id=target_chat_id,
            video=media_fid,
            caption=caption,
            reply_markup=reply_markup,
        )
    if media_type == "animation" and media_fid:
        return await bot.send_animation(
            chat_id=target_chat_id,
            animation=media_fid,
            caption=caption,
            reply_markup=reply_markup,
        )
    if media_type == "document" and media_fid:
        return await bot.send_document(
            chat_id=target_chat_id,
            document=media_fid,
            caption=caption,
            reply_markup=reply_markup,
        )

    body = txt if (txt and txt.strip()) else " "
    return await bot.send_message(
        chat_id=target_chat_id,
        text=body,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def render_draft_preview(chat_id: int, uid: int):
    if not bot:
        return
    d = DRAFTS.get(str(uid))
    if not d or not d.get("has_post", True):
        return

    old_prev = d.get("preview_message_id")
    if old_prev:
        try:
            await bot.delete_message(chat_id, old_prev)
        except Exception:
            pass

    d["chat_id"] = chat_id
    d["preview_message_id"] = None
    kb_initial = build_post_keyboard(uid, include_controls=True)
    prev_msg = await _send_content_message(chat_id, d, kb_initial)

    d["preview_message_id"] = prev_msg.message_id
    persist_store()

    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=prev_msg.message_id,
            reply_markup=build_post_keyboard(uid, include_controls=True),
        )
    except Exception:
        pass


# ==========================================
# COMMANDS & WORKFLOW HANDLERS
# ==========================================


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    for k in list(_MEDIA_GROUP_BUFFER.keys()):
        if k.startswith(f"{uid}:"):
            entry = _MEDIA_GROUP_BUFFER.pop(k, None)
            if entry and entry.get("task"):
                entry["task"].cancel()

    await state.set_state(EditorStates.waiting_for_post)
    await message.answer(
        WELCOME_START_TEXT,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("cancel", "stop"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.set_state(EditorStates.waiting_for_post)
    await message.answer(
        "Action cancelled. Send me any text, photo, or forwarded message to build a post.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("channel"))
async def cmd_channel(message: Message, state: FSMContext):
    uid = message.from_user.id
    _, ch_lbl = get_saved_channel(uid)
    await state.set_state(EditorStates.waiting_for_channel)
    await message.answer(
        f"📡 <b>Connected Channel:</b> <code>{ch_lbl}</code>\n\n"
        "Send a new <code>@channel_username</code> to change it, or send /cancel to keep the current channel.",
        parse_mode="HTML",
    )


@dp.message(EditorStates.waiting_for_channel)
async def handle_channel_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    fwd_id, fwd_title, _ = extract_forward_channel_info(message)
    if fwd_id is not None:
        ch_id, ch_label = fwd_id, fwd_title or str(fwd_id)
    else:
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Please send a valid @channel_username or /cancel.")
            return
        if raw.lower() in {"cancel", "/cancel"}:
            await state.set_state(EditorStates.waiting_for_post)
            await message.answer("Channel unchanged.")
            return
        if "t.me/" in raw:
            slug = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0].strip()
            raw = f"@{slug}" if slug and not slug.startswith("+") else raw
        if not raw.startswith("@") and not raw.lstrip("-").isdigit():
            raw = f"@{raw}"
        ch_id = int(raw) if raw.lstrip("-").isdigit() else raw
        ch_label = str(ch_id)

    set_saved_channel(uid, ch_id, ch_label)
    await state.set_state(EditorStates.waiting_for_post)
    await message.answer(
        f"✅ Target channel updated to <b>{ch_label}</b>.",
        parse_mode="HTML",
    )


def _build_links_admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in PIVOK_BUTTON_PRESETS:
        url = get_preset_url(p["key"])
        status = "✅" if url else "⚪"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {p['text']}",
                callback_data=f"setlink_{p['key']}",
            )
        )
    return builder.as_markup()


def _format_links_overview() -> str:
    lines = ["🔗 <b>PIVOK Post Button Links:</b>\n"]
    for idx, p in enumerate(PIVOK_BUTTON_PRESETS, 1):
        url = get_preset_url(p["key"])
        val_str = f"<code>{url}</code>" if url else "<i>Not set yet</i>"
        lines.append(f"{idx}. <b>{p['text']}</b>: {val_str}")
    lines.append("\nTap any button below to set or update its URL:")
    return "\n".join(lines)


@dp.message(Command("links"))
async def cmd_links(message: Message):
    await message.answer(
        _format_links_overview(),
        parse_mode="HTML",
        reply_markup=_build_links_admin_kb(),
    )


@dp.callback_query(F.data.startswith("setlink_"))
async def cb_set_link_prompt(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split("setlink_", 1)[1]
    preset = PRESET_BY_KEY.get(key)
    if not preset:
        await callback.answer("Unknown button", show_alert=True)
        return
    cur_url = get_preset_url(key)
    await state.update_data(editing_link_key=key)
    await state.set_state(EditorStates.waiting_for_link_url)
    await callback.answer()
    await callback.message.answer(
        f"🔗 Send the URL for <b>{preset['text']}</b>\n"
        f"Current: {f'<code>{cur_url}</code>' if cur_url else '<i>Not set</i>'}\n\n"
        "Send <code>clear</code> to empty the link, or /cancel to abort.",
        parse_mode="HTML",
    )


@dp.message(EditorStates.waiting_for_link_url)
async def handle_link_url_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Please send a valid URL or /cancel.")
        return
    if raw.lower() in {"/cancel", "cancel"}:
        await state.set_state(EditorStates.waiting_for_post)
        await message.answer("Link update cancelled.")
        return

    st_data = await state.get_data()
    key = st_data.get("editing_link_key")
    preset = PRESET_BY_KEY.get(key or "")
    if not preset:
        await state.set_state(EditorStates.waiting_for_post)
        return

    if raw.lower() == "clear":
        SAVED_BUTTON_LINKS[key] = ""
        persist_store()
        msg_txt = f"⚪ Link cleared for <b>{preset['text']}</b>."
    else:
        set_preset_url(key, raw)
        msg_txt = f"✅ Link saved for <b>{preset['text']}</b>: <code>{get_preset_url(key)}</code>"

    await state.set_state(EditorStates.waiting_for_post)
    await message.answer(
        msg_txt + "\n\n" + _format_links_overview(),
        parse_mode="HTML",
        reply_markup=_build_links_admin_kb(),
    )


@dp.message(Command("pinned"))
async def cmd_pinned(message: Message, state: FSMContext):
    """Preserves existing pinned catalog post management without affecting /start."""
    uid = message.from_user.id
    ch_id, ch_lbl = get_saved_channel(uid)
    live = await fetch_live_pinned_from_channel(ch_id)
    if live:
        sel_keys, custom_rows = split_rows_into_presets_and_custom(live["rows"])
        rows = live["rows"]
        txt = live["text"]
        origin_mid = live["pinned_message_id"]
    else:
        sel_keys = default_selected_preset_keys()
        custom_rows = copy.deepcopy(DEFAULT_PINNED_FIRM_ROWS)
        rows = build_rows_from_selection(sel_keys, custom_rows)
        txt = DEFAULT_PINNED_TEMPLATE_TEXT
        origin_mid = None

    DRAFTS[str(uid)] = {
        "mode": "pinned",
        "has_post": True,
        "chat_id": message.chat.id,
        "media_type": None,
        "media_file_id": None,
        "template_text": txt,
        "selected_keys": sel_keys,
        "custom_rows": custom_rows,
        "rows": rows,
        "auto_sort": True,
        "origin_channel_id": ch_id if origin_mid else None,
        "origin_message_id": origin_mid,
        "target_channel": ch_id,
        "target_channel_label": ch_lbl,
        "preview_message_id": DRAFTS.get(str(uid), {}).get("preview_message_id"),
        "last_published_sig": None,
    }
    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid)


@dp.callback_query(F.data == "pc_edit_text")
async def cb_edit_text_fallback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditorStates.editing_text)
    await callback.answer()
    await callback.message.answer(
        "📝 Send the updated text/caption for your post (or /cancel to abort):"
    )


@dp.message(EditorStates.editing_text)
async def handle_inline_text_edit(message: Message, state: FSMContext):
    uid = message.from_user.id
    d = DRAFTS.get(str(uid))
    if not d or not d.get("has_post"):
        await state.set_state(EditorStates.waiting_for_post)
        return
    new_txt = (message.text or message.caption or "").strip()
    if d.get("media_type") and len(new_txt) > 1024:
        await message.answer(
            f"⚠️ Caption is too long ({len(new_txt)}/1024 characters). Please send a shorter caption:"
        )
        return
    d["template_text"] = new_txt
    d["last_published_sig"] = None
    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid)


def _extract_message_media_and_text(message: Message) -> tuple[Optional[str], Optional[str], str]:
    if message.photo:
        return "photo", message.photo[-1].file_id, (message.caption or "")
    if message.video:
        return "video", message.video.file_id, (message.caption or "")
    if message.animation:
        return "animation", message.animation.file_id, (message.caption or "")
    if message.document:
        return "document", message.document.file_id, (message.caption or "")
    return None, None, (message.text or "")


async def _finalize_media_group_post(group_key: str, state: FSMContext):
    try:
        await asyncio.sleep(0.65)
    except asyncio.CancelledError:
        return

    entry = _MEDIA_GROUP_BUFFER.pop(group_key, None)
    if not entry or not entry.get("messages"):
        return

    msgs: List[Message] = sorted(entry["messages"], key=lambda m: m.message_id)
    uid = entry["uid"]
    chat_id = entry["chat_id"]

    primary_msg = msgs[0]
    media_type, media_fid, _ = _extract_message_media_and_text(primary_msg)
    combined_caption = ""
    for m in msgs:
        if m.caption and m.caption.strip():
            combined_caption = m.caption
            break

    create_new_post_draft(
        uid=uid,
        chat_id=chat_id,
        text=combined_caption,
        media_type=media_type,
        media_file_id=media_fid,
    )
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(chat_id, uid)


@dp.message(StateFilter(EditorStates.waiting_for_post, EditorStates.editing_post, None))
async def handle_incoming_post(message: Message, state: FSMContext):
    """
    Step 2 & Step 3:
    Accepts plain text, photo, photo with caption, video/document, forwarded messages,
    and forwarded posts from other Telegram channels.
    Creates a draft, attaches PIVOK buttons, and displays the preview with
    'Open Editor' and 'Send to Channel' without auto-publishing or changing target channel.
    """
    if message.text and message.text.startswith("/"):
        return

    uid = message.from_user.id

    # Debounce multi-photo albums so we preserve caption and create a single clean draft
    if message.media_group_id:
        gkey = f"{uid}:{message.media_group_id}"
        if gkey not in _MEDIA_GROUP_BUFFER:
            _MEDIA_GROUP_BUFFER[gkey] = {
                "uid": uid,
                "chat_id": message.chat.id,
                "messages": [],
                "task": None,
            }
        _MEDIA_GROUP_BUFFER[gkey]["messages"].append(message)
        if _MEDIA_GROUP_BUFFER[gkey]["task"]:
            _MEDIA_GROUP_BUFFER[gkey]["task"].cancel()
        _MEDIA_GROUP_BUFFER[gkey]["task"] = asyncio.create_task(
            _finalize_media_group_post(gkey, state)
        )
        return

    media_type, media_fid, text_content = _extract_message_media_and_text(message)
    if not media_type and not text_content.strip():
        await message.answer(
            "Please send text, a photo (with or without caption), or forward a post from another Telegram channel."
        )
        return

    if media_type and len(text_content) > 1024:
        text_content = text_content[:1024]

    d = create_new_post_draft(
        uid=uid,
        chat_id=message.chat.id,
        text=text_content,
        media_type=media_type,
        media_file_id=media_fid,
    )

    # If user forwards a post from our own target channel (@pivoklaleli) that already has buttons,
    # preserve its exact row layout & colors (solastore2026 workflow)
    fwd_ch_id, _, fwd_msg_id = extract_forward_channel_info(message)
    saved_ch_id, _ = get_saved_channel(uid)
    incoming_rows = extract_rows_from_markup(message.reply_markup)
    if (
        incoming_rows
        and fwd_ch_id is not None
        and str(fwd_ch_id).lower() == str(saved_ch_id).lower()
    ):
        d["rows"] = incoming_rows
        d["mode"] = "pinned"
        d["origin_channel_id"] = saved_ch_id
        d["origin_message_id"] = fwd_msg_id
        persist_store()

    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid)


# ==========================================
# STEP 5: CHANNEL PUBLISHING (IDEMPOTENT)
# ==========================================


async def _publish_to_channel(
    callback: CallbackQuery,
    uid: int,
    pin: bool = False,
):
    if not bot:
        await callback.answer("Bot token is not configured.", show_alert=True)
        return

    lock = get_user_lock(uid)
    if lock.locked():
        await callback.answer("⏳ Sending in progress, please wait...", show_alert=False)
        return

    async with lock:
        d = DRAFTS.get(str(uid))
        if not d or not d.get("has_post"):
            await callback.answer(
                "⚠️ No post ready to send. Please send text, a photo, or forward a message first.",
                show_alert=True,
            )
            return

        txt = (d.get("template_text") or "").strip()
        media_type = d.get("media_type")
        media_fid = d.get("media_file_id")

        if not media_fid and not txt:
            await callback.answer(
                "⚠️ Cannot publish an empty post. Please add text or a photo first.",
                show_alert=True,
            )
            return

        if media_fid and len(txt) > 1024:
            await callback.answer(
                f"⚠️ Caption is too long ({len(txt)}/1024 chars). Please shorten it in Open Editor.",
                show_alert=True,
            )
            return

        if not media_fid and len(txt) > 4096:
            await callback.answer(
                f"⚠️ Post text is too long ({len(txt)}/4096 chars). Please shorten it in Open Editor.",
                show_alert=True,
            )
            return

        ch_id, ch_lbl = get_saved_channel(uid)
        sig = compute_draft_signature(d, ch_id)

        if not pin and d.get("last_published_sig") == sig:
            await callback.answer(
                "⚠️ This post has already been sent to the channel.",
                show_alert=True,
            )
            return

        # Build clean keyboard WITHOUT Open Editor / Send to Channel controls
        clean_kb = build_post_keyboard(uid, include_controls=False)

        try:
            sent_msg = await _send_content_message(ch_id, d, clean_kb)
            sent_msg_id = sent_msg.message_id

            d["last_published_sig"] = sig
            if d.get("mode") == "pinned" or pin:
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
                await callback.answer("Published & pinned!")
                await callback.message.answer(
                    f"📌 Post has been published and pinned in <b>{ch_lbl}</b>!",
                    parse_mode="HTML",
                )
            else:
                await callback.answer("Sent to channel!")
                await callback.message.answer(
                    f"✅ Post has been successfully sent to <b>{ch_lbl}</b>!",
                    parse_mode="HTML",
                )
        except TelegramAPIError as e:
            await callback.answer("Failed to send post", show_alert=True)
            await callback.message.answer(
                f"❌ <b>Failed to publish post to {ch_lbl}.</b>\n"
                "Please verify that this bot is added as an <b>Administrator</b> in the channel "
                "with permission to post messages.\n\n"
                f"Details: <code>{e}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Unexpected error publishing post")
            await callback.answer("Error sending post", show_alert=True)
            await callback.message.answer(
                f"❌ <b>Unexpected error while sending post:</b> <code>{e}</code>",
                parse_mode="HTML",
            )


@dp.callback_query(F.data.in_({"pc_send_new", "pc_send_pin"}))
async def cb_send_to_channel(callback: CallbackQuery):
    uid = callback.from_user.id
    await _publish_to_channel(
        callback=callback,
        uid=uid,
        pin=(callback.data == "pc_send_pin"),
    )


@dp.callback_query(F.data == "pc_update_inplace")
async def cb_update_inplace(callback: CallbackQuery):
    if not bot:
        return
    uid = callback.from_user.id
    d = DRAFTS.get(str(uid))
    if not d:
        await callback.answer("No active draft found.", show_alert=True)
        return

    ch_id = d.get("origin_channel_id") or get_saved_channel(uid)[0]
    msg_id = d.get("origin_message_id")
    if not msg_id:
        live = await fetch_live_pinned_from_channel(ch_id)
        if live and live.get("pinned_message_id"):
            msg_id = live["pinned_message_id"]
            d["origin_channel_id"] = ch_id
            d["origin_message_id"] = msg_id
            persist_store()

    if not msg_id:
        await _publish_to_channel(callback=callback, uid=uid, pin=True)
        return

    clean_kb = build_post_keyboard(uid, include_controls=False)
    try:
        if not d.get("media_type"):
            try:
                await bot.edit_message_text(
                    chat_id=ch_id,
                    message_id=msg_id,
                    text=d.get("template_text") or DEFAULT_PINNED_TEMPLATE_TEXT,
                    reply_markup=clean_kb,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as te:
                if "message is not modified" not in str(te).lower():
                    await bot.edit_message_reply_markup(
                        chat_id=ch_id, message_id=msg_id, reply_markup=clean_kb
                    )
        else:
            await bot.edit_message_caption(
                chat_id=ch_id,
                message_id=msg_id,
                caption=(d.get("template_text") or "").strip() or None,
                reply_markup=clean_kb,
            )
        await callback.answer("Pinned post updated!")
        await callback.message.answer(
            f"✅ Pinned post in <b>{ch_id}</b> updated successfully!",
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        await callback.answer("Update failed", show_alert=True)
        await callback.message.answer(
            f"❌ Failed to update pinned post in <b>{ch_id}</b>.\nDetails: <code>{e}</code>",
            parse_mode="HTML",
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


app = FastAPI(title="PIVOK Post Builder Bot", lifespan=lifespan)
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
        "service": "PIVOK Post Builder Bot",
        "version": "v3.5-single-row-preserved",
        "channel": DEFAULT_CHANNEL,
        "language": "en",
        "buttons": [p["text"] for p in PIVOK_BUTTON_PRESETS],
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

    d = DRAFTS.get(str(uid))
    if not d:
        # Recover gracefully if cloud instance restarted while preview was open in chat
        d = create_new_post_draft(uid=uid, chat_id=cid, text="")
        if pmid:
            d["preview_message_id"] = pmid
            persist_store()
    else:
        if cid:
            d["chat_id"] = cid
        if pmid:
            d["preview_message_id"] = pmid

    selected_keys = d.get("selected_keys")
    custom_rows = d.get("custom_rows")
    if selected_keys is None or custom_rows is None:
        selected_keys, custom_rows = split_rows_into_presets_and_custom(d.get("rows", []))
        d["selected_keys"] = selected_keys
        d["custom_rows"] = custom_rows

    _, ch_lbl = get_saved_channel(uid)
    return {
        "mode": d.get("mode", "post"),
        "channel": ch_lbl,
        "media_type": d.get("media_type"),
        "text": d.get("template_text") or "",
        "rows": d.get("rows", []),
        "presets": get_all_presets_state(selected_keys),
        "custom_rows": custom_rows,
        "auto_sort": d.get("auto_sort", False),
    }


@app.post("/api/save_draft")
async def api_save_draft(request: Request):
    data = await request.json()
    uid = _resolve_webapp_uid(data)
    if not uid:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)

    cid = int(data["cid"]) if str(data.get("cid", "")).lstrip("-").isdigit() else uid
    pmid = int(data["pmid"]) if str(data.get("pmid", "")).isdigit() else None

    d = DRAFTS.get(str(uid))
    if not d:
        d = create_new_post_draft(uid=uid, chat_id=cid, text="")

    new_text = data.get("text")
    if isinstance(new_text, str):
        media_type = d.get("media_type")
        if media_type and len(new_text) > 1024:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Caption is too long ({len(new_text)}/1024 characters).",
                },
                status_code=400,
            )
        if not media_type and not new_text.strip():
            return JSONResponse(
                {
                    "success": False,
                    "error": "Post text cannot be empty for a text-only post.",
                },
                status_code=400,
            )
        if not media_type and len(new_text) > 4096:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Post text is too long ({len(new_text)}/4096 characters).",
                },
                status_code=400,
            )
        d["template_text"] = new_text

    # Primary solastore2026 row-based payload: rows = [[{text, url, style}, ...], ...]
    incoming_rows = data.get("rows")
    if isinstance(incoming_rows, list):
        cleaned_rows: List[List[dict]] = []
        for r in incoming_rows:
            if not isinstance(r, list):
                continue
            c_row = []
            for b in r:
                if not isinstance(b, dict):
                    continue
                b_txt = (b.get("text") or "").strip()
                b_url = normalize_url(b.get("url") or "")
                b_style = b.get("style") or None
                if b_txt and not b_url:
                    return JSONResponse(
                        {
                            "success": False,
                            "error": f"URL is missing for button: {b_txt}",
                        },
                        status_code=400,
                    )
                if b_txt and b_url:
                    c_row.append({"text": b_txt, "url": b_url, "style": b_style})
            if c_row:
                cleaned_rows.append(c_row)
        d["rows"] = cleaned_rows
        sel_keys, custom_rows = split_rows_into_presets_and_custom(cleaned_rows)
        d["selected_keys"] = sel_keys
        d["custom_rows"] = custom_rows
    else:
        preset_urls = data.get("preset_urls")
        if isinstance(preset_urls, dict):
            for k, v in preset_urls.items():
                if k in PRESET_BY_KEY and isinstance(v, str):
                    SAVED_BUTTON_LINKS[k] = normalize_url(v)

        selected_keys = data.get("selected_keys")
        if not isinstance(selected_keys, list):
            selected_keys = d.get("selected_keys", default_selected_preset_keys())
        selected_keys = [k for k in selected_keys if k in PRESET_BY_KEY]

        missing_labels = [
            PRESET_BY_KEY[k]["text"] for k in selected_keys if not get_preset_url(k)
        ]
        if missing_labels:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"URL is not configured yet for: {', '.join(missing_labels)}.",
                },
                status_code=400,
            )
        custom_rows = data.get("custom_rows")
        if not isinstance(custom_rows, list):
            custom_rows = d.get("custom_rows", [])
        d["selected_keys"] = selected_keys
        d["custom_rows"] = custom_rows
        d["rows"] = build_rows_from_selection(selected_keys, custom_rows)

    if "auto_sort" in data:
        d["auto_sort"] = bool(data["auto_sort"])

    d["has_post"] = True
    d["last_published_sig"] = None

    target_chat = d.get("chat_id") or cid
    target_pmid = d.get("preview_message_id") or pmid
    d["chat_id"] = target_chat
    if target_pmid:
        d["preview_message_id"] = target_pmid

    persist_store()

    if bot and target_chat:
        kb = build_post_keyboard(uid, include_controls=True)
        updated_in_place = False
        if target_pmid:
            try:
                if d.get("media_type"):
                    cap = (d.get("template_text") or "").strip() or None
                    await bot.edit_message_caption(
                        chat_id=target_chat,
                        message_id=target_pmid,
                        caption=cap,
                        reply_markup=kb,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=target_chat,
                        message_id=target_pmid,
                        text=d.get("template_text") or " ",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                updated_in_place = True
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    updated_in_place = True
                elif "there is no text in the message" in str(e).lower():
                    try:
                        cap = (d.get("template_text") or "").strip() or None
                        await bot.edit_message_caption(
                            chat_id=target_chat,
                            message_id=target_pmid,
                            caption=cap,
                            reply_markup=kb,
                        )
                        d["media_type"] = "photo"
                        persist_store()
                        updated_in_place = True
                    except Exception as ce:
                        if "message is not modified" in str(ce).lower():
                            updated_in_place = True
                else:
                    logger.warning(f"In-place preview edit fallback to fresh preview: {e}")

        if not updated_in_place:
            await render_draft_preview(target_chat, uid)

    return {"success": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=LOCAL_PORT)
