import os
import sys
import re
import time
import asyncio
import logging
from typing import Dict, Set

# Добавляем корневую папку в пути Python
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatType, ChatMemberStatus, ChatAction

from src.config import (
    TELEGRAM_BOT_TOKEN,
    validate_config,
    MAX_QUERY_LENGTH,
    USER_COOLDOWN_SECONDS,
)
from src.database import (
    init_db,
    save_or_update_message,
    get_chat_status,
    set_retention_days,
    delete_message_by_id,
    delete_messages_by_text,
    clear_all_chat_messages,
    search_relevant_messages,
)
from src.ai import analyze_homework_with_ai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("unix_bot")

user_cooldowns: Dict[int, float] = {}
processed_messages_cache: Set[tuple] = set()
UNIX_TRIGGER_RE = re.compile(r"\bюникс[ауомe]?\b", re.IGNORECASE)


def get_display_name(message: Message) -> str:
    if not message.from_user:
        return "Участник"
    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full = f"{first} {last}".strip()
    if full:
        return full
    if message.from_user.username:
        return f"@{message.from_user.username}"
    return "Участник"


async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as exc:
        logger.warning("Не удалось проверить статус админа: %s", exc)
        return False


dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    text = (
        "Привет! Я **Юникс** 👋 — помогаю находить домашнее задание прямо в переписке вашей группы.\n\n"
        "**Как со мной общаться:**\n"
        "• Пишите в чат как обычно — я молча запоминаю сообщения о ДЗ.\n"
        "• Когда нужно узнать домашку, просто позовите меня по имени:\n"
        "  — *«Юникс, что было по дз русскому?»*\n"
        "  — *«Юникс, что задали по математике?»*\n"
        "  — *«Юникс, что делать по физике?»*\n\n"
        "ℹ️ Я никогда не придумываю задания и отвечаю строго по сообщениям участников чата.\n"
        "Список команд: /help"
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "📚 **Справка по Юниксу**\n\n"
        "**Примеры вопросов:**\n"
        "• `Юникс, что по русскому?`\n"
        "• `Юникс, какое упражнение по английскому?`\n"
        "• `Юникс, а что училка по физике задала?`\n"
        "• `Юникс, что вообще задали на завтра?`\n\n"
        "**Команды:**\n"
        "• /status — сколько сообщений сейчас в памяти группы\n"
        "• /forget — ответьте (reply) этой командой на ошибочное сообщение, чтобы я забыл его\n"
        "• /settings `дней` — изменить срок хранения истории (для админов)\n"
        "• /clear — полностью очистить память группы (для админов)"
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    stats = await get_chat_status(message.chat.id)
    text = (
        "📊 **Статус Юникса в этом чате:**\n"
        f"• Сообщений в памяти: **{stats['count']}**\n"
        f"• Самая ранняя запись: `{stats['oldest']}`\n"
        f"• Последняя запись: `{stats['newest']}`\n"
        f"• Срок хранения истории: **{stats['retention_days']} дн.**\n\n"
        "✅ Я на связи и запоминаю новые сообщения группы."
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await message.reply("Эта команда работает внутри группы.")
        return

    if not message.from_user or not await is_user_admin(
        bot, message.chat.id, message.from_user.id
    ):
        await message.reply("⚙️ Изменять настройки группы могут только администраторы.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        stats = await get_chat_status(message.chat.id)
        await message.reply(
            f"⚙️ **Настройки группы**\n"
            f"Сейчас сообщения хранятся **{stats['retention_days']} дн.**\n\n"
            f"Чтобы изменить срок хранения, напишите, например:\n"
            f"`/settings 60`",
            parse_mode="Markdown",
        )
        return

    try:
        days = int(parts[1])
        if not (1 <= days <= 365):
            raise ValueError
    except ValueError:
        await message.reply("⚠️ Укажите число дней от 1 до 365. Пример: `/settings 30`", parse_mode="Markdown")
        return

    await set_retention_days(message.chat.id, days)
    await message.reply(f"✅ Срок хранения сообщений изменён на **{days} дн.**", parse_mode="Markdown")


@dp.message(Command("forget"))
async def cmd_forget(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    if message.reply_to_message:
        target_msg = message.reply_to_message
        is_author = (
            target_msg.from_user and target_msg.from_user.id == message.from_user.id
        )
        is_admin = await is_user_admin(bot, message.chat.id, message.from_user.id)

        if not (is_author or is_admin):
            await message.reply(
                "Удалить сообщение из памяти Юникса может его автор или администратор группы."
            )
            return

        deleted = await delete_message_by_id(message.chat.id, target_msg.message_id)
        if deleted:
            await message.reply("🗑 Готово! Я удалил это сообщение из памяти.")
        else:
            await message.reply("Этого сообщения нет в базе.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        if not await is_user_admin(bot, message.chat.id, message.from_user.id):
            await message.reply(
                "Удалять записи по тексту могут только администраторы. "
                "Обычные участники могут ответить командой `/forget` (reply) на своё сообщение.",
                parse_mode="Markdown",
            )
            return
        removed_count = await delete_messages_by_text(message.chat.id, parts[1])
        await message.reply(f"🗑 Удалено записей: **{removed_count}**.", parse_mode="Markdown")
        return

    await message.reply(
        "Чтобы я забыл ошибочное сообщение, ответьте (reply) на него командой `/forget`.",
        parse_mode="Markdown",
    )


@dp.message(Command("clear"))
async def cmd_clear(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE:
        if not message.from_user or not await is_user_admin(
            bot, message.chat.id, message.from_user.id
        ):
            await message.reply("🔒 Очистить память группы может только администратор.")
            return

    removed = await clear_all_chat_messages(message.chat.id)
    await message.reply(f"🧹 Память группы очищена. Удалено записей: **{removed}**.", parse_mode="Markdown")


@dp.edited_message(F.text | F.caption)
async def handle_edited_message(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    content = (message.text or message.caption or "").strip()
    if not content or content.startswith("/"):
        return

    if UNIX_TRIGGER_RE.search(content):
        return

    msg_type = "caption" if message.caption else "text"
    await save_or_update_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id,
        display_name=get_display_name(message),
        text=content,
        message_type=msg_type,
        msg_date=message.date,
    )


@dp.message(F.text | F.caption)
async def handle_chat_message(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    dedup_key = (message.chat.id, message.message_id)
    if dedup_key in processed_messages_cache:
        return
    processed_messages_cache.add(dedup_key)
    if len(processed_messages_cache) > 5000:
        processed_messages_cache.clear()

    content = (message.text or message.caption or "").strip()
    if not content or content.startswith("/"):
        return

    has_name_trigger = bool(UNIX_TRIGGER_RE.search(content))
    bot_info = await bot.get_me()
    is_reply_to_unix = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )

    is_triggered = has_name_trigger or is_reply_to_unix

    # 1. Если не звали Юникса — тихо сохраняем и молчим
    if not is_triggered:
        msg_type = "caption" if message.caption else "text"
        await save_or_update_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            display_name=get_display_name(message),
            text=content,
            message_type=msg_type,
            msg_date=message.date,
        )
        return

    # 2. Обращение к Юниксу: проверка длины и кулдауна
    if len(content) > MAX_QUERY_LENGTH:
        await message.reply(
            "⚠️ Слишком длинный запрос. Спроси короче, например: «Юникс, что задали по русскому?»"
        )
        return

    user_id = message.from_user.id
    now = time.time()
    last_call = user_cooldowns.get(user_id, 0.0)
    if now - last_call < USER_COOLDOWN_SECONDS:
        await message.reply("⏳ Подожди пару секунд перед следующим вопросом.")
        return
    user_cooldowns[user_id] = now

    cleaned_question = UNIX_TRIGGER_RE.sub("", content).strip(" ,.!?:;-—")
    if len(cleaned_question) < 2:
        await message.reply(
            "На связи! 👋 Спроси меня про домашку, например:\n«Юникс, что было по дз русскому?»"
        )
        return

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    # 3. Поиск релевантных сообщений
    relevant_messages, matched_subjects = await search_relevant_messages(
        chat_id=message.chat.id,
        user_query=cleaned_question,
    )

    # 4. Если в базе пусто
    if not relevant_messages:
        if matched_subjects:
            subj_str = ", ".join(matched_subjects)
            await message.reply(
                f"Я не нашёл в доступных сообщениях группы информации о домашнем задании ({subj_str})."
            )
        else:
            await message.reply(
                "Я не нашёл в доступных сообщениях группы информации по твоему вопросу."
            )
        return

    # 5. Анализ контекста через Gemini
    ai_answer = await analyze_homework_with_ai(
        user_question=cleaned_question,
        asker_name=get_display_name(message),
        messages=relevant_messages,
    )
    await message.reply(ai_answer)


async def main() -> None:
    validate_config()
    await init_db()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот Юникс запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот Юникс остановлен.")
      
