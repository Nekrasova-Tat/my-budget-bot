import os
import json
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CREDENTIALS_JSON = os.environ.get('GOOGLE_CREDENTIALS')

SHEET_NAME = 'Бюджет'
SHEET_EXPENSES = 'Расходы'
SHEET_INCOMES = 'Доходы'
SHEET_CATEGORIES = 'Категории'

WEBHOOK_URL = 'https://my-budget-bot-mu.vercel.app/api/bot'

# ================== GOOGLE SHEETS ==================
def get_sheet():
    creds_dict = json.loads(CREDENTIALS_JSON)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME)

def type_to_sheet(t):
    return 'Расход' if t == 'expense' else 'Доход'

def get_categories(t):
    sheet = get_sheet().worksheet(SHEET_CATEGORIES)
    data = sheet.get_all_values()
    sheet_type = type_to_sheet(t)
    seen, result = set(), []
    for row in data[1:]:
        if len(row) < 2:
            continue
        row_type = row[0].strip()
        cat = row[1].strip()
        if row_type.lower() == sheet_type.lower() and cat and cat not in seen:
            seen.add(cat)
            result.append(cat)
    return result

def get_subcategories(t, category):
    sheet = get_sheet().worksheet(SHEET_CATEGORIES)
    data = sheet.get_all_values()
    sheet_type = type_to_sheet(t)
    result = []
    for row in data[1:]:
        if len(row) < 3:
            continue
        row_type = row[0].strip()
        cat = row[1].strip()
        sub = row[2].strip()
        if (row_type.lower() == sheet_type.lower()
                and cat == category
                and sub and sub not in ('—', '-')):
            result.append(sub)
    return result

def save_entry(user, state):
    sheet_name = SHEET_EXPENSES if state['type'] == 'expense' else SHEET_INCOMES
    ws = get_sheet().worksheet(sheet_name)
    headers = ws.row_values(1)
    data = {
        'Telegram ID': user.id,
        'Username': '@' + user.username if user.username else '',
        'Дата': state.get('date') or datetime.now().strftime('%Y-%m-%d'),
        'Название': state.get('name', ''),
        'Категория': state.get('category', ''),
        'Подкатегория': state.get('subcategory', ''),
        'Сумма': state.get('amount', ''),
        'Комментарии': state.get('comment', '')
    }
    row = [str(data.get(h.strip(), '')) for h in headers]
    ws.append_row(row)

# ================== КЛАВИАТУРА С ДАТАМИ ==================
def date_keyboard():
    today = datetime.now()
    rows = []
    for i in range(5):
        d = today - timedelta(days=i)
        iso = d.strftime('%Y-%m-%d')
        label = d.strftime('%d.%m.%Y')
        if i == 0:
            label = 'Сегодня, ' + label
        elif i == 1:
            label = 'Вчера, ' + label
        rows.append([InlineKeyboardButton(label, callback_data=f'date:{iso}')])
    rows.append([InlineKeyboardButton('✍️ Ввести вручную', callback_data='date:manual')])
    return InlineKeyboardMarkup(rows)

