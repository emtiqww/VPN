import os
import logging
import sqlite3
import math
import json
import qrcode
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_file
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import requests

# ---------- Настройка ----------
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # https://your-domain.vercel.app/webhook
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []

# Marzban API
MARZBAN_URL = os.getenv('MARZBAN_URL')
MARZBAN_USER = os.getenv('MARZBAN_USER')
MARZBAN_PASS = os.getenv('MARZBAN_PASS')

# CryptoBot API
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN')

# Константы
STAR_PRICE_RUB = 1.65
USDT_PRICE_RUB = 90  # фиксированный курс
TARIFFS = {
    'month': {'name': '1 месяц', 'price': 100, 'days': 30},
    'quarter': {'name': '3 месяца', 'price': 250, 'days': 90},
    'year': {'name': '1 год', 'price': 900, 'days': 365}
}

# ---------- Flask ----------
app = Flask(__name__)

# ---------- Telegram Bot ----------
bot = telebot.TeleBot(BOT_TOKEN)

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- База данных ----------
def get_db_connection():
    """Подключение к SQLite (работает и на Vercel, если БД в /tmp)"""
    # На Vercel можно писать только в /tmp
    if os.environ.get('VERCEL'):
        db_path = '/tmp/whiteprism.db'
    else:
        db_path = 'database/whiteprism.db'
        os.makedirs('database', exist_ok=True)
    
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            currency TEXT,
            payment_id TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            marzban_username TEXT,
            config_link TEXT,
            expires_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------- Маршруты Flask ----------
@app.route('/')
def index():
    return 'WhitePrism VPN Bot is running!'

@app.route('/webhook', methods=['POST'])
def webhook():
    """Приём обновлений от Telegram"""
    json_str = request.get_data(as_text=True)
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return 'OK', 200

