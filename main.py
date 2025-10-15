import os
import asyncio
import uuid
import json
import logging
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
from aiohttp import web
from typing import Dict, Any, List

# --- ВСТАНОВИТИ ЗАЛЕЖНОСТІ: pip install python-telegram-bot gspread oauth2client aiohttp requests ---

# --- НАЛАШТУВАННЯ СЕКРЕТІВ (ЧИТАЮТЬСЯ З RENDER) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
SHEET_NAME = os.environ.get("SHEET_NAME") # Наприклад, "School_Elections"
GSPREAD_SECRET_JSON = os.environ.get("GSPREAD_SECRET_JSON") # Повний JSON-рядок сервісного акаунту
KEEP_ALIVE_INTERVAL = 600  # 10 хвилин для Keep-Alive

# --- КОНФІГУРАЦІЯ БОТА ---

# ID адміністраторів, які мають доступ до команди /result
ADMIN_IDS = [
    838464083,  # Ваш перший ID
    6484405296, # Ваш другий ID
]

# Конфігурація класів для генерації кодів (Класи: Кількість учнів)
CLASS_CONFIG = {
    "7-А": 28,
    "7-Б": 30,
    "6-Б": 25,
    "6-А": 27,
    "6-В": 29
}

# Кандидати для голосування
CANDIDATES = {
    "Viktoriia Kochut": "Вікторія Кочут",
    "Oleksandr Bilostotskyi": "Білостоцький Олександр",
    "Yeva Baziuta": "Єва Базюта",
    "Anna Strilchuk": "Анна Стрільчук"
}

# Стани для ConversationHandler
(WAITING_FOR_CODE, WAITING_FOR_CONTACT, WAITING_FOR_VOTE) = range(3)

# --- ЛОГУВАННЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- МЕНЕДЖЕР GOOGLE SHEETS (GSPREAD) ---
class SheetsManager:
    """Клас для безпечної взаємодії з Google Sheets через gspread."""
    def __init__(self, json_creds_str: str, sheet_name: str):
        self.sheet_name = sheet_name
        self.is_connected = False
        self.client = None
        self.sheet = None

        if json_creds_str and sheet_name:
            try:
                # 1. Розпарсити JSON-рядок на Python словник
                creds_dict = json.loads(json_creds_str)
                # 2. Використовувати словник для авторизації
                scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                self.client = gspread.authorize(creds)
                
                # 3. Відкрити таблицю
                self.sheet = self.client.open(sheet_name)
                self.is_connected = True
                logger.info("✅ Успішне підключення до Google Sheets.")
            except Exception as e:
                logger.error(f"❌ Помилка підключення до Google Sheets: {e}")
                self.is_connected = False

    async def get_worksheet(self, title: str):
        """Отримує робочий лист (вкладку) за назвою."""
        if not self.is_connected: return None
        try:
            return await asyncio.to_thread(self.sheet.worksheet, title)
        except gspread.WorksheetNotFound:
            logger.error(f"❌ Вкладка '{title}' не знайдена в таблиці.")
            return None
        except Exception as e:
            logger.error(f"❌ Помилка при отриманні вкладки '{title}': {e}")
            return None

    async def get_all_records(self, worksheet_title: str) -> List[Dict[str, Any]]:
        """Отримує всі записи з робочого листа."""
        ws = await self.get_worksheet(worksheet_title)
        if ws is None: return []
        try:
            return await asyncio.to_thread(ws.get_all_records)
        except Exception as e:
            logger.error(f"❌ Помилка читання даних з '{worksheet_title}': {e}")
            return []

    async def update_cell(self, worksheet_title: str, row: int, col: int, value: Any):
        """Оновлює одну клітинку."""
        ws = await self.get_worksheet(worksheet_title)
        if ws is None: return False
        try:
            await asyncio.to_thread(ws.update_cell, row, col, value)
            return True
        except Exception as e:
            logger.error(f"❌ Помилка оновлення клітинки в '{worksheet_title}' (R{row}, C{col}): {e}")
            return False

    async def append_row(self, worksheet_title: str, values: List[Any]):
        """Додає новий рядок."""
        ws = await self.get_worksheet(worksheet_title)
        if ws is None: return False
        try:
            await asyncio.to_thread(ws.append_row, values)
            return True
        except Exception as e:
            logger.error(f"❌ Помилка додавання рядка до '{worksheet_title}': {e}")
            return False
            
    async def get_all_values(self, worksheet_title: str) -> List[List[Any]]:
        """Отримує всі значення (включаючи заголовки) з робочого листа."""
        ws = await self.get_worksheet(worksheet_title)
        if ws is None: return []
        try:
            return await asyncio.to_thread(ws.get_all_values)
        except Exception as e:
            logger.error(f"❌ Помилка читання всіх значень з '{worksheet_title}': {e}")
            return []

