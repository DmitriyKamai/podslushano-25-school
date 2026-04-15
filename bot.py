"""
Бот «Подслушано 25 школа»: личка → полная копия админу + публикация в целевой чат;
ответы по reply на сообщения бота (в личке админа или в целевом чате).
"""

from __future__ import annotations

import html
import json
import logging
import os
import asyncio
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = _ROOT / "data.sqlite3"
def _env_strip(key: str) -> str:
    v = os.environ.get(key, "") or ""
    return v.strip().strip('"').strip("'").strip()


BOT_TOKEN = _env_strip("BOT_TOKEN")
ADMIN_USER_ID_RAW = _env_strip("ADMIN_USER_ID")
TARGET_GROUP_RAW = _env_strip("TARGET_GROUP_CHAT_ID")
_support = _env_strip("SUPPORT_USERNAME").lstrip("@")

MAX_TEXT = 4000
MAX_CAPTION = 3500
# Лимит частоты приёма в личке (один пользователь), секунды между сообщениями
_private_submit_last_mono: dict[int, float] = {}
_user_submit_locks: dict[int, asyncio.Lock] = {}


def _rate_limit_private_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("RATE_LIMIT_PRIVATE_SEC", "60")))
    except ValueError:
        return 60.0


def _max_document_bytes() -> int:
    try:
        return max(1024, int(os.environ.get("MAX_DOCUMENT_BYTES", str(25 * 1024 * 1024))))
    except ValueError:
        return 25 * 1024 * 1024


def _max_user_text_chars() -> int:
    try:
        return max(1000, int(os.environ.get("MAX_USER_TEXT_CHARS", "12000")))
    except ValueError:
        return 12000


def _max_voice_note_duration_sec() -> int:
    """Если у голоса/кружка нет file_size в API — ограничение по длительности (сек)."""
    try:
        return max(5, int(os.environ.get("MAX_VOICE_NOTE_DURATION_SEC", "600")))
    except ValueError:
        return 600


def _max_video_duration_no_size_sec() -> int:
    """Видео без file_size в ответе API — лимит по duration (сек)."""
    try:
        return max(10, int(os.environ.get("MAX_VIDEO_DURATION_NO_SIZE_SEC", "600")))
    except ValueError:
        return 600


# Альбом: (user_id, media_group_id) -> (первый message_id, time.monotonic())
_album_leader: dict[tuple[int, str], tuple[int, float]] = {}
_ALBUM_LEADER_TTL_SEC = 180.0


def _private_rate_wait_sec(user_id: int) -> int | None:
    """None если можно отправить; иначе секунд подождать (округление вверх)."""
    interval = _rate_limit_private_sec()
    now = time.monotonic()
    last = _private_submit_last_mono.get(user_id)
    if last is None:
        return None
    elapsed = now - last
    if elapsed >= interval:
        return None
    return max(1, int(interval - elapsed) + 1)


def _private_rate_mark(user_id: int) -> None:
    _private_submit_last_mono[user_id] = time.monotonic()
    if len(_private_submit_last_mono) > 100_000:
        _private_submit_last_mono.clear()
        logger.warning("Сброс кэша rate limit (переполнение)")


def _submit_lock(user_id: int) -> asyncio.Lock:
    if len(_user_submit_locks) > 50_000:
        _user_submit_locks.clear()
    if user_id not in _user_submit_locks:
        _user_submit_locks[user_id] = asyncio.Lock()
    return _user_submit_locks[user_id]