@app.route('/webapp', methods=['GET'])
def webapp():
    """Страница Web App"""
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>WhitePrism VPN</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: var(--tg-theme-bg-color); color: var(--tg-theme-text-color); }
            .card { background: var(--tg-theme-secondary-bg-color); border-radius: 10px; padding: 15px; margin-bottom: 15px; }
            button { background: var(--tg-theme-button-color); color: var(--tg-theme-button-text-color); border: none; padding: 10px 20px; border-radius: 8px; width: 100%; }
        </style>
    </head>
    <body>
        <div id="app">
            <h1>Личный кабинет</h1>
            <div class="card">
                <h3>Баланс: <span id="balance">0</span> ⭐️</h3>
                <button onclick="topup()">Пополнить</button>
            </div>
            <div class="card">
                <h3>Подписки</h3>
                <div id="subscriptions"></div>
            </div>
        </div>
        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            tg.ready();

            async function loadData() {
                let initData = tg.initData;
                let response = await fetch('/api/user_data', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({initData})
                });
                let data = await response.json();
                document.getElementById('balance').innerText = data.balance;
                let subsHtml = '';
                data.subscriptions.forEach(sub => {
                    subsHtml += `<div>${sub.name} - до ${sub.expires}</div>`;
                });
                document.getElementById('subscriptions').innerHTML = subsHtml || 'Нет активных подписок';
            }
            loadData();

            function topup() {
                tg.sendData(JSON.stringify({action: 'topup'}));
            }
        </script>
    </body>
    </html>
    '''
    return html

@app.route('/api/user_data', methods=['POST'])
def user_data():
    """API для WebApp — отдаёт данные пользователя"""
    data = request.json
    init_data = data.get('initData')
    # Здесь нужно валидировать initData (см. документацию Telegram)
    # Для простоты пропускаем, в реальном проекте обязательна проверка!
    from urllib.parse import parse_qs
    parsed = parse_qs(init_data)
    user = json.loads(parsed.get('user', ['{}'])[0])
    user_id = user.get('id')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    balance = row['balance'] if row else 0
    
    cur.execute('''
        SELECT * FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
    ''', (user_id,))
    subs = cur.fetchall()
    conn.close()
    
    return jsonify({
        'balance': balance,
        'subscriptions': [{'name': 'VPN', 'expires': sub['expires_at']} for sub in subs]
    })

# ---------- Команды Telegram ----------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton('🛒 Купить подписку', callback_data='buy'))
    markup.add(InlineKeyboardButton('👤 Личный кабинет', web_app=telebot.types.WebAppInfo(WEBHOOK_URL.replace('/webhook', '/webapp'))))
    bot.send_message(user_id, 'Добро пожаловать!', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'buy')
def buy_callback(call):
    user_id = call.from_user.id
    markup = InlineKeyboardMarkup()
    for key, tariff in TARIFFS.items():
        markup.add(InlineKeyboardButton(f'{tariff["name"]} — {tariff["price"]} ₽', callback_data=f'tariff_{key}'))
    bot.send_message(user_id, 'Выберите тариф:', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('tariff_'))
def tariff_selected(call):
    tariff_key = call.data.split('_')[1]
    tariff = TARIFFS[tariff_key]
    
    markup = InlineKeyboardMarkup()
    stars_amount = math.ceil(tariff['price'] / STAR_PRICE_RUB)
    markup.add(InlineKeyboardButton(f'Оплатить ⭐️ {stars_amount} Stars', callback_data=f'pay_stars_{tariff_key}_{stars_amount}'))
    
    if CRYPTOBOT_TOKEN:
        markup.add(InlineKeyboardButton(f'Оплатить USDT (≈{tariff["price"]}₽)', callback_data=f'pay_crypto_{tariff_key}'))
    
    bot.send_message(call.from_user.id, f'Тариф: {tariff["name"]}\nСумма: {tariff["price"]} ₽', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_stars_'))
def pay_stars(call):
    _, _, tariff_key, stars = call.data.split('_')
    tariff = TARIFFS[tariff_key]
    stars = int(stars)
    
    # Создаём инвойс для Telegram Stars
    prices = [telebot.types.LabeledPrice(label=tariff['name'], amount=stars * 100)]  # Stars в копейках (1 звезда = 100)
    bot.send_invoice(
        call.from_user.id,
        title=f'Подписка {tariff["name"]}',
        description=f'VPN подписка на {tariff["days"]} дней',
        invoice_payload=f'stars_{tariff_key}_{call.from_user.id}',
        provider_token='',  # Пусто для Stars
        currency='XTR',     # Код Stars
        prices=prices,
        start_parameter='create_invoice_stars'
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    user_id = message.from_user.id
    total_amount = payment.total_amount // 100  # перевод из копеек звезд
    
    if payload.startswith('stars_'):
        _, tariff_key, _ = payload.split('_')
        tariff = TARIFFS[tariff_key]
        
        # Записываем платеж
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO payments (user_id, amount, currency, payment_id, status) VALUES (?, ?, ?, ?, ?)',
            (user_id, total_amount, 'XTR', payment.telegram_payment_charge_id, 'completed')
        )
        # Начисляем баланс (можно и напрямую выдавать подписку)
        cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (total_amount * STAR_PRICE_RUB, user_id))
        conn.commit()
        conn.close()
        
        # Выдаём VPN-ключ
        create_vpn_for_user(user_id, tariff['days'])
        
        bot.send_message(user_id, '✅ Оплата прошла! Ваш VPN-ключ скоро придёт.')

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_crypto_'))
def pay_crypto(call):
    tariff_key = call.data.split('_')[2]
    tariff = TARIFFS[tariff_key]
    amount_usd = round(tariff['price'] / USDT_PRICE_RUB, 2)
    
    # Создаём инвойс в CryptoBot
    headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
    payload = {
        'asset': 'USDT',
        'amount': amount_usd,
        'description': f'VPN {tariff["name"]}',
        'payload': f'crypto_{tariff_key}_{call.from_user.id}'
    }
    resp = requests.post('https://pay.crypt.bot/api/createInvoice', headers=headers, json=payload)
    if resp.status_code == 200:
        invoice = resp.json()['result']
        pay_url = invoice['pay_url']
        invoice_id = invoice['invoice_id']
        
        # Сохраняем платёж как pending
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO payments (user_id, amount, currency, payment_id, status) VALUES (?, ?, ?, ?, ?)',
            (call.from_user.id, tariff['price'], 'USDT', str(invoice_id), 'pending')
        )
        conn.commit()
        conn.close()
        
        bot.send_message(call.from_user.id, f'Ссылка для оплаты: {pay_url}\nПосле оплаты подписка активируется автоматически.')
    else:
        bot.send_message(call.from_user.id, 'Ошибка создания счёта. Попробуйте позже.')

# ---------- Интеграция с Marzban ----------
marzban_token = None
token_expiry = None

def marzban_auth():
    global marzban_token, token_expiry
    if marzban_token and token_expiry and datetime.now() < token_expiry:
        return marzban_token
    
    resp = requests.post(f'{MARZBAN_URL}/api/admin/token', json={
        'username': MARZBAN_USER,
        'password': MARZBAN_PASS
    })
    if resp.status_code == 200:
        data = resp.json()
        marzban_token = data['access_token']
        token_expiry = datetime.now() + timedelta(hours=1)
        return marzban_token
    else:
        raise Exception('Marzban auth failed')

def create_vpn_for_user(user_id, days):
    token = marzban_auth()
    headers = {'Authorization': f'Bearer {token}'}
    
    # Проверяем, есть ли уже подписка
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT * FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
    ''', (user_id,))
    sub = cur.fetchone()
    
    if sub:
        # Продление
        username = sub['marzban_username']
        new_expires = datetime.strptime(sub['expires_at'], '%Y-%m-%d %H:%M:%S') + timedelta(days=days)
        # Обновляем в Marzban (зависит от API)
        # Например, обновляем срок
        resp = requests.put(f'{MARZBAN_URL}/api/user/{username}', headers=headers, json={
            'expire': int(new_expires.timestamp())
        })
        if resp.status_code == 200:
            cur.execute('UPDATE subscriptions SET expires_at = ? WHERE id = ?', (new_expires, sub['id']))
            conn.commit()
    else:
        # Создаём нового пользователя
        username = f'user_{user_id}_{datetime.now().timestamp()}'
        expire_timestamp = int((datetime.now() + timedelta(days=days)).timestamp())
        user_data = {
            'username': username,
            'proxies': {'vless': {}},  # или другой протокол
            'expire': expire_timestamp,
            'data_limit': 0,  # без лимита
        }
        resp = requests.post(f'{MARZBAN_URL}/api/user', headers=headers, json=user_data)
        if resp.status_code == 200:
            # Получаем конфиг (например, ссылку)
            config_resp = requests.get(f'{MARZBAN_URL}/api/user/{username}/config', headers=headers)
            if config_resp.status_code == 200:
                config_link = config_resp.json().get('link', '')
                # Сохраняем в БД
                cur.execute('''
                    INSERT INTO subscriptions (user_id, marzban_username, config_link, expires_at)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, username, config_link, (datetime.now() + timedelta(days=days))))
                conn.commit()
                
                # Генерируем QR-код
                qr = qrcode.QRCode(box_size=10, border=4)
                qr.add_data(config_link)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                bio = BytesIO()
                img.save(bio, 'PNG')
                bio.seek(0)
                
                bot.send_photo(user_id, bio, caption=f'🔑 Ваш VPN-ключ:\n`{config_link}`', parse_mode='Markdown')
    conn.close()

# ---------- Проверка платежей CryptoBot (Webhook или периодическая) ----------
# Для простоты реализуем эндпоинт для вебхука от CryptoBot
@app.route('/crypto_webhook', methods=['POST'])
def crypto_webhook():
    data = request.json
    if data.get('event') == 'invoice_paid':
        invoice_id = data['payload']['invoice_id']
        # Обновляем статус платежа
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT user_id, amount FROM payments WHERE payment_id = ? AND status = "pending"', (str(invoice_id),))
        row = cur.fetchone()
        if row:
            user_id = row['user_id']
            # Начисляем баланс (можно сразу выдавать подписку)
            cur.execute('UPDATE payments SET status = "completed" WHERE payment_id = ?', (str(invoice_id),))
            cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (row['amount'], user_id))
            conn.commit()
            
            # Извлекаем tariff_key из payload (нужно сохранять при создании)
            # Упрощённо: выдаём 30 дней
            create_vpn_for_user(user_id, 30)
        conn.close()
    return 'OK', 200

# ---------- Админ-команды ----------
def admin_required(func):
    @wraps(func)
    def wrapper(message):
        if message.from_user.id in ADMIN_IDS:
            return func(message)
        else:
            bot.reply_to(message, '⛔ Доступ запрещён')
    return wrapper

@bot.message_handler(commands=['stats'])
@admin_required
def stats(message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM users')
    users_count = cur.fetchone()[0]
    cur.execute('SELECT SUM(amount) FROM payments WHERE status="completed"')
    total_revenue = cur.fetchone()[0] or 0
    cur.execute('SELECT COUNT(*) FROM subscriptions WHERE status="active" AND expires_at > datetime("now")')
    active_subs = cur.fetchone()[0]
    conn.close()
    
    bot.send_message(message.chat.id, 
                     f'📊 Статистика:\nПользователей: {users_count}\nВыручка: {total_revenue} ₽\nАктивных подписок: {active_subs}')

@bot.message_handler(commands=['add_balance'])
@admin_required
def add_balance(message):
    try:
        _, user_id_str, amount_str = message.text.split()
        user_id = int(user_id_str)
        amount = int(amount_str)
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        if cur.rowcount == 0:
            cur.execute('INSERT INTO users (user_id, balance) VALUES (?, ?)', (user_id, amount))
        conn.commit()
        conn.close()
        
        bot.send_message(message.chat.id, f'✅ Баланс пользователя {user_id} пополнен на {amount} ₽')
        bot.send_message(user_id, f'💰 Ваш баланс пополнен на {amount} ₽ администратором.')
    except:
        bot.send_message(message.chat.id, '❌ Формат: /add_balance user_id сумма')

# ---------- Запуск ----------
if __name__ == '__main__':
    # Для локального тестирования: polling
    # bot.remove_webhook()
    # bot.polling()
    
    # Для деплоя: webhook
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)