# --- ОДНОРАЗОВА ФУНКЦІЯ ГЕНЕРАЦІЇ КОДІВ ---
async def generate_unique_codes_to_sheets(manager: SheetsManager, config: Dict[str, int]):
    """Генерує унікальні коди на основі CLASS_CONFIG і записує їх у вкладку 'Codes'."""
    codes_ws = await manager.get_worksheet("Codes")
    if codes_ws is None: return

    # Очистка старої таблиці (крім заголовків)
    try:
        await asyncio.to_thread(codes_ws.resize, rows=1, cols=7) # Зменшуємо до 1 рядка
        await asyncio.to_thread(codes_ws.resize, rows=1000) # Повертаємо багато рядків для майбутніх записів
    except Exception as e:
        logger.error(f"Не вдалося очистити стару таблицю Codes: {e}")
        return

    # Заголовки (на випадок, якщо вони були видалені)
    await asyncio.to_thread(codes_ws.update, 'A1:G1', [['Class', 'Student_Count', 'Unique_Code', 'Is_Used', 'Telegram_ID', 'Phone_Number', 'Full_Name']])
    
    rows_to_insert = []
    
    for class_name, count in config.items():
        for _ in range(count):
            # Генеруємо 8-значний код на основі UUID
            unique_code = str(uuid.uuid4()).replace('-', '')[:8].upper()
            # [Class, Student_Count, Unique_Code, Is_Used, Telegram_ID, Phone_Number, Full_Name]
            rows_to_insert.append([class_name, count, unique_code, 'FALSE', '', '', ''])

    if rows_to_insert:
        try:
            # Масове оновлення даних
            await asyncio.to_thread(codes_ws.append_rows, rows_to_insert)
            logger.info(f"✅ Успішно згенеровано та записано {len(rows_to_insert)} унікальних кодів.")
        except Exception as e:
            logger.error(f"❌ Помилка масового запису кодів: {e}")

