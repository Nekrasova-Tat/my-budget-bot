import os
import json
from datetime import datetime, timedelta, date
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
SHEET_STATE = 'Состояние'

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

    date_str = state.get('date')
    if date_str:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        date_obj = date.today()

    data = {
        'Telegram ID': user.id,
        'Username': '@' + user.username if user.username else '',
        'Дата': date_obj,
        'Название': state.get('name', ''),
        'Категория': state.get('category', ''),
        'Подкатегория': state.get('subcategory', ''),
        'Сумма': state.get('amount', ''),
        'Комментарии': state.get('comment', '')
    }
    row = [data.get(h.strip(), '') for h in headers]
    ws.append_row(row, value_input_option='USER_ENTERED')

# ================== ХРАНЕНИЕ СОСТОЯНИЯ В SHEETS ==================
def state_sheet():
    return get_sheet().worksheet(SHEET_STATE)

def get_state(chat_id):
    sh = state_sheet()
    all_rows = sh.get_all_values()
    chat_id_str = str(chat_id).strip()
    for idx, row in enumerate(all_rows[1:], start=2):
        if not row:
            continue
        cell = str(row[0]).strip().replace("'", "")
        if cell == chat_id_str:
            data_str = row[2] if len(row) > 2 else '{}'
            try:
                data = json.loads(data_str) if data_str else {}
            except Exception:
                data = {}
            return {'row': idx, 'step': row[1] if len(row) > 1 else '', 'data': data}
    return None

def save_state(chat_id, step, data):
    sh = state_sheet()
    chat_id_str = str(chat_id)

    # Преобразуем всё, что не сериализуется в JSON, в строку
    safe_data = {}
    for k, v in data.items():
        if isinstance(v, (date, datetime)):
            safe_data[k] = v.strftime('%Y-%m-%d')
        else:
            safe_data[k] = v

    data_str = json.dumps(safe_data, ensure_ascii=False)
    existing = get_state(chat_id)
    if existing:
        sh.update(f'A{existing["row"]}:C{existing["row"]}', [[chat_id_str, step, data_str]])
    else:
        sh.append_row([chat_id_str, step, data_str])

