"""
Бот «Подслушано 25 школа»: личка → полная копия админу + публикация в целевой чат;
ответы по reply на сообщения бота только в личке админа (в целевой группе ответы не маршрутизируются).
"""

from __future__ import annotations

import html
import json
import logging
import os
import asyncio
import sqlite3
import time
from collections import deque
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

# Глобальный лимит приёмов (все пользователи), скользящее окно + учёт in-flight
_global_accept_mono: deque[float] = deque()
_global_accept_inflight = 0
_global_accept_lock = asyncio.Lock()


def _rate_limit_private_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("RATE_LIMIT_PRIVATE_SEC", "60")))
    except ValueError:
        return 60.0


def _global_rate_window_sec() -> float:
    try:
        return max(10.0, float(os.environ.get("GLOBAL_RATE_WINDOW_SEC", "60")))
    except ValueError:
        return 60.0


def _max_global_submissions_per_window() -> int | None:
    """
    Макс. успешно принятых сообщений за окно GLOBAL_RATE_WINDOW_SEC со всех user_id.
    0 или отрицательное в MAX_GLOBAL_SUBMISSIONS_PER_WINDOW — выключено.
    По умолчанию без env: 10 приёмов за окно (см. GLOBAL_RATE_WINDOW_SEC).
    """
    try:
        v = int(os.environ.get("MAX_GLOBAL_SUBMISSIONS_PER_WINDOW", "10"))
    except ValueError:
        return None
    if v <= 0:
        return None
    return max(5, v)


def _global_prune_accepted(window: float) -> None:
    now = time.monotonic()
    while _global_accept_mono and now - _global_accept_mono[0] > window:
        _global_accept_mono.popleft()


async def _global_submission_enter() -> str | None:
    """
    Зарезервировать слот глобального лимита (учёт in-flight, без гонки).
    None — можно продолжать; иначе текст отказа пользователю.
    """
    global _global_accept_inflight
    cap = _max_global_submissions_per_window()
    if cap is None:
        return None
    window = _global_rate_window_sec()
    now = time.monotonic()
    async with _global_accept_lock:
        _global_prune_accepted(window)
        if len(_global_accept_mono) + _global_accept_inflight >= cap:
            logger.warning(
                "Глобальный лимит приёма: принято+в обработке %s/%s за %.0f с",
                len(_global_accept_mono) + _global_accept_inflight,
                cap,
                window,
            )
            return (
                "Сейчас очень много обращений к боту. Попробуйте отправить сообщение "
                "через несколько минут."
            )
        _global_accept_inflight += 1
    return None


async def _global_submission_leave_success() -> None:
    global _global_accept_inflight
    cap = _max_global_submissions_per_window()
    if cap is None:
        return
    window = _global_rate_window_sec()
    async with _global_accept_lock:
        _global_accept_inflight = max(0, _global_accept_inflight - 1)
        _global_accept_mono.append(time.monotonic())
        _global_prune_accepted(window)


async def _global_submission_leave_failure() -> None:
    global _global_accept_inflight
    if _max_global_submissions_per_window() is None:
        return
    async with _global_accept_lock:
        _global_accept_inflight = max(0, _global_accept_inflight - 1)


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


# Один альбом (media_group_id): после успешного кадра до deadline не действует пауза между кадрами
_album_burst_free_until: dict[tuple[int, str], float] = {}


def _album_burst_window_sec() -> float:
    """Сколько секунд после принятого кадра альбома не требовать паузу для остальных кадров."""
    try:
        return max(15.0, float(os.environ.get("ALBUM_BURST_WINDOW_SEC", "120")))
    except ValueError:
        return 120.0


def _prune_album_burst_deadlines() -> None:
    if len(_album_burst_free_until) < 400:
        return
    now = time.monotonic()
    for k, until in list(_album_burst_free_until.items()):
        if now >= until:
            _album_burst_free_until.pop(k, None)


def _private_rate_wait_sec(user_id: int, msg) -> int | None:
    """None если можно отправить; иначе секунд подождать (округление вверх)."""
    now = time.monotonic()
    mg = getattr(msg, "media_group_id", None)
    if mg is not None:
        _prune_album_burst_deadlines()
        until = _album_burst_free_until.get((user_id, str(mg)))
        if until is not None and now < until:
            return None
    interval = _rate_limit_private_sec()
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


def _is_start_command_text(text: str | None) -> bool:
    if not text:
        return False
    raw = text.strip()
    if not raw.startswith("/"):
        return False
    cmd = raw.split(maxsplit=1)[0].lower()
    return cmd == "/start" or cmd.startswith("/start@")


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
                content_fingerprint TEXT,
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
        if "content_fingerprint" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN content_fingerprint TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_submissions_fp_time "
            "ON submissions(content_fingerprint, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_submissions_uid_fp_time "
            "ON submissions(user_id, content_fingerprint, created_at)"
        )
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
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


def _message_identity_piece(msg, ctype: str) -> str:
    if msg.text:
        return msg.text.strip()
    if msg.caption:
        return msg.caption.strip()
    if msg.photo:
        return msg.photo[-1].file_unique_id
    if msg.video:
        return msg.video.file_unique_id
    if msg.document:
        return msg.document.file_unique_id
    if msg.voice:
        return msg.voice.file_unique_id
    if msg.video_note:
        return msg.video_note.file_unique_id
    if msg.audio:
        return msg.audio.file_unique_id
    if msg.sticker:
        return msg.sticker.file_unique_id
    if msg.animation:
        return msg.animation.file_unique_id
    if msg.location:
        return f"{msg.location.latitude:.6f},{msg.location.longitude:.6f}"
    if msg.contact:
        return f"{msg.contact.phone_number}:{msg.contact.user_id}"
    if msg.poll:
        return (msg.poll.question or "").strip()
    return ctype