def _submission_precheck_msg(msg) -> str | None:
    """Текст ошибки для пользователя или None."""
    max_b = _max_document_bytes()
    if msg.text and len(msg.text) > _max_user_text_chars():
        return "Слишком длинный текст. Сократите сообщение."
    if msg.caption and len(msg.caption) > _max_user_text_chars():
        return "Слишком длинная подпись."
    if msg.photo:
        largest = msg.photo[-1]
        if largest.file_size is not None and largest.file_size > max_b:
            return "Фото слишком большое по размеру. Сожмите или отправьте меньшим файлом."
    if msg.document and msg.document.file_size is not None:
        if msg.document.file_size > max_b:
            return "Файл слишком большой."
    if msg.video:
        if msg.video.file_size is not None:
            if msg.video.file_size > max_b:
                return "Видео слишком большое."
        elif msg.video.duration is not None:
            if msg.video.duration > _max_video_duration_no_size_sec():
                return "Видео слишком длинное. Сократите длительность или отправьте файлом."
    if msg.animation and msg.animation.file_size is not None:
        if msg.animation.file_size > max_b:
            return "Анимация слишком большая."
    if msg.audio and msg.audio.file_size is not None:
        if msg.audio.file_size > max_b:
            return "Аудио слишком большое."
    if msg.voice:
        if msg.voice.file_size is not None:
            if msg.voice.file_size > max_b:
                return "Голосовое слишком большое."
        elif msg.voice.duration is not None:
            if msg.voice.duration > _max_voice_note_duration_sec():
                return "Голосовое слишком длинное. Запишите короче или отправьте как файл."
        else:
            return (
                "Не удалось оценить размер голосового. Отправьте как документ (файлом) "
                "или короткое сообщение."
            )
    if msg.video_note:
        if msg.video_note.file_size is not None:
            if msg.video_note.file_size > max_b:
                return "Видеокружок слишком большой."
        elif msg.video_note.duration is not None:
            if msg.video_note.duration > _max_voice_note_duration_sec():
                return "Видеокружок слишком длинный."
        else:
            return (
                "Не удалось оценить размер видеокружка. Отправьте как видеофайл или короче."
            )
    if msg.sticker and msg.sticker.file_size is not None:
        if msg.sticker.file_size > max_b:
            return "Стикер слишком большой для приёма."
    return None


def _prune_album_leaders() -> None:
    now = time.monotonic()
    if len(_album_leader) < 300:
        return
    for k, (_, t) in list(_album_leader.items()):
        if now - t > _ALBUM_LEADER_TTL_SEC:
            _album_leader.pop(k, None)


def _album_tail_or_register_leader(user_id: int, msg) -> str | None:
    """
    Для media_group: после precheck регистрируем лидера (первый прошедший проверки кадр).
    Остальные кадры того же альбома отклоняем (один слот приёма / одна публикация).
    """
    mg = getattr(msg, "media_group_id", None)
    if mg is None:
        return None
    _prune_album_leaders()
    key = (user_id, str(mg))
    now = time.monotonic()
    prev = _album_leader.get(key)
    if prev is None:
        _album_leader[key] = (msg.message_id, now)
        return None
    first_mid, t0 = prev
    if first_mid == msg.message_id:
        return None
    if now - t0 > _ALBUM_LEADER_TTL_SEC:
        _album_leader[key] = (msg.message_id, now)
        return None
    return (
        "Вы отправили альбом из нескольких файлов. Уже принято первое вложение из этого набора. "
        "Остальное пришлите отдельными сообщениями, если нужно несколько материалов."
    )


def _album_clear_leader_if_leader(user_id: int, msg) -> None:
    mg = getattr(msg, "media_group_id", None)
    if mg is None:
        return
    key = (user_id, str(mg))
    prev = _album_leader.get(key)
    if prev and prev[0] == msg.message_id:
        _album_leader.pop(key, None)


def _admin_user_id() -> int | None:
    if not ADMIN_USER_ID_RAW:
        return None
    try:
        return int(ADMIN_USER_ID_RAW)
    except ValueError:
        return None


