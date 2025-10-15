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
import aiohttp # Додаємо для коректної роботи ClientSession в keep_alive
from typing import Dict, Any, List

# --- ВСТАНОВИТИ ЗАЛЕЖНОСТІ: pip install python-telegram-bot gspread oauth2client aiohttp requests ---

# --- КОНФІГУРАЦІЯ СЕКРЕТІВ З RENDER (ТІЛЬКИ ДЛЯ БЕЗПЕЧНИХ КЛЮЧІВ) ---
# Ці змінні будуть читатися з Render.
GSPREAD_SECRET_JSON = os.environ.get("GSPREAD_SECRET_JSON", '{"type": "service_account", "placeholder": "PASTE YOUR FULL JSON HERE"}') # Секрет
INITIAL_CODE_GENERATION = os.environ.get("INITIAL_CODE_GENERATION", 'FALSE').upper() # 'TRUE' або 'FALSE'

# --- ОСНОВНА КОНФІГУРАЦІЯ БОТА (В КОДІ) ---
# 🌟 Усі ці значення тепер жорстко задані в коді
TELEGRAM_BOT_TOKEN = "7710517859:AAFVhcHqe5LqAc98wLhRVrAEc8lW4XhgWuw" # ВАШ ТОКЕН
WEBHOOK_BASE_URL = "https://school-voting-bot.onrender.com"  # ВАШ ОСНОВНИЙ URL RENDER
SHEET_NAME = "School_Elections"  # НАЗВА ВАШОЇ ТАБЛИЦІ GOOGLE SHEETS
KEEP_ALIVE_INTERVAL = 600  # 10 хвилин для Keep-Alive
# Render автоматично надає змінну PORT, але ми використовуємо 8080 як резерв
PORT = 8080 

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
                # Змінюємо логування для більшої інформативності
                logger.error(f"❌ Критична помилка підключення до Google Sheets. Перевірте GSPREAD_SECRET_JSON, права доступу та назву таблиці '{sheet_name}'. Деталі: {e}")
                self.is_connected = False

    async def get_worksheet(self, title: str):
        """Отримує робочий лист (вкладку) за назвою."""
        if not self.is_connected: return None
        try:
            ws = await asyncio.to_thread(self.sheet.worksheet, title)
            return ws
        except gspread.WorksheetNotFound:
            logger.error(f"❌ Критична помилка: Вкладка '{title}' не знайдена в таблиці '{self.sheet_name}'. Перевірте назви вкладок ('Codes' та 'Votes').")
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
    """
    Генерує унікальні коди на основі CLASS_CONFIG і записує їх у вкладку 'Codes'.
    УВАГА: Ця функція очищає всі існуючі записи в таблиці 'Codes' перед записом.
    """
    codes_ws = await manager.get_worksheet("Codes")
    if codes_ws is None: 
        logger.error("Генерація кодів: Не вдалося отримати вкладку 'Codes'.")
        return

    # Очистка старої таблиці (крім заголовків)
    try:
        logger.info("Генерація кодів: Очищую існуючі записи...")
        # Використовуємо методи gspread синхронно через to_thread
        await asyncio.to_thread(codes_ws.resize, rows=1, cols=7) # Зменшуємо до 1 рядка
        await asyncio.to_thread(codes_ws.resize, rows=1000) # Повертаємо багато рядків для майбутніх записів
    except Exception as e:
        logger.error(f"Генерація кодів: Не вдалося очистити стару таблицю Codes: {e}")
        # Не зупиняємося, якщо очистка не вдалася, спробуємо оновити заголовки
        pass

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
            logger.info(f"✅ Генерація кодів: Успішно згенеровано та записано {len(rows_to_insert)} унікальних кодів.")
        except Exception as e:
            logger.error(f"❌ Генерація кодів: Помилка масового запису кодів: {e}")

