import os
import re
import json
import logging
import base64
import pathlib
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

# ── LOGGING ───────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── FIREBASE ──────────────────────────────────────────────
_raw = os.environ.get('FIREBASE_CREDENTIALS', '')
if _raw:
    cred = credentials.Certificate(json.loads(base64.b64decode(_raw).decode()))
else:
    cred = credentials.Certificate('serviceAccount.json')

firebase_admin.initialize_app(cred)
db = firestore.client()

BOT_TOKEN = os.environ['BOT_TOKEN']

# ── USER STORAGE ──────────────────────────────────────────
USERS_FILE = 'users.json'

def load_users() -> dict:
    p = pathlib.Path(USERS_FILE)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}

def save_users(users: dict):
    pathlib.Path(USERS_FILE).write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8'
    )

def get_uid(tg_id: str) -> str | None:
    return load_users().get(tg_id)

# ── CONVERSATION STATES ───────────────────────────────────
ENTER_AMOUNT, CHOOSE_CATEGORY, ENTER_CUSTOM_CAT = range(3)

# ── CATEGORIES ────────────────────────────────────────────
EXPENSE_CATS = [
    ('🍕 Еда',         'Еда',         '🍕'),
    ('🚗 Транспорт',   'Транспорт',   '🚗'),
    ('🏠 Жильё',       'Жильё',       '🏠'),
    ('💊 Здоровье',    'Здоровье',    '💊'),
    ('🎬 Развлечения', 'Развлечения', '🎬'),
    ('👗 Одежда',      'Одежда',      '👗'),
    ('📱 Связь',       'Связь',       '📱'),
    ('🏦 Кредит',      'Кредит',      '🏦'),
    ('💡 Другое',      'Другое',      '💡'),
]
INCOME_CATS = [
    ('💼 Зарплата',    'Зарплата',    '💼'),
    ('💻 Фриланс',     'Фриланс',     '💻'),
    ('🎁 Подарок',     'Подарок',     '🎁'),
    ('📈 Инвестиции',  'Инвестиции',  '📈'),
    ('💡 Другое',      'Другое',      '💡'),
]

def fmt(amount: float) -> str:
    s = f"{amount:,.2f}".replace(',', ' ').replace('.', ',')
    return f"{s} ⃀"

def cat_keyboard(tx_type: str) -> InlineKeyboardMarkup:
    cats = EXPENSE_CATS if tx_type == 'expense' else INCOME_CATS
    rows, row = [], []
    for label, name, _ in cats:
        row.append(InlineKeyboardButton(label, callback_data=f"cat:{name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Своя категория", callback_data="cat:__custom__")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📉 Расход", callback_data="type:expense"),
            InlineKeyboardButton("📈 Доход",  callback_data="type:income"),
        ],
        [
            InlineKeyboardButton("💳 Баланс",     callback_data="menu:balance"),
            InlineKeyboardButton("📊 Статистика", callback_data="menu:stat"),
        ],
        [
            InlineKeyboardButton("🕓 Последние",     callback_data="menu:last"),
            InlineKeyboardButton("🗑 Удалить посл.", callback_data="menu:delete"),
        ],
    ])

# ── /start ────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    name  = update.effective_user.first_name or "друг"

    if not get_uid(tg_id):
        await update.message.reply_text(
            f"👋 Привет, *{name}*!\n\n"
            "Я помогу управлять *личными финансами* прямо из Telegram — "
            "записи мгновенно синхронизируются с твоим веб-приложением.\n\n"
            "🔑 Чтобы начать:\n"
            "1. Открой сайт → *Меню → Настройки*\n"
            "2. Скопируй свой *Firebase UID*\n"
            "3. Отправь мне: `/uid ТВОЙ_UID`",
            parse_mode='Markdown'
        )
        return

    await update.message.reply_text(
        f"👋 Привет, *{name}*! Что делаем?",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# ── /uid ──────────────────────────────────────────────────
async def cmd_uid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Использование: `/uid ТВОЙ_FIREBASE_UID`", parse_mode='Markdown')
        return
    uid = context.args[0].strip()
    users = load_users()
    users[tg_id] = uid
    save_users(users)
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"✅ *Аккаунт подключён!* Привет, *{name}*!\n\nЧто делаем?",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not get_uid(tg_id):
        await update.message.reply_text("⚠️ Сначала подключи аккаунт: /start")
        return
    await update.message.reply_text("Что делаем?", reply_markup=main_keyboard())

# ── CONVERSATION: выбор типа (entry point) ────────────────
async def cb_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = str(query.from_user.id)
    if not get_uid(tg_id):
        await query.edit_message_text("⚠️ Аккаунт не подключён. /start")
        return ConversationHandler.END

    tx_type = query.data.split(':')[1]
    context.user_data['tx_type'] = tx_type
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"

    await query.edit_message_text(
        f"*{type_label}*\n\n💰 Введи сумму:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="cancel")
        ]])
    )
    return ENTER_AMOUNT