def _target_group_chat_id() -> int | None:
    if not TARGET_GROUP_RAW:
        return None
    try:
        return int(TARGET_GROUP_RAW)
    except ValueError:
        return None


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                content_type TEXT,
                text_content TEXT,
                identifiers_json TEXT NOT NULL,
                raw_message_json TEXT,
                recipient_user_id INTEGER,
                recipient_chat_id INTEGER
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "recipient_user_id" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN recipient_user_id INTEGER")
        if "recipient_chat_id" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN recipient_chat_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anon_reply_routes (
                dest_chat_id INTEGER NOT NULL,
                bot_message_id INTEGER NOT NULL,
                anon_sender_user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (dest_chat_id, bot_message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_anon_route_dest ON anon_reply_routes(dest_chat_id)"
        )
        conn.commit()


def _to_dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return None


def collect_identifiers(update: Update) -> dict[str, Any]:
    msg = update.effective_message
    data: dict[str, Any] = {
        "user": _to_dict(update.effective_user),
        "chat": _to_dict(update.effective_chat),
    }
    if msg:
        if msg.sender_chat:
            data["sender_chat"] = _to_dict(msg.sender_chat)
        origin = getattr(msg, "forward_origin", None)
        if origin:
            data["forward_origin"] = _to_dict(origin)
        if msg.contact:
            data["contact"] = _to_dict(msg.contact)
    return data


def _user_display_name(user_dict: dict[str, Any]) -> str:
    first = (user_dict.get("first_name") or "").strip()
    last = (user_dict.get("last_name") or "").strip()
    return " ".join(x for x in (first, last) if x).strip() or "—"


def format_person_lines(label: str, u: dict[str, Any]) -> str:
    name = _user_display_name(u)
    uname = u.get("username")
    username_line = f"@{uname}" if uname else "—"
    uid = u.get("id")
    id_line = str(uid) if uid is not None else "—"
    core = (
        f"Имя: {name}\n"
        f"Username: {username_line}\n"
        f"ID: {id_line}\n"
    )
    return f"{label}\n{core}" if label else core


def clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n… (обрезано)"


def message_content_type(msg) -> str:
    if msg.text:
        return "text"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.audio:
        return "audio"
    if msg.sticker:
        return "sticker"
    if msg.animation:
        return "animation"
    if msg.location:
        return "location"
    if msg.contact:
        return "contact"
    if msg.poll:
        return "poll"
    return "other"


def extract_text_content(msg) -> str | None:
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.contact:
        c = msg.contact
        extra = " ".join(x for x in (c.first_name, c.last_name) if x)
        base = f"contact:{c.phone_number}"
        return f"{base} {extra}".strip() if extra else base
    return None


def format_message_body_for_admin(msg, ctype: str) -> str:
    if msg.text and not msg.photo:
        return msg.text or ""
    if msg.caption:
        return msg.caption
    if msg.contact:
        c = msg.contact
        lines = [f"Контакт, телефон: {c.phone_number}"]
        card_name = " ".join(x for x in (c.first_name, c.last_name) if x)
        if card_name:
            lines.append(f"В карточке: {card_name}")
        if c.user_id is not None:
            lines.append(f"user_id в контакте: {c.user_id}")
        return "\n".join(lines)
    if msg.location:
        return f"Координаты: {msg.location.latitude}, {msg.location.longitude}"
    if msg.poll:
        return msg.poll.question
    if msg.sticker:
        em = (msg.sticker.emoji or "").strip()
        return ("Стикер " + em).strip() if em else "Стикер"
    if msg.document and msg.document.file_name:
        return f"Файл: {msg.document.file_name}"
    if msg.audio:
        if msg.audio.title:
            return f"Аудио: {msg.audio.title}"
        if msg.audio.file_name:
            return f"Аудио: {msg.audio.file_name}"
    labels = {
        "photo": "Фотография (без подписи)",
        "video": "Видео (без подписи)",
        "document": "Документ",
        "voice": "Голосовое сообщение",
        "video_note": "Видеосообщение (кружок)",
        "audio": "Аудио",
        "animation": "GIF / анимация",
        "poll": "Опрос",
        "other": "Вложение",
    }
    return labels.get(ctype, f"Вложение ({ctype})")


def build_admin_notification_text(
    row_id: int, ctype: str, identifiers: dict[str, Any], msg
) -> str:
    u = identifiers.get("user") or {}
    body = format_message_body_for_admin(msg, ctype)
    return (
        f"📥 Подслушано 25 школа — запись #{row_id}\n"
        f"Тип: {ctype}\n\n"
        f"{format_person_lines('', u)}"
        f"Сообщение:\n{body}"
    )


def save_submission(
    *,
    user_id: int | None,
    chat_id: int | None,
    message_id: int | None,
    content_type: str,
    text_content: str | None,
    identifiers: dict[str, Any],
    raw_message: dict[str, Any] | None,
    recipient_user_id: int | None = None,
    recipient_chat_id: int | None = None,
) -> int:
    created = datetime.now(timezone.utc).isoformat()
    identifiers_json = json.dumps(identifiers, ensure_ascii=False)
    raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions
            (created_at, user_id, chat_id, message_id, content_type, text_content,
             identifiers_json, raw_message_json, recipient_user_id, recipient_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created,
                user_id,
                chat_id,
                message_id,
                content_type,
                text_content,
                identifiers_json,
                raw_json,
                recipient_user_id,
                recipient_chat_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def register_anon_reply_routes(
    dest_chat_id: int,
    bot_message_ids: Sequence[int],
    anon_sender_user_id: int,
    submission_id: int,
) -> None:
    if not bot_message_ids:
        return
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for mid in bot_message_ids:
            conn.execute(
                """
                INSERT OR REPLACE INTO anon_reply_routes
                (dest_chat_id, bot_message_id, anon_sender_user_id, submission_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dest_chat_id, int(mid), anon_sender_user_id, submission_id, created),
            )
        conn.commit()


def lookup_anon_reply_route(
    dest_chat_id: int, reply_to_bot_message_id: int
) -> tuple[int, int] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT anon_sender_user_id, submission_id FROM anon_reply_routes
            WHERE dest_chat_id = ? AND bot_message_id = ?
            """,
            (dest_chat_id, reply_to_bot_message_id),
        ).fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1])


