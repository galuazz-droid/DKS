import logging
import os
import sys
from datetime import date
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Для работы с PostgreSQL
import psycopg2
from psycopg2.extras import RealDictCursor

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Состояния
CHOOSING, TYPING_REPLY = range(2)

# Предустановленные статусы
PRESET_STATUSES = ["✅ На работе", "🏠 Дома", "🌴 В отпуске", "🤒 Болею", "✈️ В командировке"]

# Подключение к БД
def get_db_connection():
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        raise ValueError("Переменная окружения DATABASE_URL не задана!")
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# Инициализация БД
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            chat_id BIGINT,
            is_active BOOLEAN DEFAULT TRUE
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS statuses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            chat_id BIGINT,
            status_text TEXT,
            date DATE DEFAULT CURRENT_DATE
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

# Функции работы с БД
def add_user(user_id, username, chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO users (user_id, username, chat_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
    ''', (user_id, username, chat_id))
    conn.commit()
    cur.close()
    conn.close()

def set_user_active(user_id, active=True):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE users SET is_active = %s WHERE user_id = %s', (active, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_active_users(chat_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT user_id, username FROM users WHERE chat_id = %s AND is_active = TRUE', (chat_id,))
    result = cur.fetchall()
    cur.close()
    conn.close()
    return [(row['user_id'], row['username']) for row in result]

def save_status(user_id, chat_id, status_text):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO statuses (user_id, chat_id, status_text)
        VALUES (%s, %s, %s)
    ''', (user_id, chat_id, status_text))
    conn.commit()
    cur.close()
    conn.close()

def get_today_statuses(chat_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('''
        SELECT u.username, s.status_text
        FROM statuses s
        JOIN users u ON s.user_id = u.user_id
        WHERE s.chat_id = %s AND s.date = CURRENT_DATE
    ''', (chat_id,))
    result = cur.fetchall()
    cur.close()
    conn.close()
    return [(row['username'], row['status_text']) for row in result]

# Обработчики
async def test_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        await update.message.reply_text("✅ Подключение к БД успешно!")
        cur.close()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка подключения: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    add_user(user.id, user.username or user.first_name, chat_id)
    await update.message.reply_text(
        f"Привет, {user.first_name}! Ты добавлен в список опроса.\n"
        "Каждый день в 9:00 я буду спрашивать твой статус.\n"
        "Используй /status чтобы посмотреть статусы участников сегодня.\n"
        "Используй /setstatus чтобы обновить статус в любое время."
    )

async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT is_active FROM users WHERE user_id = %s', (user.id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        new_status = not row[0]
        set_user_active(user.id, new_status)
        status_text = "включён" if new_status else "отключён"
        await update.message.reply_text(f"Твой статус опроса {status_text}.")
    else:
        await update.message.reply_text("Сначала отправь /start.")

async def set_status_manually(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setstatus — внеплановая установка статуса."""
    keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Выбери или напиши свой статус:", reply_markup=reply_markup)
    return CHOOSING

async def status_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "✏️ Написать свой":
        await update.message.reply_text("Напиши свой статус:", reply_markup=ReplyKeyboardRemove())
        return TYPING_REPLY
    if text in PRESET_STATUSES:
        save_status(update.effective_user.id, update.effective_chat.id, text)
        await update.message.reply_text("Статус сохранён! ✅", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Пожалуйста, выбери статус из кнопок:", reply_markup=reply_markup)
    return CHOOSING

async def custom_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_status(update.effective_user.id, update.effective_chat.id, update.message.text)
    await update.message.reply_text("Твой статус сохранён! ✅")
    return ConversationHandler.END

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    statuses = get_today_statuses(update.effective_chat.id)
    if not statuses:
        await update.message.reply_text("Сегодня никто ещё не отправил статус.")
    else:
        msg = "📊 Статусы участников сегодня:\n\n"
        for username, status in statuses:
            msg += f"👤 {username}: {status}\n"
        await update.message.reply_text(msg)

async def ask_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    users = get_active_users(chat_id)
    for user_id, username in users:
        try:
            keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            await context.bot.send_message(chat_id=user_id, text="Как твой статус сегодня?", reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

async def send_daily_poll_to_all_chats(application: Application):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT chat_id FROM users WHERE is_active = TRUE')
    chat_ids = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    for chat_id in chat_ids:
        await ask_status(application, chat_id)

async def post_init(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))
    scheduler.add_job(send_daily_poll_to_all_chats, 'cron', hour=9, minute=0, args=[application])
    scheduler.start()
    logger.info("Планировщик запущен: ежедневный опрос в 9:00")

def main():
    init_db()
    TOKEN = os.environ.get('TELEGRAM_TOKEN')
    if not TOKEN:
        raise ValueError("Переменная окружения TELEGRAM_TOKEN не задана!")

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, status_chosen),
            CommandHandler("setstatus", set_status_manually),
        ],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_chosen)],
            TYPING_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_status)],
        },
        fallbacks=[],
        per_chat=False,
        per_user=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("toggle", toggle))
    application.add_handler(CommandHandler("status", show_status))
    application.add_handler(CommandHandler("testdb", test_db))
    application.add_handler(conv_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
