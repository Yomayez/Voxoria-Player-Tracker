import asyncio
import aiohttp
import os
import time
import aiosqlite
import hashlib
import base64
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest
import requests

load_dotenv()


class OrderForm(StatesGroup):
    waiting_address = State()


token_key = os.getenv('TOKEN')
if not token_key:
    raise RuntimeError("Не найден TOKEN. Добавь его в .env")

bot = Bot(token=token_key)
dp = Dispatcher()
rt = Router()
dp.include_router(rt)

resp = requests.get('https://raw.githubusercontent.com/Yomayez/allow-users/refs/heads/main/hashes.txt')
allow_users = resp.text.split(' ')
# Формат:
# {
#     user_id: {
#         "nickname": str,
#         "chat_id": int,
#         "message_id": int,
#         "task": asyncio.Task
#     }
# }
active_tracks = {}

DB_FILE = "player_positions.db"


def get_stop_keyboard():
    """Создает инлайн-клавиатуру с кнопкой остановки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🛑 Остановить отслеживание", callback_data="stop_track")
    return builder.as_markup()


def safe_int(value, default: int = 0) -> int:
    """Безопасно переводит значение в int."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


async def init_db():
    """Создает таблицу для хранения координат игроков."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS player_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                world TEXT NOT NULL,
                timestamp INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_positions_nickname_timestamp
            ON player_positions(nickname, timestamp DESC)
            """
        )
        await db.commit()


async def save_player_position(nickname: str, data: dict):
    """Сохраняет координаты игрока и время получения в БД."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            INSERT INTO player_positions (nickname, x, y, z, world, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                nickname.lower(),
                safe_int(data.get("x")),
                safe_int(data.get("y")),
                safe_int(data.get("z")),
                data.get("world") or "unknown",
                int(time.time())
            )
        )
        await db.commit()


async def get_last_position(nickname: str):
    """Возвращает последнюю сохраненную позицию игрока из БД."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT x, y, z, world, timestamp
            FROM player_positions
            WHERE nickname = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (nickname.lower(),)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)


def format_elapsed(seconds: int) -> str:
    """Форматирует прошедшее время в человекочитаемый вид."""
    seconds = max(0, int(seconds))

    def plural(n: int, one: str, few: str, many: str) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return one
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return few
        return many

    if seconds < 60:
        return f"{seconds} {plural(seconds, 'секунда', 'секунды', 'секунд')}"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {plural(minutes, 'минута', 'минуты', 'минут')}"

    hours = seconds // 3600
    if hours < 24:
        return f"{hours} {plural(hours, 'час', 'часа', 'часов')}"

    days = seconds // 86400
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


async def fetch_players(session: aiohttp.ClientSession, url: str):
    """Получает список игроков из JSON API."""
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if isinstance(data, dict):
                players = data.get("players", [])
                return players if isinstance(players, list) else []
            return []
    except Exception as e:
        print(f"Ошибка запроса к {url}: {e}")
        return []


async def get_data(nickname: str):
    """Получает координаты игрока из обычного мира, незера и энда."""
    nickname = nickname.lower()
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Проверка Overworld
        players_over = await fetch_players(
            session,
            "https://map.vo-xo.com/maps/world/live/players.json"
        )

        for p in players_over:
            if not isinstance(p, dict):
                continue

            if p.get("name", "").lower() != nickname:
                continue

            if p.get("foreign", False):
                continue

            pos = p.get("position", {})
            if not isinstance(pos, dict):
                continue

            return {
                "x": pos.get("x", 0),
                "y": pos.get("y", 0),
                "z": pos.get("z", 0),
                "world": "overworld"
            }

        # Проверка Nether / End
        players_nether = await fetch_players(
            session,
            "https://map.vo-xo.com/maps/world_the_nether/live/players.json"
        )

        for p in players_nether:
            if not isinstance(p, dict):
                continue

            if p.get("name", "").lower() != nickname:
                continue

            pos = p.get("position", {})
            if not isinstance(pos, dict):
                continue

            # Логика как в твоем оригинале:
            # если foreign == False -> nether,
            # если foreign == True -> end.
            world = "end" if p.get("foreign", False) else "nether"

            return {
                "x": pos.get("x", 0),
                "y": pos.get("y", 0),
                "z": pos.get("z", 0),
                "world": world
            }

    return None


