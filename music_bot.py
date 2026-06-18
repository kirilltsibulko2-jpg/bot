"""
Telegram Music Bot
Скачивает музыку с YouTube и SoundCloud и отправляет в чат.

Зависимости (установить перед запуском):
    pip install python-telegram-bot yt-dlp sclib aiohttp

Как запустить:
    1. Создай бота через @BotFather в Telegram → получи TOKEN
    2. Вставь токен в переменную BOT_TOKEN ниже (или задай env-переменную TELEGRAM_BOT_TOKEN)
    3. python music_bot.py
"""

import os
import asyncio
import logging
import re
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# ─── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8989591700:AAEzpWeI-cFH2aubSp-aQjQRtfJuqS28COc")
MAX_FILE_SIZE_MB = 45          # Telegram лимит для ботов — 50 МБ, берём с запасом
MAX_RESULTS = 5                # Сколько результатов показывать при поиске
DOWNLOAD_DIR = tempfile.mkdtemp(prefix="music_bot_")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ─── Утилиты yt-dlp ───────────────────────────────────────────────────────────

def _ydl_opts_search(source: str, max_results: int) -> dict:
    """Опции для поиска (без скачивания)."""
    if source == "youtube":
        query_prefix = f"ytsearch{max_results}:"
    elif source == "soundcloud":
        query_prefix = f"scsearch{max_results}:"
    else:
        query_prefix = f"ytsearch{max_results}:"
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,       # не скачиваем, только метаданные
        "default_search": query_prefix,
        "skip_download": True,
    }


def _ydl_opts_download(out_path: str) -> dict:
    """Опции для скачивания аудио."""
    return {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": out_path,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
    }


def search_tracks(query: str, source: str = "all") -> list[dict]:
    """
    Ищет треки на YouTube / SoundCloud.
    Возвращает список словарей: {title, url, duration, uploader, source}
    """
    results = []

    sources = []
    if source in ("all", "youtube"):
        sources.append(("youtube", f"ytsearch{MAX_RESULTS}:{query}"))
    if source in ("all", "soundcloud"):
        sources.append(("soundcloud", f"scsearch{MAX_RESULTS}:{query}"))

    for src_name, search_url in sources:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for entry in entries:
                    if not entry:
                        continue
                    duration = entry.get("duration") or 0
                    results.append(
                        {
                            "title": entry.get("title", "Без названия"),
                            "url": entry.get("url") or entry.get("webpage_url", ""),
                            "duration": int(duration),
                            "uploader": entry.get("uploader") or entry.get("channel", ""),
                            "source": src_name,
                            "id": entry.get("id", ""),
                        }
                    )
        except Exception as e:
            log.warning("Ошибка поиска на %s: %s", src_name, e)

    return results[:MAX_RESULTS * len(sources)]


def download_track(url: str) -> str | None:
    """
    Скачивает трек по URL, возвращает путь к mp3-файлу.
    Возвращает None при ошибке или если файл слишком большой.
    """
    out_template = os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s")
    opts = _ydl_opts_download(out_template)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Ищем скачанный mp3
            file_id = info.get("id", "")
            candidates = list(Path(DOWNLOAD_DIR).glob(f"{file_id}*.mp3"))
            if not candidates:
                # Иногда расширение другое — берём первый попавшийся файл с этим id
                candidates = list(Path(DOWNLOAD_DIR).glob(f"{file_id}*"))
            if candidates:
                path = str(candidates[0])
                size_mb = os.path.getsize(path) / 1024 / 1024
                if size_mb > MAX_FILE_SIZE_MB:
                    os.remove(path)
                    return None
                return path
    except yt_dlp.utils.DownloadError as e:
        log.error("Ошибка скачивания: %s", e)
    except Exception as e:
        log.error("Непредвиденная ошибка: %s", e)
    return None


# ─── Форматирование ───────────────────────────────────────────────────────────

def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def source_emoji(source: str) -> str:
    return "🎬" if source == "youtube" else "☁️"


# ─── Хранилище сессий (in-memory) ─────────────────────────────────────────────
# user_id → список треков из последнего поиска
user_sessions: dict[int, list[dict]] = {}


