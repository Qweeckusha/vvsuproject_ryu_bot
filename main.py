import asyncio
import logging
import os
import re
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message
)
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN doesn't exist.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# FSM States
class ProcessingStates(StatesGroup):
    waiting_for_url = State()
    processing = State()  # можно использовать для защиты от дублирования

# Главное меню
def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обработка", callback_data="action:process"), InlineKeyboardButton(text="Описание", callback_data="action:description")]
    ])

def get_cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Отмена", callback_data="action:cancel_processing")]
    ])

# Валидация ссылки на пост VK (упрощённая)
def is_vk_post_url(text: str) -> bool:
    pattern = r'^https?://(www\.)?vk\.com/wall-?\d+_\d+(/.*)?$'
    return bool(re.match(pattern, text))

# Формирование прогресс-бара
def make_progress_bar(percent: int, width: int = 10) -> str:
    filled = int(width * percent // 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {percent}%"

async def send_safe_error_message(chat_id: int, message_id: int | None = None) -> None:
    error_text = (
        "⚠️ Произошла непредвиденная ошибка при обработке запроса.\n"
        "Разработчик уже получает уведомление.\n"
        "Пожалуйста, вернитесь в главное меню и попробуйте снова."
    )
    markup = get_main_menu()
    try:
        if message_id:
            # Пытаемся отредактировать существующее сообщение
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=error_text,
                reply_markup=markup
            )
        else:
            # Или отправить новое
            await bot.send_message(chat_id=chat_id, text=error_text, reply_markup=markup)
    except Exception as e:
        # Если даже это сломалось — просто отправим без кнопок
        try:
            await bot.send_message(chat_id=chat_id, text="⚠️ Ошибка. Вернитесь в главное меню.", reply_markup=markup)
        except:
            pass



# Обработчик /start
@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Добро пожаловать в бота-обработчика постов VK с отчётами по проведённым акциям\nВыберите опцию ниже:",
        reply_markup=get_main_menu()
    )

# Обработчик кнопок из меню
@router.callback_query(lambda c: c.data and c.data.startswith("action:"))
async def handle_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]

    if action == "process":
        await state.set_state(ProcessingStates.waiting_for_url)
        new_text = "Выбран режим *обработки* постов по URL.\nПришлите ссылку на пост VK (формат: `https://vk.com/wall-123456789_1234`)."
        new_markup = get_cancel_markup()
    elif action == "description":
        new_text = (
            "Это бот для обработки входящих URL на посты в социальной сети VK 📲\n\n"
            'Чтобы проанализировать пост — нажмите кнопку "Обработка" и отправьте ссылку 🔗\n\n'
            "Во время работы бот будет отображать прогресс-бар 📊,\nа по достижении 100% — "
            "вышлет подробный отчёт по критериям ✅"
        )
        new_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="action:back_to_main")]
        ])
    elif action == "back_to_main":
        await state.clear()
        new_text = "Добро пожаловать в бота-обработчика постов VK с отчётами по проведённым акциям\nВыберите опцию ниже:"
        new_markup = get_main_menu()
    elif action == "cancel_processing":
        await state.clear()
        new_text = "Операция отменена.\nВыберите опцию ниже:"
        new_markup = get_main_menu()
    else:
        new_text = "Неизвестное действие."
        new_markup = None

    try:
        await callback.message.edit_text(
            text=new_text,
            reply_markup=new_markup,
            parse_mode="Markdown"
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await callback.answer()

# Приём ссылки от пользователя
@router.message(StateFilter(ProcessingStates.waiting_for_url))
async def receive_url(message: Message, state: FSMContext) -> None:
    try:
        url = message.text.strip()

        if not is_vk_post_url(url):
            await message.answer(
                "Некорректная ссылка. Убедитесь, что это пост VK вида:\n`https://vk.com/wall-123456789_1234`",
                parse_mode="Markdown",
                reply_markup=get_cancel_markup()
            )
            return

        await state.update_data(vk_url=url)
        await state.set_state(ProcessingStates.processing)

        progress_msg = await message.answer("Запуск обработки...")
        await state.update_data(progress_message_id=progress_msg.message_id, chat_id=message.chat.id)

        asyncio.create_task(simulate_processing(message.chat.id, progress_msg.message_id, state))

    except Exception as e:
        logging.exception(f"Ошибка при приёме URL от {message.from_user.id}: {e}")
        await send_safe_error_message(message.chat.id)
        await state.clear()

REPORT_CRITERIA = [
    {"id": 1, "status": "✅ Выполнен"},
    {"id": 2, "status": "❌ Не обнаружен"},
    {"id": 3, "status": "🟡 Частично"},
    {"id": 4, "status": "✅ Выполнен"},
    {"id": 5, "status": "✅ Выполнен"},
    {"id": 6, "status": "✅ Выполнен"},
    {"id": 7, "status": "❌ Не выполнен"}
]

async def simulate_processing(chat_id: int, msg_id: int, state: FSMContext) -> None:
    try:
        steps = [
            ("Смотрю пост", 25),
            ('Проливаем кофе', 27),
            ('Ищем глубинные смыслы', 31),
            ("Обрабатываю пост", 50),
            ('Да здравствует Санкт-Петербург', 52),
            ('Увольняем дизайнера', 63),
            ("Оцениваю по критериям", 75),
            ('Уходим с работы', 82),
            ('Попадаем в будущее', 94),
            ("Подготавливаем отчёт", 100),
        ]

        for status_text, percent in steps:
            await asyncio.sleep(1.1)
            bar = make_progress_bar(percent)
            text = f"{status_text}\n{bar}"
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
            except TelegramBadRequest:
                break  # пользователь удалил сообщение

        # === ФОРМИРОВАНИЕ ФИНАЛЬНОГО ОТЧЁТА ===
        await asyncio.sleep(1)

        report_lines = ["✅ Обработка завершена!\n\n📋 **Отчёт по критериям:**"]
        for crit in REPORT_CRITERIA:
            report_lines.append(f"🔹 *Критерий {crit['id']}*: {crit['status']}")

        full_report = "\n".join(report_lines)

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=full_report,
            parse_mode="Markdown",
            reply_markup=get_main_menu()
        )

    except Exception as e:
        logging.exception(f"Ошибка в simulate_processing: {e}")
        await send_safe_error_message(chat_id, msg_id)
    finally:
        await state.clear()

# Обработка отмены во время ввода ссылки (если пользователь нажмёт кнопку "Отмена")
@router.callback_query(lambda c: c.data == "action:cancel_processing")
async def handle_cancel_during_input(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state == ProcessingStates.waiting_for_url.state:
        await state.clear()
        await callback.message.edit_text(
            "Операция отменена.\nВыберите опцию ниже:",
            reply_markup=get_main_menu()
        )
    await callback.answer()

dp.include_router(router)

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())