# ── CONVERSATION: ввод суммы ──────────────────────────────
async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(',', '.')
    try:
        amount = float(text)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи корректную сумму, например: `1500` или `299.90`",
            parse_mode='Markdown'
        )
        return ENTER_AMOUNT

    context.user_data['amount'] = amount
    tx_type    = context.user_data['tx_type']
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"

    await update.message.reply_text(
        f"*{type_label}* · `{fmt(amount)}`\n\n📂 Выбери категорию:",
        parse_mode='Markdown',
        reply_markup=cat_keyboard(tx_type)
    )
    return CHOOSE_CATEGORY

# ── CONVERSATION: выбор категории ─────────────────────────
async def cb_choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat_name = query.data.split(':', 1)[1]

    if cat_name == '__custom__':
        await query.edit_message_text(
            "✏️ Напиши название своей категории:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]])
        )
        return ENTER_CUSTOM_CAT

    tx_type = context.user_data['tx_type']
    cats    = EXPENSE_CATS if tx_type == 'expense' else INCOME_CATS
    emoji   = next((e for _, n, e in cats if n == cat_name), '💡')
    context.user_data['category'] = cat_name
    context.user_data['emoji']    = emoji

    await _show_confirm(query, context)
    return CHOOSE_CATEGORY

# ── CONVERSATION: своя категория ──────────────────────────
async def enter_custom_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_name = update.message.text.strip()
    if not cat_name:
        await update.message.reply_text("⚠️ Введи название категории:")
        return ENTER_CUSTOM_CAT

    context.user_data['category'] = cat_name
    context.user_data['emoji']    = '💡'

    tx_type    = context.user_data['tx_type']
    amount     = context.user_data['amount']
    today      = date.today().strftime('%d.%m.%Y')
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"

    await update.message.reply_text(
        f"*{type_label}*\n\n"
        f"💡 {cat_name}\n"
        f"💰 `{fmt(amount)}`\n"
        f"📅 {today}\n\n"
        f"Всё верно?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Добавить запись", callback_data="confirm")],
            [InlineKeyboardButton("❌ Отмена",          callback_data="cancel")],
        ])
    )
    return CHOOSE_CATEGORY

async def _show_confirm(query, context: ContextTypes.DEFAULT_TYPE):
    tx_type    = context.user_data['tx_type']
    amount     = context.user_data['amount']
    category   = context.user_data['category']
    emoji      = context.user_data['emoji']
    today      = date.today().strftime('%d.%m.%Y')
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"

    await query.edit_message_text(
        f"*{type_label}*\n\n"
        f"{emoji} {category}\n"
        f"💰 `{fmt(amount)}`\n"
        f"📅 {today}\n\n"
        f"Всё верно?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Добавить запись", callback_data="confirm")],
            [InlineKeyboardButton("◀️ Назад",           callback_data=f"back_to_cat")],
            [InlineKeyboardButton("❌ Отмена",           callback_data="cancel")],
        ])
    )

# ── CONVERSATION: назад к категориям ─────────────────────
async def cb_back_to_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tx_type    = context.user_data.get('tx_type', 'expense')
    amount     = context.user_data.get('amount', 0)
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"
    await query.edit_message_text(
        f"*{type_label}* · `{fmt(amount)}`\n\n📂 Выбери категорию:",
        parse_mode='Markdown',
        reply_markup=cat_keyboard(tx_type)
    )
    return CHOOSE_CATEGORY

# ── CONVERSATION: подтверждение ───────────────────────────
async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = str(query.from_user.id)
    uid   = get_uid(tg_id)
    if not uid:
        await query.edit_message_text("⚠️ Аккаунт не подключён.")
        return ConversationHandler.END

    tx_type  = context.user_data['tx_type']
    amount   = context.user_data['amount']
    category = context.user_data['category']
    emoji    = context.user_data['emoji']
    today    = date.today().isoformat()
    type_label = "📉 Расход" if tx_type == 'expense' else "📈 Доход"

    try:
        db.collection('users').document(uid).collection('transactions').add({
            'type': tx_type, 'amount': amount, 'category': category,
            'emoji': emoji, 'desc': category, 'date': today,
            'ts': SERVER_TIMESTAMP,
        })
        await query.edit_message_text(
            f"✅ *{type_label} добавлен!*\n\n"
            f"{emoji} {category} · `{fmt(amount)}`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Ещё запись", callback_data="new_entry"),
                InlineKeyboardButton("🏠 Меню",       callback_data="menu:main"),
            ]])
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ Ошибка: {e}")

    context.user_data.clear()
    return ConversationHandler.END

# ── CONVERSATION: отмена ──────────────────────────────────
async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "Отменено.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
        ]])
    )
    return ConversationHandler.END