def fetch_submission_for_reply(
    submission_id: int,
) -> tuple[str | None, int | None, int | None, int | None, int | None] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT text_content, recipient_user_id, recipient_chat_id, chat_id, message_id
            FROM submissions WHERE id = ?
            """,
            (submission_id,),
        ).fetchone()
    if not row:
        return None
    tc, ru, rc, sch, smid = row[0], row[1], row[2], row[3], row[4]
    return (
        str(tc) if tc is not None else None,
        int(ru) if ru is not None else None,
        int(rc) if rc is not None else None,
        int(sch) if sch is not None else None,
        int(smid) if smid is not None else None,
    )


def format_owner_reply_for_sender_html(
    original_anon_snippet: str,
    owner_reply_text: str,
    *,
    quote_original: bool = True,
) -> str:
    body = clip(owner_reply_text, MAX_TEXT - 400)
    if not quote_original:
        return html.escape(body)
    o = clip(original_anon_snippet.strip() or "📎", 900)
    return f"<blockquote>{html.escape(o)}</blockquote>\n{html.escape(body)}"


async def send_admin_copy_collect_ids(
    bot,
    admin_id: int,
    source_chat_id: int,
    msg,
    admin_text: str,
) -> list[int]:
    """Копия админу; возвращает message_id всех сообщений бота для маршрутизации reply."""
    ids: list[int] = []
    try:
        if msg.text and not msg.photo:
            m = await bot.send_message(chat_id=admin_id, text=clip(admin_text, MAX_TEXT))
            ids.append(m.message_id)
        else:
            copied = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=source_chat_id,
                message_id=msg.message_id,
            )
            ids.append(copied.message_id)
            r = await copied.reply_text(clip(admin_text, MAX_CAPTION))
            ids.append(r.message_id)
    except Exception:
        logger.exception("Не удалось отправить копию админу основным способом")
        m = await bot.send_message(chat_id=admin_id, text=clip(admin_text, MAX_TEXT))
        ids.append(m.message_id)
    return ids


async def publish_to_target_group_collect_ids(
    bot,
    target_chat_id: int,
    source_chat_id: int,
    msg,
) -> list[int]:
    """Публикация в целевой чат без ID отправителя в тексте; message_id для reply-маршрутов."""
    ids: list[int] = []
    try:
        if msg.text and not msg.photo:
            sent = await bot.send_message(chat_id=target_chat_id, text=msg.text or "")
            ids.append(sent.message_id)
        else:
            cp = await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=msg.message_id,
            )
            ids.append(cp.message_id)
    except Exception:
        logger.exception("Не удалось опубликовать в целевой чат target=%s", target_chat_id)
    return ids


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type != "private":
        await msg.reply_text("Напишите боту в личные сообщения.")
        return
    text = (
        "<b>Подслушано 25 школа</b>\n\n"
        "Здесь можно <b>анонимно</b> прислать горячий контент — текст, фото, видео, "
        "голосовое, стикер или другое вложение.\n\n"
        "Просто отправьте сообщение сюда."
    )
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type != "private":
        await msg.reply_text("Напишите боту в личные сообщения.")
        return
    lines = [
        "<b>Справка</b>\n",
        "Отправьте боту в личку сообщение или медиа — оно будет принято анонимно в рамках сервиса.",
    ]
    if _support:
        sup = html.escape(_support, quote=False)
        href = html.escape(f"https://t.me/{_support}", quote=True)
        lines.append(f'\nВопросы: <a href="{href}">@{sup}</a>')
    await msg.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_owner_reply_to_sender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    anon_sender_user_id: int,
    submission_id: int,
) -> None:
    msg = update.effective_message
    if not msg:
        return
    bot = context.bot
    owner_chat_id = msg.chat_id
    reply_to_owner = ReplyParameters(message_id=msg.message_id, chat_id=msg.chat_id)

    sub = fetch_submission_for_reply(submission_id)
    if sub is None:
        try:
            await bot.copy_message(
                chat_id=anon_sender_user_id,
                from_chat_id=owner_chat_id,
                message_id=msg.message_id,
            )
        except Exception:
            logger.exception(
                "Не удалось доставить ответ (нет submission) user=%s",
                anon_sender_user_id,
            )
            await bot.send_message(
                chat_id=msg.chat_id,
                text="Не удалось доставить ответ. Возможно, отправитель заблокировал бота.",
                reply_parameters=reply_to_owner,
            )
            return
        await bot.send_message(
            chat_id=msg.chat_id,
            text="<b>Готово.</b>",
            reply_parameters=reply_to_owner,
            parse_mode=ParseMode.HTML,
        )
        return

    text_content, _rec_uid, _rec_cid, sender_chat_id, sender_message_id = sub
    original = (text_content or "").strip() or "📎"

    sender_thread: ReplyParameters | None = None
    if (
        sender_chat_id is not None
        and sender_message_id is not None
        and sender_chat_id == anon_sender_user_id
    ):
        sender_thread = ReplyParameters(
            message_id=sender_message_id,
            chat_id=sender_chat_id,
        )

    try:
        if msg.text and not msg.photo:
            body_html = format_owner_reply_for_sender_html(
                original,
                msg.text or "",
                quote_original=sender_thread is None,
            )
            sm: dict[str, Any] = {
                "chat_id": anon_sender_user_id,
                "text": body_html,
                "parse_mode": ParseMode.HTML,
            }
            if sender_thread is not None:
                sm["reply_parameters"] = sender_thread
            await bot.send_message(**sm)
        else:
            cm: dict[str, Any] = {
                "chat_id": anon_sender_user_id,
                "from_chat_id": owner_chat_id,
                "message_id": msg.message_id,
            }
            if sender_thread is not None:
                cm["reply_parameters"] = sender_thread
            await bot.copy_message(**cm)
    except Exception:
        logger.exception(
            "Не удалось доставить ответ отправителю user=%s",
            anon_sender_user_id,
        )
        await bot.send_message(
            chat_id=msg.chat_id,
            text="Не удалось доставить ответ. Возможно, отправитель заблокировал бота.",
            reply_parameters=reply_to_owner,
        )
        return

    await bot.send_message(
        chat_id=msg.chat_id,
        text="<b>Готово.</b>",
        reply_parameters=reply_to_owner,
        parse_mode=ParseMode.HTML,
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    chat = update.effective_chat
    user = update.effective_user

    if msg.reply_to_message:
        route = lookup_anon_reply_route(chat.id, msg.reply_to_message.message_id)
        if route is not None:
            anon_uid, sub_id = route
            await handle_owner_reply_to_sender(update, context, anon_uid, sub_id)
            return

    if chat.type != "private" or not user:
        return

    admin_id = _admin_user_id()
    target_id = _target_group_chat_id()
    if admin_id is None or target_id is None:
        await msg.reply_text(
            "Сервис временно недоступен: задайте ADMIN_USER_ID и TARGET_GROUP_CHAT_ID в .env "
            "рядом с bot.py и перезапустите бота."
        )
        logger.warning(
            "Пропуск приёма: admin_id=%s target_group=%s",
            admin_id,
            target_id,
        )
        return

    async with _submit_lock(user.id):
        wait = _private_rate_wait_sec(user.id)
        if wait is not None:
            await msg.reply_text(
                f"Не чаще одного сообщения в {_rate_limit_private_sec():.0f} сек. "
                f"Подождите ещё ~{wait} с."
            )
            return

        if err := _submission_precheck_msg(msg):
            await msg.reply_text(err)
            return

        if err := _album_tail_or_register_leader(user.id, msg):
            await msg.reply_text(err)
            return

        identifiers = collect_identifiers(update)
        ctype = message_content_type(msg)
        text_part = extract_text_content(msg)
        raw_msg = _to_dict(msg)

        try:
            row_id = save_submission(
                user_id=user.id,
                chat_id=chat.id,
                message_id=msg.message_id,
                content_type=ctype,
                text_content=text_part,
                identifiers=identifiers,
                raw_message=raw_msg,
                recipient_user_id=None,
                recipient_chat_id=target_id,
            )

            admin_text = build_admin_notification_text(row_id, ctype, identifiers, msg)
            bot = context.bot

            admin_ids = await send_admin_copy_collect_ids(
                bot, admin_id, chat.id, msg, admin_text
            )
            register_anon_reply_routes(admin_id, admin_ids, user.id, row_id)

            group_ids = await publish_to_target_group_collect_ids(
                bot, target_id, chat.id, msg
            )
            if group_ids:
                register_anon_reply_routes(target_id, group_ids, user.id, row_id)

            await msg.reply_text("Принято. Спасибо!")
            _private_rate_mark(user.id)
        except Exception:
            _album_clear_leader_if_leader(user.id, msg)
            logger.exception("Ошибка при приёме сообщения user_id=%s", user.id)
            await msg.reply_text(
                "Не удалось обработать сообщение. Попробуйте ещё раз или другой тип вложения."
            )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в .env")
    init_db()
    if _admin_user_id() is None:
        logger.warning("ADMIN_USER_ID не задан")
    if _target_group_chat_id() is None:
        logger.warning("TARGET_GROUP_CHAT_ID не задан")

    aid = _admin_user_id()
    tid = _target_group_chat_id()
    logger.info(
        "Конфиг: ADMIN_USER_ID=%s TARGET_GROUP_CHAT_ID=%s RATE_LIMIT_PRIVATE_SEC=%s "
        "MAX_VOICE_NOTE_DURATION_SEC=%s MAX_VIDEO_DURATION_NO_SIZE_SEC=%s",
        aid,
        tid,
        _rate_limit_private_sec(),
        _max_voice_note_duration_sec(),
        _max_video_duration_no_size_sec(),
    )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    private = filters.ChatType.PRIVATE
    # Текст без команд (в т.ч. одни эмодзи / цифры — не COMMAND)
    app.add_handler(
        MessageHandler(private & filters.TEXT & ~filters.COMMAND, handle_user_message)
    )
    app.add_handler(MessageHandler(private & filters.PHOTO, handle_user_message))
    app.add_handler(MessageHandler(private & filters.VIDEO, handle_user_message))
    app.add_handler(MessageHandler(private & filters.Document.ALL, handle_user_message))
    app.add_handler(MessageHandler(private & filters.VOICE, handle_user_message))
    app.add_handler(MessageHandler(private & filters.VIDEO_NOTE, handle_user_message))
    app.add_handler(MessageHandler(private & filters.AUDIO, handle_user_message))
    app.add_handler(MessageHandler(private & filters.ANIMATION, handle_user_message))
    app.add_handler(MessageHandler(private & filters.Sticker.ALL, handle_user_message))
    app.add_handler(MessageHandler(private & filters.LOCATION, handle_user_message))
    app.add_handler(MessageHandler(private & filters.CONTACT, handle_user_message))
    app.add_handler(MessageHandler(private & filters.POLL, handle_user_message))
    # Reply в группе и супергруппе (явно оба типа — на разных сборках PTB)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP,
            handle_user_message,
        )
    )

    logger.info("Бот «Подслушано 25 школа» запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