def clear_state(chat_id):
    sh = state_sheet()
    existing = get_state(chat_id)
    if existing:
        sh.delete_rows(existing['row'])

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
    chat_id = update.effective_chat.id
    clear_state(chat_id)
    kb = [[
        InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
        InlineKeyboardButton('💰 Доход', callback_data='type:income')
    ]]
    await update.message.reply_text('Что записываем?', reply_markup=InlineKeyboardMarkup(kb))

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    data = q.data

    state = get_state(chat_id)
    step_data = state['data'] if state else {}

    if data.startswith('type:'):
        t = data.split(':')[1]
        step_data = {'type': t}
        cats = get_categories(t)
        if not cats:
            await q.message.reply_text('❌ Нет категорий в таблице')
            return
        step_data['cats'] = cats
        save_state(chat_id, 'category', step_data)
        kb = [[InlineKeyboardButton(c, callback_data=f'cat:{i}')] for i, c in enumerate(cats)]
        kb.append([InlineKeyboardButton('⬅️ Отмена', callback_data='menu')])
        await q.message.reply_text('📂 Выберите категорию:', reply_markup=InlineKeyboardMarkup(kb))

    elif data == 'menu':
        clear_state(chat_id)
        kb = [[
            InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
            InlineKeyboardButton('💰 Доход', callback_data='type:income')
        ]]
        await q.message.reply_text('Что записываем?', reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith('cat:'):
        idx = int(data.split(':')[1])
        cat = step_data['cats'][idx]
        step_data['category'] = cat
        subs = get_subcategories(step_data['type'], cat)
        if not subs:
            step_data['subcategory'] = ''
            save_state(chat_id, 'name', step_data)
            kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip_name')]]
            await q.message.reply_text(
                '📝 Введите название операции (или нажмите «Пропустить»):',
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            step_data['subs'] = subs
            save_state(chat_id, 'subcategory', step_data)
            kb = [[InlineKeyboardButton(s, callback_data=f'sub:{i}')] for i, s in enumerate(subs)]
            kb.append([InlineKeyboardButton('⬅️ Отмена', callback_data='menu')])
            await q.message.reply_text('📁 Выберите подкатегорию:', reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith('sub:'):
        idx = int(data.split(':')[1])
        step_data['subcategory'] = step_data['subs'][idx]
        save_state(chat_id, 'name', step_data)
        kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip_name')]]
        await q.message.reply_text(
            '📝 Введите название операции (или нажмите «Пропустить»):',
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == 'skip_name':
        step_data['name'] = ''
        save_state(chat_id, 'amount', step_data)
        await q.message.reply_text('💰 Введите сумму:')

    elif data.startswith('date:'):
        value = data.split(':', 1)[1]
        if value == 'manual':
            save_state(chat_id, 'date', step_data)
            await q.message.reply_text('📅 Введите дату в формате ДД.ММ.ГГГГ:')
        else:
            parsed = datetime.strptime(value, '%Y-%m-%d')
            step_data['date'] = parsed.strftime('%Y-%m-%d')
            step_data['date_display'] = parsed.strftime('%d.%m.%Y')
            save_state(chat_id, 'comment', step_data)
            kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip')]]
            await q.message.reply_text('📝 Комментарий:', reply_markup=InlineKeyboardMarkup(kb))

    elif data == 'skip':
        step_data['comment'] = ''
        await finish(chat_id, q.message, step_data)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state:
        await start(update, ctx)
        return

    step = state['step']
    step_data = state['data']
    text = update.message.text.strip()

    if step == 'name':
        step_data['name'] = text
        save_state(chat_id, 'amount', step_data)
        await update.message.reply_text('💰 Введите сумму:')

    elif step == 'amount':
        try:
            amount = float(text.replace(',', '.').replace(' ', ''))
        except ValueError:
            await update.message.reply_text('❌ Нужно число. Попробуйте снова:')
            return
        step_data['amount'] = amount
        save_state(chat_id, 'date', step_data)
        await update.message.reply_text(
            '📅 Выберите дату или введите вручную (ДД.ММ.ГГГГ):',
            reply_markup=date_keyboard()
        )

    elif step == 'date':
        try:
            parsed = datetime.strptime(text, '%d.%m.%Y')
            step_data['date'] = parsed.strftime('%Y-%m-%d')
            step_data['date_display'] = parsed.strftime('%d.%m.%Y')
        except ValueError:
            await update.message.reply_text(
                '❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ, например 25.09.2026:'
            )
            return
        save_state(chat_id, 'comment', step_data)
        kb = [[InlineKeyboardButton('⏭ Пропустить', callback_data='skip')]]
        await update.message.reply_text('📝 Комментарий:', reply_markup=InlineKeyboardMarkup(kb))

    elif step == 'comment':
        step_data['comment'] = text
        await finish(chat_id, update.message, step_data)

    else:
        await start(update, ctx)

async def finish(chat_id, message, step_data):
    try:
        user = message.from_user if hasattr(message, 'from_user') else None
        if user is None:
            user = type('User', (), {'id': chat_id, 'username': ''})()
        save_entry(user, step_data)
        t = '➖ Расход' if step_data['type'] == 'expense' else '➕ Доход'
        msg = f"✅ Записано!\n\n{t}: {step_data.get('name', '') or '—'}"
        msg += f"\n📂 {step_data.get('category', '')}"
        if step_data.get('subcategory'):
            msg += f" / {step_data['subcategory']}"
        msg += f"\n💰 {step_data['amount']}"
        msg += f"\n📅 {step_data.get('date_display', step_data.get('date', ''))}"
        if step_data.get('comment'):
            msg += f"\n💬 {step_data['comment']}"
    except Exception as e:
        msg = f'❌ Ошибка: {e}'

    clear_state(chat_id)
    kb = [[
        InlineKeyboardButton('➕ Расход', callback_data='type:expense'),
        InlineKeyboardButton('💰 Доход', callback_data='type:income')
    ]]
    await message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb))

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
        await applicationов.stop()

 —app = FastAPI(lifespan=lifes этоpan)

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
