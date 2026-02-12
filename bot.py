import os
import logging
import sqlite3
import math
import json
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import requests

# ========== ЗАГРУЗКА .ENV ==========
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []

# Marzban API
MARZBAN_URL = os.getenv('MARZBAN_URL', 'http://localhost:8443')
MARZBAN_USER = os.getenv('MARZBAN_USER', 'admin')
MARZBAN_PASS = os.getenv('MARZBAN_PASS')

# CryptoBot (опционально)
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN')

# ========== КОНСТАНТЫ ==========
STAR_PRICE_RUB = 1.65
USDT_PRICE_RUB = 90
TARIFFS = {
    'month': {'name': '1 месяц', 'price': 100, 'days': 30},
    'quarter': {'name': '3 месяца', 'price': 250, 'days': 90},
    'year': {'name': '1 год', 'price': 900, 'days': 365}
}

# ========== FLASK ==========
app = Flask(__name__)

# ========== TELEGRAM BOT ==========
bot = telebot.TeleBot(BOT_TOKEN)

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ (SQLite) ==========
def get_db():
    if os.environ.get('RENDER'):
        db_path = '/tmp/whiteprism.db'
    else:
        db_path = 'whiteprism.db'
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
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
logger.info("✅ Database initialized")

# ========== УСТАНОВКА ВЕБХУКА ==========
if WEBHOOK_URL:
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"✅ Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Webhook setup failed: {e}")

# ========== MARZBAN API ==========
marzban_token = None
token_expiry = None

def marzban_auth():
    global marzban_token, token_expiry
    if marzban_token and token_expiry and datetime.now() < token_expiry:
        return marzban_token
    
    try:
        resp = requests.post(
            f'{MARZBAN_URL}/api/admin/token',
            json={'username': MARZBAN_USER, 'password': MARZBAN_PASS},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            marzban_token = data['access_token']
            token_expiry = datetime.now() + timedelta(hours=1)
            return marzban_token
        else:
            logger.error(f"Marzban auth failed: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Marzban connection error: {e}")
        return None

def create_vpn_for_user(user_id, days):
    token = marzban_auth()
    if not token:
        return False
    
    headers = {'Authorization': f'Bearer {token}'}
    conn = get_db()
    cur = conn.cursor()
    
    try:
        # Проверяем активную подписку
        cur.execute('''
            SELECT * FROM subscriptions 
            WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
        ''', (user_id,))
        sub = cur.fetchone()
        
        if sub:
            # Продление существующего
            username = sub['marzban_username']
            new_expire = datetime.fromisoformat(sub['expires_at']) + timedelta(days=days)
            
            resp = requests.put(
                f'{MARZBAN_URL}/api/user/{username}',
                headers=headers,
                json={'expire': int(new_expire.timestamp())},
                timeout=10
            )
            
            if resp.status_code == 200:
                cur.execute(
                    'UPDATE subscriptions SET expires_at = ? WHERE id = ?',
                    (new_expire.strftime('%Y-%m-%d %H:%M:%S'), sub['id'])
                )
                conn.commit()
                bot.send_message(user_id, f"✅ Подписка продлена на {days} дней!")
                return True
        else:
            # Создание нового
            username = f"user_{user_id}_{int(datetime.now().timestamp())}"
            expire = int((datetime.now() + timedelta(days=days)).timestamp())
            
            user_data = {
                'username': username,
                'proxies': {'vless': {}},
                'expire': expire,
                'data_limit': 0,
                'status': 'active'
            }
            
            resp = requests.post(
                f'{MARZBAN_URL}/api/user',
                headers=headers,
                json=user_data,
                timeout=10
            )
            
            if resp.status_code == 200:
                # Получаем конфиг
                config_resp = requests.get(
                    f'{MARZBAN_URL}/api/user/{username}/config',
                    headers=headers,
                    timeout=10
                )
                
                if config_resp.status_code == 200:
                    config_link = config_resp.json().get('link', '')
                    
                    cur.execute('''
                        INSERT INTO subscriptions (user_id, marzban_username, config_link, expires_at)
                        VALUES (?, ?, ?, ?)
                    ''', (
                        user_id,
                        username,
                        config_link,
                        (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                    ))
                    conn.commit()
                    
                    # Отправляем конфиг
                    text = (
                        f"✅ VPN-доступ активирован!\n"
                        f"📅 Действует до: {(datetime.now() + timedelta(days=days)).strftime('%d.%m.%Y')}\n"
                        f"🔗 Ссылка для подключения:\n`{config_link}`\n\n"
                        f"📱 Для Android: v2rayNG\n"
                        f"🍏 Для iPhone: Streisand"
                    )
                    bot.send_message(user_id, text, parse_mode='Markdown')
                    return True
    except Exception as e:
        logger.error(f"VPN creation error: {e}")
        bot.send_message(user_id, "❌ Ошибка при создании VPN. Администратор уже уведомлён.")
    finally:
        conn.close()
    return False

# ========== КОМАНДЫ TELEGRAM ==========
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton('🛒 Купить подписку', callback_data='buy'))
    bot.send_message(
        user_id,
        '👋 Добро пожаловать в WhitePrism VPN!\n\n'
        '🚀 Быстрый и стабильный VPN на базе VLESS\n'
        '🌍 Сервера в Нидерландах\n'
        '📱 Поддержка всех устройств\n\n'
        '👇 Нажми кнопку ниже, чтобы выбрать тариф',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == 'buy')
def buy_callback(call):
    markup = InlineKeyboardMarkup()
    for key, tariff in TARIFFS.items():
        markup.add(InlineKeyboardButton(
            f'{tariff["name"]} — {tariff["price"]} ₽',
            callback_data=f'tariff_{key}'
        ))
    bot.send_message(call.from_user.id, '📦 Выберите тариф:', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('tariff_'))
def tariff_selected(call):
    tariff_key = call.data.split('_')[1]
    tariff = TARIFFS[tariff_key]
    
    markup = InlineKeyboardMarkup()
    stars_amount = math.ceil(tariff['price'] / STAR_PRICE_RUB)
    markup.add(InlineKeyboardButton(
        f'⭐️ Оплатить {stars_amount} Stars',
        callback_data=f'pay_stars_{tariff_key}_{stars_amount}'
    ))
    
    if CRYPTOBOT_TOKEN:
        markup.add(InlineKeyboardButton(
            '💲 Оплатить USDT (CryptoBot)',
            callback_data=f'pay_crypto_{tariff_key}'
        ))
    
    bot.send_message(
        call.from_user.id,
        f'📌 Тариф: {tariff["name"]}\n'
        f'💰 Сумма: {tariff["price"]} ₽\n'
        f'📆 Период: {tariff["days"]} дней\n\n'
        f'Выберите способ оплаты:',
        reply_markup=markup
    )

# ========== ОПЛАТА STARS ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_stars_'))
def pay_stars(call):
    _, _, tariff_key, stars = call.data.split('_')
    tariff = TARIFFS[tariff_key]
    stars = int(stars)
    
    prices = [telebot.types.LabeledPrice(label=tariff['name'], amount=stars * 100)]
    
    try:
        bot.send_invoice(
            call.from_user.id,
            title=f'VPN {tariff["name"]}',
            description=f'Подписка на {tariff["days"]} дней',
            invoice_payload=f'stars_{tariff_key}_{call.from_user.id}',
            provider_token='',
            currency='XTR',
            prices=prices,
            start_parameter='create_invoice_stars'
        )
    except Exception as e:
        logger.error(f"Stars payment error: {e}")
        bot.send_message(call.from_user.id, "❌ Ошибка при создании счёта. Попробуйте позже.")

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    user_id = message.from_user.id
    amount_stars = payment.total_amount // 100
    
    if payload.startswith('stars_'):
        _, tariff_key, _ = payload.split('_')
        tariff = TARIFFS[tariff_key]
        rub_amount = int(amount_stars * STAR_PRICE_RUB)
        
        conn = get_db()
        cur = conn.cursor()
        
        # Записываем платеж
        cur.execute(
            'INSERT INTO payments (user_id, amount, currency, payment_id, status) VALUES (?, ?, ?, ?, ?)',
            (user_id, rub_amount, 'XTR', payment.telegram_payment_charge_id, 'completed')
        )
        
        # Начисляем баланс
        cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (rub_amount, user_id))
        if cur.rowcount == 0:
            cur.execute('INSERT INTO users (user_id, balance) VALUES (?, ?)', (user_id, rub_amount))
        
        conn.commit()
        conn.close()
        
        # Выдаём VPN
        bot.send_message(user_id, "⏳ Создаём ваш VPN-ключ...")
        create_vpn_for_user(user_id, tariff['days'])