def build_message_fingerprint(msg, ctype: str) -> str:
    piece = _message_identity_piece(msg, ctype)
    return f"{ctype}|{piece}"


def is_blacklisted(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM blacklist WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def blacklist_users(user_ids: set[int], reason: str) -> None:
    if not user_ids:
        return
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for uid in user_ids:
            conn.execute(
                """
                INSERT OR IGNORE INTO blacklist(user_id, reason, created_at)
                VALUES (?, ?, ?)
                """,
                (uid, reason, created),
            )
        conn.commit()


def detect_and_apply_blacklist_for_duplicate(
    user_id: int,
    fingerprint: str,
) -> tuple[bool, set[int]]:
    now = datetime.now(timezone.utc)
    minute_start = now.replace(second=0, microsecond=0).isoformat()
    minute_end = now.replace(second=59, microsecond=999999).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        same_user_row = conn.execute(
            """
            SELECT COUNT(*) FROM submissions
            WHERE user_id = ?
              AND content_fingerprint = ?
              AND created_at >= ? AND created_at <= ?
            """,
            (user_id, fingerprint, minute_start, minute_end),
        ).fetchone()
        matched_rows = conn.execute(
            """
            SELECT DISTINCT user_id FROM submissions
            WHERE content_fingerprint = ?
              AND created_at >= ? AND created_at <= ?
              AND user_id IS NOT NULL
            """,
            (fingerprint, minute_start, minute_end),
        ).fetchall()
    same_user_count = int(same_user_row[0]) if same_user_row else 0
    matched_users = {int(r[0]) for r in matched_rows if r[0] is not None}
    other_users = {uid for uid in matched_users if uid != user_id}
    should_block = same_user_count >= 1 or bool(other_users)
    if not should_block:
        return False, set()
    to_block = {user_id}
    if other_users:
        to_block.update(other_users)
        reason = (
            "Дубликат сообщения за минуту между разными пользователями "
            f"(fingerprint={fingerprint})"
        )
    else:
        reason = (
            "Повторная отправка одинакового сообщения одним пользователем "
            f"в течение минуты (fingerprint={fingerprint})"
        )
    blacklist_users(to_block, reason)
    return True, to_block


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
    content_fingerprint: str | None,
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
            (created_at, user_id, chat_id, message_id, content_type, text_content, content_fingerprint,
             identifiers_json, raw_message_json, recipient_user_id, recipient_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created,
                user_id,
                chat_id,
                message_id,
                content_type,
                text_content,
                content_fingerprint,
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
    """Публикация в целевой чат без ID отправителя в тексте; message_id — для логов/расширений."""
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
        tid = _target_group_chat_id()
        if tid is not None and chat.id == tid:
            return
        route = lookup_anon_reply_route(chat.id, msg.reply_to_message.message_id)
        if route is not None:
            anon_uid, sub_id = route
            await handle_owner_reply_to_sender(update, context, anon_uid, sub_id)
            return

    if chat.type != "private" or not user:
        return

    if is_blacklisted(user.id):
        await msg.reply_text("Вы добавлены в блек-лист за дублирование сообщений.")
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
        wait = _private_rate_wait_sec(user.id, msg)
        if wait is not None:
            await msg.reply_text(
                f"Не чаще одного сообщения в {_rate_limit_private_sec():.0f} сек. "
                f"Подождите ещё ~{wait} с."
            )
            return

        if err := _submission_precheck_msg(msg):
            await msg.reply_text(err)
            return

        identifiers = collect_identifiers(update)
        ctype = message_content_type(msg)
        text_part = extract_text_content(msg)
        fingerprint = build_message_fingerprint(msg, ctype)
        raw_msg = _to_dict(msg)

        if not _is_start_command_text(msg.text):
            blocked, blocked_users = detect_and_apply_blacklist_for_duplicate(
                user.id, fingerprint
            )
            if blocked:
                logger.warning(
                    "Пользователи добавлены в blacklist за дубли: %s",
                    sorted(blocked_users),
                )
                await msg.reply_text(
                    "Сообщение отклонено: обнаружен дубликат в ту же минуту. "
                    "Вы добавлены в блек-лист."
                )
                return

        if err := await _global_submission_enter():
            await msg.reply_text(err)
            return

        try:
            row_id = save_submission(
                user_id=user.id,
                chat_id=chat.id,
                message_id=msg.message_id,
                content_type=ctype,
                text_content=text_part,
                content_fingerprint=fingerprint,
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

            await publish_to_target_group_collect_ids(bot, target_id, chat.id, msg)

            await msg.reply_text("Принято. Спасибо!")
            _private_rate_mark(user.id)
            await _global_submission_leave_success()
            mg = getattr(msg, "media_group_id", None)
            if mg is not None:
                _prune_album_burst_deadlines()
                _album_burst_free_until[(user.id, str(mg))] = (
                    time.monotonic() + _album_burst_window_sec()
                )
        except Exception:
            await _global_submission_leave_failure()
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
    gcap = _max_global_submissions_per_window()
    logger.info(
        "Конфиг: ADMIN_USER_ID=%s TARGET_GROUP_CHAT_ID=%s RATE_LIMIT_PRIVATE_SEC=%s "
        "ALBUM_BURST_WINDOW_SEC=%s MAX_GLOBAL_SUBMISSIONS_PER_WINDOW=%s GLOBAL_RATE_WINDOW_SEC=%s "
        "MAX_VOICE_NOTE_DURATION_SEC=%s MAX_VIDEO_DURATION_NO_SIZE_SEC=%s",
        aid,
        tid,
        _rate_limit_private_sec(),
        _album_burst_window_sec(),
        gcap if gcap is not None else "off",
        _global_rate_window_sec(),
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
