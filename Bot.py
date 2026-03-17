import os
import re
import json
import logging
import base64
import pathlib
from datetime import datetime, date

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter

# ── LOGGING ──────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── FIREBASE INIT ─────────────────────────────────────────
# На Railway передаём сервисный аккаунт через переменную окружения
# FIREBASE_CREDENTIALS = base64 от содержимого serviceAccount.json
_raw = os.environ.get('FIREBASE_CREDENTIALS', '')
if _raw:
    _decoded = base64.b64decode(_raw).decode('utf-8')
    _cred_dict = json.loads(_decoded)
    cred = credentials.Certificate(_cred_dict)
else:
    # Локальная разработка — файл рядом с bot.py
    cred = credentials.Certificate('serviceAccount.json')

firebase_admin.initialize_app(cred)
db = firestore.client()

BOT_TOKEN = os.environ['BOT_TOKEN']

# ── USER STORAGE ──────────────────────────────────────────
# Хранит telegram_id → firebase_uid
USERS_FILE = 'users.json'

def load_users() -> dict:
    p = pathlib.Path(USERS_FILE)
    if p.exists():
        return json.loads(p.read_text(encoding='utf-8'))
    return {}

def save_users(users: dict):
    pathlib.Path(USERS_FILE).write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8'
    )

# ── CATEGORY DETECTION ───────────────────────────────────
EXP_KEYWORDS = {
    'Еда':          {'еда','кофе','ресторан','кафе','обед','ужин','завтрак',
                     'продукты','супермаркет','пицца','суши','бургер','фаст'},
    'Транспорт':    {'такси','метро','автобус','транспорт','бензин','парковка',
                     'uber','яндекс','маршрутка','поезд','билет'},
    'Жильё':        {'аренда','квартира','жильё','коммунальные','ком','свет',
                     'вода','газ','квплата'},
    'Здоровье':     {'аптека','врач','здоровье','лекарство','клиника','больница',
                     'таблетки','анализы'},
    'Развлечения':  {'кино','театр','развлечения','концерт','игры','подписка',
                     'netflix','spotify','книга','спорт'},
    'Одежда':       {'одежда','обувь','шопинг','покупки'},
    'Связь':        {'телефон','связь','интернет','мтс','билайн','мегафон','симка'},
    'Кредит':       {'кредит','ипотека','займ','долг','рассрочка'},
}
EXP_EMOJIS = {
    'Еда':'🍕','Транспорт':'🚗','Жильё':'🏠','Здоровье':'💊',
    'Развлечения':'🎬','Одежда':'👗','Связь':'📱','Кредит':'🏦','Другое':'💡',
}
INC_KEYWORDS = {
    'Зарплата':   {'зарплата','salary','оклад'},
    'Фриланс':    {'фриланс','проект','заказ','работа'},
    'Подарок':    {'подарок','gift'},
    'Инвестиции': {'инвестиции','дивиденды','акции','вклад','процент'},
}
INC_EMOJIS = {
    'Зарплата':'💼','Фриланс':'💻','Подарок':'🎁','Инвестиции':'📈','Другое':'💡',
}

def detect_category(desc: str, tx_type: str) -> tuple[str, str]:
    dl = desc.lower()
    if tx_type == 'expense':
        for cat, kws in EXP_KEYWORDS.items():
            if any(kw in dl for kw in kws):
                return cat, EXP_EMOJIS[cat]
        return 'Другое', '💡'
    else:
        for cat, kws in INC_KEYWORDS.items():
            if any(kw in dl for kw in kws):
                return cat, INC_EMOJIS[cat]
        return 'Другое', '💡'

# ── HELPERS ───────────────────────────────────────────────
def fmt(amount: float) -> str:
    """1500.5 → '1 500,50 ⃀'"""
    s = f"{amount:,.2f}".replace(',', ' ').replace('.', ',')
    return f"{s} ⃀"

def get_uid(tg_id: str) -> str | None:
    return load_users().get(tg_id)

def require_uid(uid) -> bool:
    return uid is not None

HELP_TEXT = (
    "📝 *Команды бота:*\n\n"
    "*Добавить запись:*\n"
    "`расход 500 кофе` — расход\n"
    "`доход 50000 зарплата` — доход\n"
    "`-500 такси` или `+5000 фриланс` — быстрый ввод\n\n"
    "*Просмотр:*\n"
    "/balance — текущий баланс\n"
    "/stat — топ расходов за месяц\n"
    "/last — последние 5 записей\n\n"
    "*Управление:*\n"
    "/delete — удалить последнюю запись\n"
    "/disconnect — отвязать Firebase аккаунт\n"
    "/help — эта справка"
)