# ── МЕНЮ-КОЛБЭКИ ─────────────────────────────────────────
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    action = query.data.split(':')[1]
    tg_id  = str(query.from_user.id)
    uid    = get_uid(tg_id)
    back   = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="menu:main")]]

    if action == 'main':
        await query.edit_message_text("Что делаем?", reply_markup=main_keyboard())
        return

    if not uid:
        await query.edit_message_text("⚠️ Аккаунт не подключён. /start")
        return

    if action == 'balance':
        inc = exp = 0.0
        for t in db.collection('users').document(uid).collection('transactions').stream():
            d = t.to_dict()
            if d.get('type') == 'income': inc += d.get('amount', 0)
            else: exp += d.get('amount', 0)
        bal  = inc - exp
        sign = '📈' if bal >= 0 else '📉'
        await query.edit_message_text(
            f"💳 *Баланс*\n\n{sign} *{fmt(bal)}*\n\n↑ Доходы:  `{fmt(inc)}`\n↓ Расходы: `{fmt(exp)}`",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(back)
        )

    elif action == 'stat':
        from datetime import datetime
        month = datetime.now().strftime('%Y-%m')
        cats, inc_m = {}, 0.0
        for t in db.collection('users').document(uid).collection('transactions').stream():
            d = t.to_dict()
            if not d.get('date','').startswith(month): continue
            if d.get('type') == 'income': inc_m += d.get('amount', 0)
            else:
                cat = d.get('category','Другое')
                cats.setdefault(cat, {'emoji': d.get('emoji','💡'), 'total': 0.0})
                cats[cat]['total'] += d.get('amount', 0)

        if not cats:
            await query.edit_message_text(f"📊 В *{month}* расходов пока нет.",
                parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(back)); return

        top      = sorted(cats.items(), key=lambda x: x[1]['total'], reverse=True)[:5]
        total_e  = sum(v['total'] for _, v in top)
        lines    = [f"📊 *Расходы за {month}*\n"]
        for i, (cat, data) in enumerate(top, 1):
            pct = round(data['total'] / total_e * 100) if total_e else 0
            bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
            lines.append(f"{i}. {data['emoji']} *{cat}*\n    `{bar}` {pct}%  —  `{fmt(data['total'])}`")
        lines += [f"\n💸 Расходы: `{fmt(total_e)}`", f"💰 Доходы:  `{fmt(inc_m)}`"]
        await query.edit_message_text('\n'.join(lines), parse_mode='Markdown',
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == 'last':
        docs = list(
            db.collection('users').document(uid).collection('transactions')
            .order_by('ts', direction=firestore.Query.DESCENDING).limit(5).stream()
        )
        if not docs:
            await query.edit_message_text("📭 Записей пока нет.",
                reply_markup=InlineKeyboardMarkup(back)); return
        lines = ["📋 *Последние записи:*\n"]
        for t in docs:
            d = t.to_dict()
            sign = '↑' if d.get('type') == 'income' else '↓'
            lines.append(f"{sign} {d.get('emoji','💡')} `{fmt(d.get('amount',0))}` — {d.get('desc','')} _{d.get('date','')}_")
        await query.edit_message_text('\n'.join(lines), parse_mode='Markdown',
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == 'delete':
        docs = list(
            db.collection('users').document(uid).collection('transactions')
            .order_by('ts', direction=firestore.Query.DESCENDING).limit(1).stream()
        )
        if not docs:
            await query.edit_message_text("📭 Нет записей.",
                reply_markup=InlineKeyboardMarkup(back)); return
        doc = docs[0]; d = doc.to_dict(); doc.reference.delete()
        type_word = 'Доход' if d.get('type') == 'income' else 'Расход'
        await query.edit_message_text(
            f"🗑 *{type_word} удалён*\n{d.get('emoji','💡')} {d.get('category','')} — `{fmt(d.get('amount',0))}`\n📅 {d.get('date','')}",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(back)
        )

async def cb_new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Выбери тип записи:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📉 Расход", callback_data="type:expense"),
                InlineKeyboardButton("📈 Доход",  callback_data="type:income"),
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:main")],
        ])
    )

async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    if tg_id in users: del users[tg_id]; save_users(users)
    await update.message.reply_text("✅ Аккаунт отвязан. /start — подключить снова.")

# ── MAIN ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_choose_type, pattern=r'^type:')],
        states={
            ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount),
                CallbackQueryHandler(cb_cancel, pattern=r'^cancel$'),
            ],
            CHOOSE_CATEGORY: [
                CallbackQueryHandler(cb_choose_category, pattern=r'^cat:'),
                CallbackQueryHandler(cb_confirm,         pattern=r'^confirm$'),
                CallbackQueryHandler(cb_cancel,          pattern=r'^cancel$'),
                CallbackQueryHandler(cb_back_to_cat,     pattern=r'^back_to_cat$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_custom_cat),
            ],
            ENTER_CUSTOM_CAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_custom_cat),
                CallbackQueryHandler(cb_cancel, pattern=r'^cancel$'),
            ],
        },
        fallbacks=[CallbackQueryHandler(cb_cancel, pattern=r'^cancel$')],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler('start',      cmd_start))
    app.add_handler(CommandHandler('uid',        cmd_uid))
    app.add_handler(CommandHandler('menu',       cmd_menu))
    app.add_handler(CommandHandler('disconnect', cmd_disconnect))
    app.add_handler(CallbackQueryHandler(cb_menu,      pattern=r'^menu:'))
    app.add_handler(CallbackQueryHandler(cb_new_entry, pattern=r'^new_entry$'))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()