# ─── Обработчики команд ───────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Привет! Я музыкальный бот.*\n\n"
        "Я умею искать и скачивать музыку с YouTube и SoundCloud.\n\n"
        "*Команды:*\n"
        "/search `<запрос>` — поиск везде\n"
        "/yt `<запрос>` — только YouTube\n"
        "/sc `<запрос>` — только SoundCloud\n\n"
        "Или просто напиши название песни — я сам найду! 🎶"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def _do_search(update: Update, query: str, source: str = "all"):
    """Общий поиск с выводом кнопок."""
    msg = await update.message.reply_text(f"🔍 Ищу: *{query}*...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_tracks, query, source)

    if not results:
        await msg.edit_text("😔 Ничего не найдено. Попробуй другой запрос.")
        return

    user_sessions[update.effective_user.id] = results

    lines = []
    keyboard = []
    for i, track in enumerate(results, 1):
        emoji = source_emoji(track["source"])
        dur = fmt_duration(track["duration"]) if track["duration"] else "?:??"
        title_short = track["title"][:45] + ("…" if len(track["title"]) > 45 else "")
        lines.append(f"{i}. {emoji} *{title_short}*\n    👤 {track['uploader']}  ⏱ {dur}")
        keyboard.append(
            [InlineKeyboardButton(f"⬇️ {i}. {title_short}", callback_data=f"dl:{i-1}")]
        )

    text = "🎵 *Результаты поиска:*\n\n" + "\n\n".join(lines)
    markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=markup)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Укажи запрос: `/search название песни`", parse_mode="Markdown")
        return
    await _do_search(update, query, source="all")


async def cmd_youtube(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Укажи запрос: `/yt название песни`", parse_mode="Markdown")
        return
    await _do_search(update, query, source="youtube")


async def cmd_soundcloud(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Укажи запрос: `/sc название песни`", parse_mode="Markdown")
        return
    await _do_search(update, query, source="soundcloud")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обычный текст без команды — ищем везде."""
    query = update.message.text.strip()
    if query:
        await _do_search(update, query, source="all")


async def handle_download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Нажатие кнопки 'Скачать'."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "dl:0", "dl:1", ...
    match = re.match(r"dl:(\d+)", data)
    if not match:
        return

    idx = int(match.group(1))
    user_id = query.from_user.id
    tracks = user_sessions.get(user_id, [])

    if idx >= len(tracks):
        await query.edit_message_text("❌ Сессия устарела. Повтори поиск.")
        return

    track = tracks[idx]
    emoji = source_emoji(track["source"])
    title = track["title"]

    msg = await query.edit_message_text(
        f"{emoji} Скачиваю: *{title}*\nПодожди немного... ⏳",
        parse_mode="Markdown",
    )

    loop = asyncio.get_event_loop()
    file_path = await loop.run_in_executor(None, download_track, track["url"])

    if not file_path:
        await msg.edit_text(
            f"❌ Не удалось скачать трек.\n"
            f"Возможно, файл слишком большой (>{MAX_FILE_SIZE_MB} МБ) "
            f"или трек недоступен."
        )
        return

    try:
        await msg.edit_text(f"📤 Загружаю в Telegram: *{title}*...", parse_mode="Markdown")
        dur = track["duration"] if track["duration"] else None
        with open(file_path, "rb") as audio_file:
            await ctx.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=audio_file,
                title=title,
                performer=track["uploader"],
                duration=dur,
                caption=f"{emoji} {title}\n👤 {track['uploader']}",
            )
        await msg.delete()
    except Exception as e:
        log.error("Ошибка отправки аудио: %s", e)
        await msg.edit_text(f"❌ Ошибка при отправке файла: {e}")
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_СВОЙ_TOKEN_СЮДА":
        print("❌ ОШИБКА: Вставь токен бота в переменную BOT_TOKEN или задай TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("yt", cmd_youtube))
    app.add_handler(CommandHandler("sc", cmd_soundcloud))
    app.add_handler(CallbackQueryHandler(handle_download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🎵 Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