# ================== ЛОГИКА БОТА ==================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    kb = [[
        InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
        InlineKeyboardButton('💰 Доход', callback_data='type:income')
    ]]
    await update.message.reply_text('Что записываем?', reply_markup=InlineKeyboardMarkup(kb))

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith('type:'):
        t = data.split(':')[1]
        ctx.user_data.clear()
        ctx.user_data['type'] = t
        cats = get_categories(t)
        if not cats:
            await q.edit_message_text('❌ Нет категорий в таблице')
            return
        ctx.user_data['cats'] = cats
        kb = [[InlineKeyboardButton(c, callback_data=f'cat:{i}')] for i, c in enumerate(cats)]
        kb.append([InlineKeyboardButton('⬅️ Отмена', callback_data='menu')])
        await q.edit_message_text('📂 Выберите категорию:', reply_markup=InlineKeyboardMarkup(kb))

    elif data == 'menu':
        ctx.user_data.clear()
        kb = [[
            InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
            InlineKeyboardButton('💰 Доход', callback_data='type:income')
        ]]
        await q.edit_message_text('Что записываем?', reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith('cat:'):
        idx = int(data.split(':')[1])
        cat = ctx.user_data['cats'][idx]
        ctx.user_data['category'] = cat
        subs = get_subcategories(ctx.user_data['type'], cat)
        if not subs:
            ctx.user_data['subcategory'] = ''
            ctx.user_data['step'] = 'name'
            kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip_name')]]
            await q.edit_message_text(
                '📝 Введите название операции (или нажмите «Пропустить»):',
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            ctx.user_data['subs'] = subs
            kb = [[InlineKeyboardButton(s, callback_data=f'sub:{i}')] for i, s in enumerate(subs)]
            kb.append([InlineKeyboardButton('⬅️ Отмена', callback_data='menu')])
            await q.edit_message_text('📁 Выберите подкатегорию:', reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith('sub:'):
        idx = int(data.split(':')[1])
        ctx.user_data['subcategory'] = ctx.user_data['subs'][idx]
        ctx.user_data['step'] = 'name'
        kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip_name')]]
        await q.edit_message_text(
            '📝 Введите название операции (или нажмите «Пропустить»):',
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == 'skip_name':
        ctx.user_data['name'] = ''
        ctx.user_data['step'] = 'amount'
        await q.edit_message_text('💰 Введите сумму:')

    elif data.startswith('date:'):
        value = data.split(':', 1)[1]
        if value == 'manual':
            ctx.user_data['step'] = 'date'
            await q.edit_message_text('📅 Введите дату в формате ДД.ММ.ГГГГ:')
        else:
            parsed = datetime.strptime(value, '%Y-%m-%d')
            ctx.user_data['date'] = parsed.strftime('%Y-%m-%d')
            ctx.user_data['date_display'] = parsed.strftime('%d.%m.%Y')
            ctx.user_data['step'] = 'comment'
            kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip')]]
            await q.edit_message_text('📝 Комментарий:', reply_markup=InlineKeyboardMarkup(kb))

    elif data == 'skip':
        ctx.user_data['comment'] = ''
        await finish(update, ctx)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get('step')
    text = update.message.text.strip()

    if step == 'name':
        ctx.user_data['name'] = text
        ctx.user_data['step'] = 'amount'
        await update.message.reply_text('💰 Введите сумму:')

    elif step == 'amount':
        try:
            amount = float(text.replace(',', '.').replace(' ', ''))
        except ValueError:
            await update.message.reply_text('❌ Нужно число. Попробуйте снова:')
            return
        ctx.user_data['amount'] = amount
        ctx.user_data['step'] = 'date'
        await update.message.reply_text(
            '📅 Выберите дату или введите вручную (ДД.ММ.ГГГГ):',
            reply_markup=date_keyboard()
        )

    elif step == 'date':
        try:
            parsed = datetime.strptime(text, '%d.%m.%Y')
            ctx.user_data['date'] = parsed.strftime('%Y-%m-%d')
            ctx.user_data['date_display'] = parsed.strftime('%d.%m.%Y')
        except ValueError:
            await update.message.reply_text(
                '❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ, например 25.09.2026:'
            )
            return
        ctx.user_data['step'] = 'comment'
        kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip')]]
        await update.message.reply_text('📝 Комментарий:', reply_markup=InlineKeyboardMarkup(kb))

    elif step == 'comment':
        ctx.user_data['comment'] = text
        await finish(update, ctx)

    else:
        await start(update, ctx)

async def finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        save_entry(user, ctx.user_data)
        t = '➖ Расход' if ctx.user_data['type'] == 'expense' else '➕ Доход'
        msg = f"✅ Записано!\n\n{t}: {ctx.user_data.get('name', '') or '—'}"
        msg += f"\n📂 {ctx.user_data.get('category', '')}"
        if ctx.user_data.get('subcategory'):
            msg += f" / {ctx.user_data['subcategory']}"
        msg += f"\n💰 {ctx.user_data['amount']}"
        msg += f"\n📅 {ctx.user_data.get('date_display', ctx.user_data.get('date', ''))}"
        if ctx.user_data.get('comment'):
            msg += f"\n💬 {ctx.user_data['comment']}"
    except Exception as e:
        msg = f'❌ Ошибка: {e}'
    ctx.user_data.clear()
    kb = [[
        InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
        InlineKeyboardButton('💰 Доход', callback_data='type:income')
    ]]
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb))

# ================== FASTAPI ОБЁРТКА ==================
application = Application.builder().token(BOT_TOKEN).updater(None).build()
application.add_handler(CommandHandler('start', start))
application.add_handler(CallbackQueryHandler(on_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES
    )
    async with application:
        await application.start()
        yield
        await application.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/api/bot")
async def process_update(request: Request):
    try:
        req = await request.json()
        update = Update.de_json(req, application.bot)
        await application.process_update(update)
        return Response(status_code=HTTPStatus.OK)
    except Exception as e:
        print(f"Error: {e}")
        return Response(status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