async def build_status_text(nickname: str, data):
    """Собирает текст сообщения для онлайн/оффлайн статуса."""
    url = f"https://vo-xo.com/community/u/{nickname}"

    if data is not None:
        return (
            f"<b>Пользователь:</b> <a href=\"{url}\">{nickname}</a>\n"
            f"<b>Координаты:</b> "
            f"X:{safe_int(data.get('x'))} "
            f"Y:{safe_int(data.get('y'))} "
            f"Z:{safe_int(data.get('z'))}\n"
            f"<b>Мир:</b> {data.get('world') or 'unknown'}\n\n"
            f"<i>Обновление...</i>"
        )

    last = await get_last_position(nickname)

    if last:
        elapsed = int(time.time()) - safe_int(last.get("timestamp"))
        return (
            f"<b>Пользователь:</b> <a href=\"{url}\">{nickname}</a>\n"
            f"<b>Статус:</b> человек не в сети {format_elapsed(elapsed)}\n"
            f"<b>Последние координаты:</b> "
            f"X:{safe_int(last.get('x'))} "
            f"Y:{safe_int(last.get('y'))} "
            f"Z:{safe_int(last.get('z'))}\n"
            f"<b>Мир:</b> {last.get('world') or 'unknown'}\n\n"
            f"<i>Обновление...</i>"
        )

    return (
        f"<b>Пользователь:</b> <a href=\"{url}\">{nickname}</a>\n"
        f"<b>Статус:</b> Не в сети\n"
        f"<b>Последние координаты:</b> неизвестны\n\n"
        f"<i>Обновление...</i>"
    )


async def track_player_loop(user_id: int, chat_id: int, nickname: str, message_id: int):
    """Фоновая функция, которая регулярно обновляет сообщение."""
    last_text = ""

    while True:
        try:
            data = await get_data(nickname.lower())

            if data is not None:
                try:
                    await save_player_position(nickname, data)
                except Exception as e:
                    print(f"Не удалось сохранить координаты в БД: {e}")

            text = await build_status_text(nickname, data)

            if text != last_text:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_stop_keyboard()
                    )
                    last_text = text
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e):
                        last_text = text
                    else:
                        print(f"Ошибка редактирования сообщения: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Ошибка в цикле отслеживания: {e}")

        await asyncio.sleep(1)


@rt.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id

    salten = "МункаЛох" + str(user_id)
    raw_bytes = hashlib.sha256(salten.encode('utf-8')).digest()
    short_hash = base64.b64encode(raw_bytes).decode('utf-8')

    if short_hash in allow_users:
        await message.answer(
            text=(
                "Привет! Это неофициальный бот для отслеживания \n"
                "координат игроков проекта "
                "<a href=\"https://vo-xo.com\"><b>Voxoria</b></a>"
            ),
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    else:
        await message.answer(text="Сьебалось чудище")


@rt.message(F.text)
async def search(message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id

    salten = "МункаЛох" + str(user_id)
    raw_bytes = hashlib.sha256(salten.encode('utf-8')).digest()
    short_hash = base64.b64encode(raw_bytes).decode('utf-8')

    if short_hash not in allow_users:
        await message.answer(text="Сьебалось чудище")
        return

    nickname = message.text.strip()
    if not nickname:
        return

    # Если пользователь уже кого-то отслеживает, останавливаем старую задачу.
    old_track = active_tracks.pop(user_id, None)
    if old_track:
        old_track["task"].cancel()

        try:
            await bot.edit_message_text(
                chat_id=old_track.get("chat_id"),
                message_id=old_track.get("message_id"),
                text="<b>Отслеживание остановлено.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
        except Exception:
            pass

    try:
        data = await get_data(nickname.lower())

        if data is not None:
            try:
                await save_player_position(nickname, data)
            except Exception as e:
                print(f"Не удалось сохранить координаты в БД: {e}")

        text = await build_status_text(nickname, data)
    except Exception as e:
        print(f"Ошибка при первом получении данных: {e}")
        text = await build_status_text(nickname, None)

    sent_msg = await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_stop_keyboard()
    )

    task = asyncio.create_task(
        track_player_loop(
            user_id,
            message.chat.id,
            nickname,
            sent_msg.message_id
        )
    )

    active_tracks[user_id] = {
        "nickname": nickname,
        "chat_id": message.chat.id,
        "message_id": sent_msg.message_id,
        "task": task
    }


@rt.callback_query(F.data == "stop_track")
async def stop_tracking(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    track = active_tracks.get(user_id)

    if not track:
        await callback.answer("Отслеживание не запущено.")
        return

    if callback.message is None:
        await callback.answer("Не удалось получить сообщение.")
        return

    if track.get("message_id") != callback.message.message_id:
        await callback.answer("Эта кнопка устарела.")
        return

    active_tracks.pop(user_id, None)
    track["task"].cancel()

    current_text = callback.message.text or ""
    base_text = current_text.split("\n\n")[0]

    if base_text:
        clean_text = f"{base_text}\n\n<b>Отслеживание остановлено.</b>"
    else:
        clean_text = "<b>Отслеживание остановлено.</b>"

    try:
        await callback.message.edit_text(
            text=clean_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )
    except Exception as e:
        print(f"Ошибка при остановке отслеживания: {e}")

    await callback.answer("Отслеживание остановлено!")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())