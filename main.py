import os
import asyncio
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
# Бібліотеки для Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials
# Бібліотеки для keep-alive та вебхуків
from aiohttp import web
import requests
import pytz
import uuid
from typing import Dict, Any, List

# --- Налаштування та Змінні Середовища ---
# Рекомендується використовувати змінні середовища для секретних ключів
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
SHEETS_CREDENTIALS_FILE = os.environ.get("SHEETS_CREDENTIALS_FILE", "sheets_creds.json") # Назва твого JSON ключа
SHEET_NAME = os.environ.get("SHEET_NAME", "School_Elections") # Назва твоєї Google Sheets
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-render-app.onrender.com/YOUR_TELEGRAM_BOT_TOKEN")

# ID адміністраторів, які можуть бачити результати (/result)
ADMIN_IDS = [
    12345678, # Заміни на свій Telegram ID
    87654321  # Додай інші ID
]

# Назви класів та кількість учнів для генерації кодів
CLASS_CONFIG = {
    "7-А": 25,
    "7-Б": 25,
    "6-Б": 28,
    "6-А": 27,
    "6-В": 26,
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

# Логування
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Робота з Google Sheets ---
class SheetsManager:
    def __init__(self, creds_file: str, sheet_name: str):
        self.creds_file = creds_file
        self.sheet_name = sheet_name
        self.client: gspread.Client = None
        self.codes_sheet: gspread.Worksheet = None
        self.votes_sheet: gspread.Worksheet = None
        self.is_connected = False
        asyncio.create_task(self._connect()) # Асинхронне підключення при ініціалізації

    async def _connect(self):
        """Встановлює з'єднання з Google Sheets асинхронно."""
        try:
            # Використовуємо to_thread, щоб не блокувати Event Loop
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = await asyncio.to_thread(ServiceAccountCredentials.from_json_keyfile_name, self.creds_file, scope)
            self.client = await asyncio.to_thread(gspread.authorize, creds)
            
            sheet = await asyncio.to_thread(self.client.open, self.sheet_name)
            self.codes_sheet = await asyncio.to_thread(sheet.worksheet, "Codes")
            self.votes_sheet = await asyncio.to_thread(sheet.worksheet, "Votes")
            self.is_connected = True
            logger.info("✅ Успішне підключення до Google Sheets.")
        except Exception as e:
            logger.error(f"❌ Помилка підключення до Google Sheets: {e}")
            self.is_connected = False

    async def _ensure_connection(self) -> bool:
        """Перевіряє і перепідключається, якщо з'єднання втрачено."""
        if not self.is_connected or not self.client:
            logger.warning("З'єднання з Google Sheets відсутнє. Спроба перепідключення...")
            await self._connect()
        return self.is_connected

    async def find_code(self, unique_code: str) -> Dict[str, Any] | None:
        """Шукає код і перевіряє його статус."""
        if not await self._ensure_connection(): return None
        try:
            # Отримання всіх кодів. Це швидше, ніж шукати по одному.
            all_records = await asyncio.to_thread(self.codes_sheet.get_all_records)
            for row in all_records:
                if row.get('Unique_Code') == unique_code:
                    return row
            return None
        except Exception as e:
            logger.error(f"Помилка пошуку коду: {e}")
            return None

    async def mark_code_used(self, unique_code: str, user_data: Dict[str, Any]):
        """Позначає код як використаний і оновлює дані учня."""
        if not await self._ensure_connection(): return False
        try:
            # Знайдемо рядок, де знаходиться код
            cell = await asyncio.to_thread(self.codes_sheet.find, unique_code, in_column=3) # Unique_Code у 3-й колонці
            if not cell: return False

            row_index = cell.row
            
            # Оновлюємо дані у відповідних колонках
            updates = [
                ('Is_Used', 'TRUE'),
                ('Telegram_ID', user_data['telegram_id']),
                ('Phone_Number', user_data['phone_number']),
                ('Full_Name', user_data['full_name'])
            ]

            # Використовуємо batch update для швидкості
            updates_list = []
            for col_name, value in updates:
                # Знаходимо індекс колонки за заголовком (припускаємо, що заголовки у першому рядку)
                headers = await asyncio.to_thread(self.codes_sheet.row_values, 1)
                try:
                    col_index = headers.index(col_name) + 1
                    updates_list.append({
                        'range': gspread.utils.rowcol_to_a1(row_index, col_index),
                        'values': [[value]]
                    })
                except ValueError:
                    logger.warning(f"Колонка '{col_name}' не знайдена у таблиці Codes.")

            if updates_list:
                await asyncio.to_thread(self.codes_sheet.batch_update, updates_list)
                logger.info(f"Код {unique_code} успішно позначено як використаний.")
                return True
            return False

        except Exception as e:
            logger.error(f"Помилка оновлення статусу коду: {e}")
            return False

    async def record_vote(self, code_data: Dict[str, Any], candidate_key: str) -> bool:
        """Записує голос у таблицю Votes."""
        if not await self._ensure_connection(): return False
        try:
            now = datetime.now(pytz.timezone('Europe/Kyiv')).strftime("%Y-%m-%d %H:%M:%S")
            vote_row = [
                now,
                code_data.get('Class', 'N/A'),
                code_data.get('Unique_Code', 'N/A'),
                code_data.get('Telegram_ID', 'N/A'),
                code_data.get('Username', 'N/A'), # Username ми додаємо з Telegram ID
                code_data.get('Full_Name', 'N/A'),
                CANDIDATES.get(candidate_key, 'N/A')
            ]
            await asyncio.to_thread(self.votes_sheet.append_row, vote_row)
            logger.info(f"Голос за {CANDIDATES.get(candidate_key)} успішно записано.")
            return True
        except Exception as e:
            logger.error(f"Помилка запису голосу: {e}")
            return False

    async def get_results(self) -> Dict[str, float] | None:
        """Розраховує результати голосування у відсотках."""
        if not await self._ensure_connection(): return None
        try:
            # Отримуємо всі голоси
            all_votes_records = await asyncio.to_thread(self.votes_sheet.get_all_records)
            
            # Кількість голосів за кожного кандидата
            vote_counts = {name: 0 for name in CANDIDATES.values()}
            total_votes = len(all_votes_records)

            for row in all_votes_records:
                candidate = row.get('Candidate_Voted')
                if candidate in vote_counts:
                    vote_counts[candidate] += 1
            
            # Розрахунок відсотків
            results = {}
            for candidate, count in vote_counts.items():
                percentage = (count / total_votes) * 100 if total_votes > 0 else 0
                results[candidate] = percentage
            
            return results

        except Exception as e:
            logger.error(f"Помилка розрахунку результатів: {e}")
            return None


# --- Генерація Унікальних Кодів (Консольна утиліта) ---
async def generate_unique_codes_to_sheets(sheets_manager: SheetsManager, class_config: Dict[str, int]):
    """Генерує унікальні коди для кожного учня і записує їх у таблицю Codes."""
    
    # Викликається вручну адміністратором
    
    if not await sheets_manager._ensure_connection():
        logger.error("Не вдалося підключитися до Sheets. Коди не згенеровано.")
        return

    all_codes_to_insert = []
    
    # Заголовки, якщо їх немає
    headers = ["Class", "Student_Count", "Unique_Code", "Is_Used", "Telegram_ID", "Phone_Number", "Full_Name"]
    
    # Очистка і вставка заголовків (ВВАЖАЙТЕ, це очистить вміст!)
    try:
        await asyncio.to_thread(sheets_manager.codes_sheet.clear)
        await asyncio.to_thread(sheets_manager.codes_sheet.append_row, headers)
        logger.info("Таблицю Codes очищено та додано заголовки.")
    except Exception as e:
        logger.error(f"Помилка очищення/додавання заголовків: {e}")
        return

    for class_name, count in class_config.items():
        for _ in range(count):
            # Генеруємо унікальний код (наприклад, UUID4 без дефісів)
            unique_code = uuid.uuid4().hex.upper()[:8] 
            all_codes_to_insert.append([
                class_name,
                count,
                unique_code,
                'FALSE',
                '',
                '',
                ''
            ])

    try:
        if all_codes_to_insert:
            await asyncio.to_thread(sheets_manager.codes_sheet.append_rows, all_codes_to_insert)
            logger.info(f"✅ Успішно згенеровано та додано {len(all_codes_to_insert)} унікальних кодів!")
        else:
            logger.warning("Конфігурація класів порожня. Коди не згенеровано.")
    except Exception as e:
        logger.error(f"Помилка пакетного додавання кодів: {e}")


# --- Обробники команд та стани бота ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Надсилає стартове повідомлення та запитує унікальний код."""
    user = update.effective_user
    logger.info(f"Користувач {user.id} почав розмову.")
    
    # Перевіряємо, чи вже проголосував
    # У цьому прикладі ми це зробимо в наступному кроці для спрощення логіки ConversationHandler
    
    await update.message.reply_text(
        f"🗳️ Вітаємо, {user.first_name}! Це система голосування за президента школи.\n\n"
        "Для початку голосування, будь ласка, **введіть ваш унікальний код**, який ви отримали у класного керівника. /cancel для скасування."
    )
    return WAITING_FOR_CODE

async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отримує унікальний код і перевіряє його в базі."""
    unique_code = update.message.text.strip().upper()
    user = update.effective_user
    
    if len(unique_code) != 8 or not unique_code.isalnum():
        await update.message.reply_text("❌ Некоректний формат коду. Будь ласка, введіть **8-значний** унікальний код (букви та цифри).")
        return WAITING_FOR_CODE

    sheets_manager: SheetsManager = context.bot_data['sheets_manager']
    code_data = await sheets_manager.find_code(unique_code)
    
    if not code_data:
        await update.message.reply_text("❌ Цей код **не знайдено** в базі. Перевірте правильність введення або зверніться до класного керівника.")
        return WAITING_FOR_CODE
    
    if code_data.get('Is_Used') == 'TRUE':
        await update.message.reply_text("❌ Цей код **вже був використаний** для голосування. Ви можете проголосувати лише один раз.")
        return ConversationHandler.END

    context.user_data['unique_code'] = unique_code
    context.user_data['code_data'] = code_data

    # Запитуємо номер телефону для фіксації в базі
    keyboard = [[KeyboardButton("Надіслати мій номер телефону 📲", request_contact=True)]]
    await update.message.reply_text(
        f"✅ Код прийнято! Ви представляєте клас **{code_data.get('Class', 'N/A')}**.\n\n"
        "Для остаточної ідентифікації та запису в базу, будь ласка, **натисніть кнопку** і поділіться вашим номером телефону. /cancel для скасування.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return WAITING_FOR_CONTACT

async def receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отримує контакт, зберігає його і переходить до голосування."""
    if not update.message.contact or update.message.contact.user_id != update.effective_user.id:
        await update.message.reply_text("❌ Будь ласка, **натисніть на кнопку** 'Надіслати мій номер телефону' для підтвердження контакту.")
        return WAITING_FOR_CONTACT
        
    contact = update.message.contact
    user = update.effective_user
    unique_code = context.user_data['unique_code']
    code_data = context.user_data['code_data']
    
    # Збираємо всі дані для бази
    user_data_to_store = {
        'telegram_id': str(user.id),
        'phone_number': contact.phone_number,
        'full_name': f"{contact.first_name} {contact.last_name or ''}".strip(),
        'username': user.username or 'N/A'
    }
    
    # Оновлюємо код унікальності в об'єкті user_data, який буде використано для запису голосу
    code_data['Telegram_ID'] = user_data_to_store['telegram_id']
    code_data['Phone_Number'] = user_data_to_store['phone_number']
    code_data['Full_Name'] = user_data_to_store['full_name']
    code_data['Username'] = user_data_to_store['username']
    
    sheets_manager: SheetsManager = context.bot_data['sheets_manager']
    
    # Позначаємо код як використаний у таблиці Codes
    success = await sheets_manager.mark_code_used(unique_code, user_data_to_store)

    if not success:
        await update.message.reply_text("❌ Виникла помилка при реєстрації вашого коду в базі. Спробуйте пізніше або зверніться до адміністратора.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    # Формуємо клавіатуру для голосування
    keyboard = []
    for key, name in CANDIDATES.items():
        keyboard.append([InlineKeyboardButton(name, callback_data=f"vote_{key}")])
        
    await update.message.reply_text(
        "✅ Успішна ідентифікація. Тепер оберіть свого кандидата:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_FOR_VOTE

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє вибір кандидата та фіксує голос."""
    query = update.callback_query
    await query.answer()
    
    # Перевірка, що це справді колбек голосування
    if not query.data.startswith("vote_"):
        await query.edit_message_text("❌ Невідома дія. Спробуйте ще раз, натиснувши кнопку з кандидатом.")
        return WAITING_FOR_VOTE

    candidate_key = query.data.split("_")[1]
    candidate_name = CANDIDATES.get(candidate_key)
    
    if not candidate_name:
        await query.edit_message_text("❌ Некоректний кандидат. Спробуйте ще раз.")
        return WAITING_FOR_VOTE

    # Отримання даних, збережених на попередньому кроці
    code_data = context.user_data.get('code_data')
    if not code_data:
        await query.edit_message_text("❌ Помилка: дані про ваш код втрачено. Почніть знову з /start.")
        return ConversationHandler.END

    sheets_manager: SheetsManager = context.bot_data['sheets_manager']
    
    # Записуємо голос у таблицю Votes
    vote_recorded = await sheets_manager.record_vote(code_data, candidate_key)
    
    if vote_recorded:
        await query.edit_message_text(
            f"🎉 **Ваш голос зараховано!**\n\n"
            f"Ви проголосували за **{candidate_name}**.\n\n"
            f"Дякуємо за участь у виборах!",
            reply_markup=None,
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            "❌ Виникла помилка при записі вашого голосу. Спробуйте пізніше або зверніться до адміністратора."
        )

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє скасування розмови."""
    await update.effective_message.reply_text(
        'Операцію скасовано. Для початку голосування знову введіть /start.',
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def show_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда адміністратора для виведення результатів."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Ця команда доступна лише адміністраторам.")
        return

    sheets_manager: SheetsManager = context.bot_data['sheets_manager']
    
    if not sheets_manager.is_connected:
        await update.message.reply_text("❌ Не вдалося підключитися до Google Sheets. Перевірте лог.")
        return

    await update.message.reply_text("📊 *Обчислюю результати...*", parse_mode='Markdown')

    results = await sheets_manager.get_results()
    
    if results is None:
        await update.message.reply_text("❌ Помилка при отриманні результатів. Перевірте структуру таблиць.")
        return

    total_votes = sum(results.values()) / 100 if results else 0
    total_codes = sum(CLASS_CONFIG.values())
    
    # Форматування виведення
    result_text = "📈 **Результати Виборів Президента Школи** 📈\n\n"
    
    if results:
        sorted_results = dict(sorted(results.items(), key=lambda item: item[1], reverse=True))
        
        for candidate, percentage in sorted_results.items():
            result_text += f"**{candidate}**: `{percentage:.2f}%`\n"
            
    result_text += (
        f"\n---\n"
        f"**Всього голосів (підтверджених):** `{int(total_votes)}`\n"
        f"**Всього потенційних виборців:** `{total_codes}`"
    )

    await update.message.reply_text(result_text, parse_mode='Markdown')

# --- Keep-Alive та Вебхук ---
async def keep_alive_task(app: web.Application):
    """Задача для підтримки активності бота (keep-alive) на Render. Виконується кожні 10 хвилин."""
    while True:
        try:
            # Звертаємося до себе, щоб запобігти 'засинанню'
            await asyncio.to_thread(requests.get, WEBHOOK_URL.rsplit('/', 1)[0] + '/status', timeout=5) 
            logger.debug("Keep-alive request sent.")
        except Exception as e:
            logger.warning(f"Keep-alive failed: {e}")
        await asyncio.sleep(600) # Виконуємо кожні 10 хвилин (600 секунд)

async def status_handler(request: web.Request) -> web.Response:
    """Простий обробник для keep-alive запитів."""
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

async def init_webhook(application: Application, webhook_url: str):
    """Встановлює вебхук."""
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    logger.info(f"Вебхук успішно встановлено на {webhook_url}")

async def main() -> None:
    # Перевірка наявності файлу з ключем
    if not os.path.exists(SHEETS_CREDENTIALS_FILE):
        logger.error(f"Файл {SHEETS_CREDENTIALS_FILE} не знайдено. Голосування не працюватиме.")
        # Залишаємо заглушку для запуску бота, але функціонал Sheets буде недоступний
        with open(SHEETS_CREDENTIALS_FILE, 'w') as f:
            f.write('{"placeholder": "replace with actual service account json"}')

    # Ініціалізація менеджера Google Sheets
    sheets_manager = SheetsManager(SHEETS_CREDENTIALS_FILE, SHEET_NAME)

    # --- Створення та налаштування Application ---
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data['sheets_manager'] = sheets_manager
    
    # --- Обробник розмови для голосування ---
    voting_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
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
    
    # 3. Додаємо keep-alive задачу
    web_app.on_startup.append(lambda app: asyncio.create_task(keep_alive_task(app)))

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
        # Для першого запуску, щоб згенерувати коди, розкоментуйте цей блок та запустіть окремо
        # import gspread
        # import asyncio
        # async def initial_setup():
        #     manager = SheetsManager(SHEETS_CREDENTIALS_FILE, SHEET_NAME)
        #     # Даємо час на підключення
        #     await asyncio.sleep(5) 
        #     await generate_unique_codes_to_sheets(manager, CLASS_CONFIG)
        # 
        # asyncio.run(initial_setup()) 
        
        # Після генерації кодів запускайте основний main()
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено вручну.")