# ========== CRYPTOBOT WEBHOOK ==========
@app.route('/crypto_webhook', methods=['POST'])
def crypto_webhook():
    if not CRYPTOBOT_TOKEN:
        return 'CryptoBot not configured', 400
    
    data = request.json
    if data.get('event') == 'invoice_paid':
        invoice_id = data['payload']['invoice_id']
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT user_id, amount FROM payments WHERE payment_id = ? AND status = "pending"',
            (str(invoice_id),)
        )
        row = cur.fetchone()
        
        if row:
            user_id = row['user_id']
            amount = row['amount']
            
            cur.execute('UPDATE payments SET status = "completed" WHERE payment_id = ?', (str(invoice_id),))
            cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
            conn.commit()
            
            bot.send_message(user_id, "⏳ Создаём ваш VPN-ключ...")
            create_vpn_for_user(user_id, 30)  # По умолчанию 30 дней
        
        conn.close()
    
    return 'OK', 200

# ========== АДМИН-КОМАНДЫ ==========
def admin_only(func):
    @wraps(func)
    def wrapped(message):
        if message.from_user.id in ADMIN_IDS:
            return func(message)
        else:
            bot.reply_to(message, "⛔ Доступ запрещён")
    return wrapped

@bot.message_handler(commands=['stats'])
@admin_only
def stats(message):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('SELECT COUNT(*) FROM users')
    users_count = cur.fetchone()[0]
    
    cur.execute('SELECT SUM(amount) FROM payments WHERE status="completed"')
    total_revenue = cur.fetchone()[0] or 0
    
    cur.execute('SELECT COUNT(*) FROM subscriptions WHERE status="active" AND expires_at > datetime("now")')
    active_subs = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM subscriptions')
    total_subs = cur.fetchone()[0]
    
    conn.close()
    
    text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"💰 Выручка: {total_revenue} ₽\n"
        f"✅ Активных подписок: {active_subs}\n"
        f"📦 Всего продано: {total_subs}"
    )
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

@bot.message_handler(commands=['broadcast'])
@admin_only
def broadcast(message):
    text = message.text.replace('/broadcast', '').strip()
    if not text:
        bot.reply_to(message, "❌ Введите текст для рассылки")
        return
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM users')
    users = cur.fetchall()
    conn.close()
    
    sent = 0
    for user in users:
        try:
            bot.send_message(user['user_id'], text)
            sent += 1
        except:
            continue
    
    bot.reply_to(message, f"✅ Рассылка отправлена {sent} пользователям")

# ========== WEBHOOK ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_str = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 500

@app.route('/')
def index():
    return 'WhitePrism VPN Bot is running!'

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