# --- ФУНКЦІЇ БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Початкова точка, просить користувача ввести унікальний код."""
    user = update.effective_user
    manager: SheetsManager = context.bot_data.get('sheets_manager')
    
    if not manager or not manager.is_connected:
        await update.message.reply_text("❌ Вибачте, сервіс голосування тимчасово недоступний. Спробуйте пізніше.")
        return ConversationHandler.END

    if user.id in ADMIN_IDS:
        await update.message.reply_text("Ви адміністратор. Щоб отримати результати, скористайтесь командою /result.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"🗳️ Вітаємо, {user.first_name}! Для початку голосування, будь ласка, введіть свій **унікальний код** доступу.",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_FOR_CODE

async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє введений код, перевіряє його валідність та статус."""
    code = update.message.text.strip().upper()
    manager: SheetsManager = context.bot_data.get('sheets_manager')

    if len(code) != 8:
        await update.message.reply_text("❌ Код має складатися рівно з 8 символів. Спробуйте ще раз.")
        return WAITING_FOR_CODE

    codes_values = await manager.get_all_values("Codes")
    if not codes_values:
        await update.message.reply_text("❌ Виникла помилка при доступі до бази кодів. Спробуйте пізніше.")
        return ConversationHandler.END

    # Знаходимо код
    header = codes_values[0]
    data_rows = codes_values[1:]

    context.user_data['code_row_index'] = None
    context.user_data['code_info'] = None
    
    # Індекси колонок
    col_code = header.index('Unique_Code') + 1
    col_is_used = header.index('Is_Used') + 1

    for i, row in enumerate(data_rows):
        # i + 2, оскільки індексація gspread починається з 1, і ми пропускаємо рядок заголовків
        row_num = i + 2
        
        if row[col_code - 1] == code:
            # Знайдено код
            context.user_data['code_row_index'] = row_num
            context.user_data['code_info'] = dict(zip(header, row))
            
            if row[col_is_used - 1].upper() == 'TRUE':
                await update.message.reply_text("❌ Цей код вже був використаний для голосування.")
                return WAITING_FOR_CODE
            
            # Код валідний та не використаний. Просимо номер телефону.
            context.user_data['unique_code'] = code
            
            keyboard = [[KeyboardButton("Надіслати мій номер телефону", request_contact=True)]]
            await update.message.reply_text(
                "✅ Код прийнято! Для підтвердження вашої особи, будь ласка, **надішліть свій номер телефону** через кнопку нижче. Це потрібно для ідентифікації.",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            )
            return WAITING_FOR_CONTACT

    # Якщо цикл завершився і код не знайдено
    await update.message.reply_text("❌ Невірний унікальний код. Спробуйте ще раз.")
    return WAITING_FOR_CODE

async def receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє отриманий контакт (номер телефону) та пропонує голосувати."""
    contact = update.message.contact
    user = update.effective_user
    manager: SheetsManager = context.bot_data.get('sheets_manager')
    
    if contact.user_id != user.id:
        await update.message.reply_text("❌ Будь ласка, надішліть саме свій номер телефону, використовуючи кнопку.")
        return WAITING_FOR_CONTACT

    # 1. Оновлюємо рядок у таблиці Codes
    row_num = context.user_data.get('code_row_index')
    
    if row_num:
        try:
            codes_ws = await manager.get_worksheet("Codes")
            await asyncio.to_thread(codes_ws.update_cell, row_num, codes_ws.find("Is_Used").col, 'TRUE')
            await asyncio.to_thread(codes_ws.update_cell, row_num, codes_ws.find("Telegram_ID").col, user.id)
            await asyncio.to_thread(codes_ws.update_cell, row_num, codes_ws.find("Phone_Number").col, contact.phone_number)
            await asyncio.to_thread(codes_ws.update_cell, row_num, codes_ws.find("Full_Name").col, f"{user.full_name} (@{user.username or 'N/A'})")
        except Exception as e:
            logger.error(f"Помилка оновлення рядка коду: {e}")
            await update.message.reply_text("❌ Виникла помилка під час фіксації реєстрації. Зверніться до адміністратора.")
            return ConversationHandler.END

    # 2. Формуємо кнопки для голосування
    keyboard = []
    for key, value in CANDIDATES.items():
        keyboard.append([InlineKeyboardButton(value, callback_data=f"vote_{key}")])

    await update.message.reply_text(
        "🤝 Реєстрація успішна! Тепер ви можете віддати свій єдиний голос. **Зробіть свій вибір:**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_FOR_VOTE

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє вибір кандидата та фіксує голос."""
    query = update.callback_query
    await query.answer()

    manager: SheetsManager = context.bot_data.get('sheets_manager')
    user = query.from_user
    
    # Витягуємо ключ кандидата (наприклад, "Viktoriia Kochut")
    candidate_key = query.data.replace("vote_", "")
    candidate_name = CANDIDATES.get(candidate_key, "Невідомий кандидат")
    
    code_info = context.user_data.get('code_info', {})

    # 1. Записуємо голос у вкладку Votes
    vote_data = [
        datetime.now().isoformat(),
        code_info.get('Class', 'N/A'),
        context.user_data.get('unique_code', 'N/A'),
        user.id,
        user.username or 'N/A',
        user.full_name,
        candidate_name
    ]
    
    success = await manager.append_row("Votes", vote_data)

    if success:
        await query.edit_message_text(
            f"✅ **Ваш голос зараховано!**\n\nВи проголосували за **{candidate_name}**.",
            reply_markup=None,
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text("❌ Виникла помилка під час фіксації вашого голосу. Зверніться до адміністратора.")

    # Очистка даних користувача
    context.user_data.clear()
    return ConversationHandler.END

async def show_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адміністративна команда: виводить результати голосування у відсотках."""
    user = update.effective_user
    manager: SheetsManager = context.bot_data.get('sheets_manager')

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Ця команда доступна лише адміністраторам.")
        return

    await update.message.reply_text("⏳ Збираю та аналізую результати...")

    # 1. Отримуємо всі голоси
    votes_data = await manager.get_all_records("Votes")
    if not votes_data:
        await update.message.reply_text("📊 Наразі жодного голосу не зафіксовано.")
        return

    total_votes = len(votes_data)
    vote_counts: Dict[str, int] = {}

    # 2. Підраховуємо голоси за кандидатів
    for vote in votes_data:
        candidate = vote.get('Candidate_Voted', 'Невідомий')
        vote_counts[candidate] = vote_counts.get(candidate, 0) + 1

    # 3. Формуємо вивід результатів
    results_text = f"📊 **Результати Виборів Президента Школи**\n\n"
    results_text += f"Всього зарахованих голосів: **{total_votes}**\n\n"
    
    sorted_results = sorted(vote_counts.items(), key=lambda item: item[1], reverse=True)
    
    for candidate, count in sorted_results:
        percentage = (count / total_votes) * 100 if total_votes > 0 else 0
        
        # Створюємо простий графік за допомогою емодзі
        blocks = int(percentage / 10)
        chart = '█' * blocks + '░' * (10 - blocks)
        
        results_text += (
            f"**{candidate}**:\n"
            f"   {count} голосів ({percentage:.2f}%)\n"
            f"   `{chart}`\n"
        )
        
    await update.message.reply_text(results_text, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Скасовує активну розмову."""
    await update.effective_message.reply_text(
        'Операцію скасовано.',
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

# --- WEBHOOK ТА KEEP-ALIVE ---

async def init_webhook(application: Application, url: str) -> None:
    """Встановлює вебхук."""
    if url:
        try:
            await application.bot.set_webhook(url=url)
            logger.info(f"Вебхук успішно встановлено на {url}")
        except Exception as e:
            logger.error(f"Не вдалося встановити вебхук: {e}")

async def keep_alive_task(app: web.Application):
    """Задача для підтримки активності сервера (Keep-Alive)."""
    while True:
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)
        # Надсилаємо запит до /status endpoint
        try:
            async with app['ptb_app'].http_client.get(f"{app['ptb_app'].webhook_url_base}/status", timeout=5) as resp:
                if resp.status == 200:
                    logger.info("✅ Keep-Alive успішний.")
                else:
                    logger.warning(f"⚠️ Keep-Alive отримав статус: {resp.status}")
        except Exception as e:
            logger.error(f"❌ Keep-Alive помилка: {e}")

async def status_handler(request: web.Request) -> web.Response:
    """Endpoint для перевірки статусу (використовується Keep-Alive)."""
    return web.Response(text="Bot is running", status=200)

async def handle_telegram_webhook(request: web.Request) -> web.Response:
    """Обробляє вхідні оновлення від Telegram."""
    application = request.app['ptb_app']
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response()
    except json.JSONDecodeError:
        logger.warning("Не вдалося розпарсити JSON з вебхука Telegram.")
        return web.Response(status=400)
    except Exception as e:
        logger.error(f"Помилка в обробнику вебхука: {e}")
        return web.Response(status=500)

async def main() -> None:
    # Перевірка наявності секрету в змінних середовища
    if not GSPREAD_SECRET_JSON:
        logger.error("❌ Критична помилка: змінна середовища GSPREAD_SECRET_JSON не знайдена. Робота з Sheets неможлива.")

    # Ініціалізація менеджера Google Sheets
    sheets_manager = SheetsManager(GSPREAD_SECRET_JSON, SHEET_NAME)

    # --- Створення та налаштування Application ---
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data['sheets_manager'] = sheets_manager
    
    # --- Обробник розмови для голосування ---
    voting_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            # Фільтр для кнопки "Надіслати контакт"
            WAITING_FOR_CONTACT: [MessageHandler(filters.CONTACT, receive_contact)], 
            WAITING_FOR_VOTE: [CallbackQueryHandler(handle_vote, pattern='^vote_.*$')]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        conversation_timeout=3600 # Тайм-аут 1 година
    )

    application.add_handler(voting_conv)
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("result", show_results)) # Адмін-команда
    
    # --- Налаштування aiohttp веб-сервера ---
    web_app = web.Application()
    web_app['ptb_app'] = application
    web_app.add_routes([
        web.get('/status', status_handler),
        web.post(f'/{TELEGRAM_BOT_TOKEN}', handle_telegram_webhook)
    ])
    
    # 🌟 ВИПРАВЛЕННЯ: Додаємо keep-alive задачу ДО runner.setup()
    web_app.on_startup.append(lambda app: asyncio.create_task(keep_alive_task(app)))

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)

    # --- Запуск ---
    await application.initialize()
    await application.start()

    # 1. Встановлюємо вебхук
    await init_webhook(application, WEBHOOK_URL)

    # 2. Запускаємо веб-сервер
    await site.start()
    logger.info(f"Веб-сервер запущено на http://0.0.0.0:{port}")
    
    # Головний цикл для підтримки роботи
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        # Коректне завершення роботи
        await application.stop()
        await runner.cleanup()
        logger.info("Бот та веб-сервер зупинено.")

if __name__ == '__main__':
    # --- БЛОК ДЛЯ ОДНОРАЗОВОЇ ГЕНЕРАЦІЇ КОДІВ ---
    
    async def initial_setup():
        """Локальна функція для генерації кодів перед запуском бота на Render."""
        json_creds = os.environ.get("GSPREAD_SECRET_JSON")
        sheet_name = os.environ.get("SHEET_NAME")
        if not json_creds or not sheet_name:
             print("\n\n❌ ПОМИЛКА: Не встановлені змінні GSPREAD_SECRET_JSON або SHEET_NAME.")
             print("Переконайтеся, що ви їх експортували в терміналі перед запуском!")
             return

        print("\n\n⏳ Починаю генерацію унікальних кодів та очищення таблиці Codes...")
        manager = SheetsManager(json_creds, sheet_name)
        
        # Даємо час на асинхронне підключення до Google Sheets
        await asyncio.sleep(5) 
        
        if manager.is_connected:
            await generate_unique_codes_to_sheets(manager, CLASS_CONFIG)
            print("\n✅ ГЕНЕРАЦІЮ КОДІВ ЗАВЕРШЕНО. ПЕРЕВІРТЕ ТАБЛИЦЮ GOOGLE SHEETS.")
        else:
            print("\n❌ ГЕНЕРАЦІЯ НЕ ВДАЛАСЯ. Перевірте, чи коректно вставлено JSON-ключ.")


    try:
        # ЗАУВАЖТЕ: 
        # 1. Для одноразової генерації локально, РОЗКОМЕНТУЙТЕ рядок нижче і ЗАКОМЕНТУЙТЕ рядок з asyncio.run(main()).
        # 2. Для запуску бота на Render (чи після генерації), ЗАКОМЕНТУЙТЕ рядок нижче і РОЗКОМЕНТУЙТЕ рядок з asyncio.run(main()).
        
        # asyncio.run(initial_setup()) 
        
        # Запуск бота:
        asyncio.run(main()) 

    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено вручну.")