# ── HANDLERS ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)

    if uid:
        await update.message.reply_text(
            f"👋 С возвращением!\n\n{HELP_TEXT}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Я помогу управлять *личными финансами* прямо из Telegram.\n\n"
            "Данные хранятся в твоём Firebase и синхронизируются с веб-приложением в реальном времени.\n\n"
            "🔑 *Шаг 1:* Открой сайт с финансами\n"
            "🔑 *Шаг 2:* Зайди в *Меню → Мой UID* и скопируй ID\n"
            "🔑 *Шаг 3:* Отправь мне:\n"
            "`/uid ТВОЙ_UID`",
            parse_mode='Markdown'
        )

async def cmd_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)

    if not context.args:
        await update.message.reply_text(
            "Использование: `/uid ТВОЙ_FIREBASE_UID`", parse_mode='Markdown'
        )
        return

    uid = context.args[0].strip()

    # Проверяем что такой пользователь существует в Firestore
    try:
        doc = db.collection('users').document(uid).get()
        # Даже если документа нет — UID всё равно валидный (новый юзер)
        users = load_users()
        users[tg_id] = uid
        save_users(users)
        await update.message.reply_text(
            "✅ *Аккаунт подключён!*\n\n"
            "Теперь можешь добавлять записи:\n"
            "`расход 500 кофе`\n"
            "`доход 50000 зарплата`\n\n"
            "Или нажми /помощь для списка команд.",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка подключения: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)
    if not uid:
        await update.message.reply_text("⚠️ Сначала подключи аккаунт: /start")
        return

    try:
        txs = db.collection('users').document(uid).collection('transactions').stream()
        total_inc = total_exp = 0.0
        for t in txs:
            d = t.to_dict()
            if d.get('type') == 'income':
                total_inc += d.get('amount', 0)
            else:
                total_exp += d.get('amount', 0)

        bal = total_inc - total_exp
        sign = '📈' if bal >= 0 else '📉'

        await update.message.reply_text(
            f"💳 *Баланс*\n\n"
            f"{sign} *{fmt(bal)}*\n\n"
            f"↑ Доходы:  `{fmt(total_inc)}`\n"
            f"↓ Расходы: `{fmt(total_exp)}`",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)
    if not uid:
        await update.message.reply_text("⚠️ Сначала подключи аккаунт: /start")
        return

    month = datetime.now().strftime('%Y-%m')
    try:
        txs = db.collection('users').document(uid).collection('transactions').stream()
        cat_map: dict[str, dict] = {}
        total_inc_m = 0.0

        for t in txs:
            d = t.to_dict()
            if not d.get('date', '').startswith(month):
                continue
            if d.get('type') == 'income':
                total_inc_m += d.get('amount', 0)
            else:
                cat = d.get('category', 'Другое')
                emoji = d.get('emoji', '💡')
                amt = d.get('amount', 0)
                if cat not in cat_map:
                    cat_map[cat] = {'emoji': emoji, 'total': 0.0}
                cat_map[cat]['total'] += amt

        if not cat_map:
            await update.message.reply_text(
                f"📊 В *{month}* расходов пока нет.", parse_mode='Markdown'
            )
            return

        sorted_cats = sorted(cat_map.items(), key=lambda x: x[1]['total'], reverse=True)[:5]
        total_exp_m = sum(v['total'] for _, v in sorted_cats)

        lines = [f"📊 *Расходы за {month}*\n"]
        for i, (cat, data) in enumerate(sorted_cats, 1):
            pct = round(data['total'] / total_exp_m * 100) if total_exp_m else 0
            bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
            lines.append(f"{i}. {data['emoji']} *{cat}*\n    `{bar}` {pct}%  —  `{fmt(data['total'])}`")

        lines.append(f"\n💸 Расходы: `{fmt(total_exp_m)}`")
        lines.append(f"💰 Доходы:  `{fmt(total_inc_m)}`")

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)
    if not uid:
        await update.message.reply_text("⚠️ Сначала подключи аккаунт: /start")
        return

    try:
        docs = (
            db.collection('users').document(uid).collection('transactions')
            .order_by('ts', direction=firestore.Query.DESCENDING)
            .limit(5)
            .stream()
        )
        items = list(docs)
        if not items:
            await update.message.reply_text("📭 Записей пока нет.")
            return

        lines = ["📋 *Последние записи:*\n"]
        for t in items:
            d = t.to_dict()
            sign = '↑' if d.get('type') == 'income' else '↓'
            emoji = d.get('emoji', '💡')
            amt = fmt(d.get('amount', 0))
            desc = d.get('desc', '')
            dt = d.get('date', '')
            lines.append(f"{sign} {emoji} `{amt}` — {desc} _{dt}_")

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)
    if not uid:
        await update.message.reply_text("⚠️ Сначала подключи аккаунт: /start")
        return

    try:
        docs = list(
            db.collection('users').document(uid).collection('transactions')
            .order_by('ts', direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not docs:
            await update.message.reply_text("📭 Нет записей для удаления.")
            return

        doc = docs[0]
        d = doc.to_dict()
        doc.reference.delete()

        type_word = 'Доход' if d.get('type') == 'income' else 'Расход'
        emoji = d.get('emoji', '💡')
        await update.message.reply_text(
            f"🗑 *{type_word} удалён*\n"
            f"{emoji} {d.get('category','')} — `{fmt(d.get('amount',0))}`\n"
            f"📝 {d.get('desc','')}\n"
            f"📅 {d.get('date','')}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    if tg_id in users:
        del users[tg_id]
        save_users(users)
    await update.message.reply_text(
        "✅ Аккаунт отвязан.\nИспользуй /start чтобы подключить снова."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем текстовые сообщения — добавление транзакций."""
    tg_id = str(update.effective_user.id)
    uid = get_uid(tg_id)

    if not uid:
        await update.message.reply_text(
            "⚠️ Аккаунт не подключён. Отправь /start для настройки."
        )
        return

    text = update.message.text.strip()

    # Паттерны парсинга
    patterns = [
        # "расход 500 кофе" / "р 1500 такси"
        (r'^(?:расход|расх|р|expense)\s+([\d\s.,]+)\s*(.*)', 'expense'),
        # "доход 50000 зарплата" / "д 5000"
        (r'^(?:доход|дох|д|income)\s+([\d\s.,]+)\s*(.*)', 'income'),
        # "-500 кофе"
        (r'^-([\d\s.,]+)\s*(.*)', 'expense'),
        # "+50000 зарплата"
        (r'^\+([\d\s.,]+)\s*(.*)', 'income'),
    ]

    tx_type = None
    amount = None
    desc = ''

    for pattern, ptype in patterns:
        m = re.match(pattern, text, re.IGNORECASE)
        if m:
            raw_amt = m.group(1).replace(' ', '').replace(',', '.')
            try:
                amount = float(raw_amt)
            except ValueError:
                continue
            desc = m.group(2).strip() if m.lastindex >= 2 else ''
            tx_type = ptype
            break

    if tx_type is None:
        # Не похоже на транзакцию — игнорируем
        return

    if amount is None or amount <= 0:
        await update.message.reply_text("⚠️ Некорректная сумма.")
        return

    category, emoji = detect_category(desc, tx_type)
    if not desc:
        desc = category

    today = date.today().isoformat()

    try:
        db.collection('users').document(uid).collection('transactions').add({
            'type': tx_type,
            'amount': amount,
            'category': category,
            'emoji': emoji,
            'desc': desc,
            'date': today,
            'ts': SERVER_TIMESTAMP,
        })

        sign = '↑' if tx_type == 'income' else '↓'
        type_word = 'Доход' if tx_type == 'income' else 'Расход'
        await update.message.reply_text(
            f"✅ *{type_word} добавлен*\n\n"
            f"{sign} {emoji} *{category}*\n"
            f"💰 `{fmt(amount)}`\n"
            f"📝 {desc}\n"
            f"📅 {today}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка сохранения: {e}")


# ── MAIN ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start',      cmd_start))
    app.add_handler(CommandHandler('uid',        cmd_uid))
    app.add_handler(CommandHandler('help',       cmd_help))
    app.add_handler(CommandHandler('balance',    cmd_balance))
    app.add_handler(CommandHandler('stat',       cmd_stats))
    app.add_handler(CommandHandler('last',       cmd_last))
    app.add_handler(CommandHandler('delete',     cmd_delete))
    app.add_handler(CommandHandler('disconnect', cmd_disconnect))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