# --- ФУНКЦІЇ БОТА (start, receive_code, receive_contact, handle_vote, show_results, cancel) ---

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
            # Використовуємо .find() для пошуку колонок для надійності
            col_is_used = await asyncio.to_thread(codes_ws.find, "Is_Used")
            col_tg_id = await asyncio.to_thread(codes_ws.find, "Telegram_ID")
            col_phone = await asyncio.to_thread(codes_ws.find, "Phone_Number")
            col_full_name = await asyncio.to_thread(codes_ws.find, "Full_Name")
            
            if all([col_is_used, col_tg_id, col_phone, col_full_name]):
                await asyncio.to_thread(codes_ws.update_cell, row_num, col_is_used.col, 'TRUE')
                await asyncio.to_thread(codes_ws.update_cell, row_num, col_tg_id.col, user.id)
                await asyncio.to_thread(codes_ws.update_cell, row_num, col_phone.col, contact.phone_number)
                await asyncio.to_thread(codes_ws.update_cell, row_num, col_full_name.col, f"{user.full_name} (@{user.username or 'N/A'})")
            else:
                logger.error("Не знайдено одну з необхідних колонок у вкладці Codes.")
                raise Exception("Проблема з колонками Sheets.")
                
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
    # Правильно формуємо повний WEBHOOK_URL
    base_url = WEBHOOK_BASE_URL.rstrip('/')
    full_url = f"{base_url}/{TELEGRAM_BOT_TOKEN}"
    
    if full_url:
        try:
            await application.bot.set_webhook(url=full_url)
            logger.info(f"Вебхук успішно встановлено на {full_url}")
        except Exception as e:
            logger.error(f"Не вдалося встановити вебхук: {e}")

async def keep_alive_task(app: web.Application):
    """
    Задача для підтримки активності сервера (Keep-Alive).
    Використовує aiohttp.ClientSession для обходу помилки http_client.
    """
    # URL для пінг-запиту
    ping_url = f"{WEBHOOK_BASE_URL.rstrip('/')}/status"
    
    # Створюємо aiohttp.ClientSession один раз
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)
            try:
                async with session.get(ping_url) as resp:
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
    if GSPREAD_SECRET_JSON.startswith('{"type": "service_account", "placeholder": '):
        logger.error("❌ Критична помилка: Змінна GSPREAD_SECRET_JSON містить заглушку. Будь ласка, замініть її на повний JSON-ключ.")

    # Ініціалізація менеджера Google Sheets
    sheets_manager = SheetsManager(GSPREAD_SECRET_JSON, SHEET_NAME)
    
    # 🌟 АВТОМАТИЧНИЙ ЗАПУСК ГЕНЕРАЦІЇ КОДІВ (ПЕРШИЙ ЗАПУСК)
    if INITIAL_CODE_GENERATION == 'TRUE' and sheets_manager.is_connected:
        logger.warning(">>> INITIAL_CODE_GENERATION=TRUE. Виконую одноразову генерацію кодів...")
        await generate_unique_codes_to_sheets(sheets_manager, CLASS_CONFIG)
        logger.warning(">>> Одноразову генерацію кодів завершено. ВИДАЛІТЬ змінну INITIAL_CODE_GENERATION з Render, щоб уникнути повторного очищення!")

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
        # Використовуємо {TELEGRAM_BOT_TOKEN} у шляху для вебхука
        web.post(f'/{TELEGRAM_BOT_TOKEN}', handle_telegram_webhook) 
    ])
    
    # ВИПРАВЛЕННЯ: Додаємо keep-alive задачу ДО runner.setup()
    web_app.on_startup.append(lambda app: asyncio.create_task(keep_alive_task(app)))

    runner = web.AppRunner(web_app)
    await runner.setup()
    
    # ВИПРАВЛЕННЯ: Використовуємо PORT, наданий Render, за замовчуванням 8080
    # Примітка: Render встановлює змінну PORT, якщо вона існує, але ми використовуємо 8080 як резерв
    port = int(os.environ.get("PORT", PORT))
    # Біндимо до всіх інтерфейсів (0.0.0.0)
    site = web.TCPSite(runner, '0.0.0.0', port) 

    # --- Запуск ---
    await application.initialize()
    await application.start()

    # 1. Встановлюємо вебхук
    # Тут використовується оновлена логіка init_webhook, яка коректно формує повний URL
    await init_webhook(application, WEBHOOK_BASE_URL) 

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
    try:
        # Запуск бота:
        asyncio.run(main()) 

    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено вручну.")
