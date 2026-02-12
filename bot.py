import os
import sys
import logging
import sqlite3
import math
import json
import qrcode
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import parse_qs

from flask import Flask, request, jsonify, send_file
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from dotenv import load_dotenv
import requests

# ================ НАСТРОЙКА ЛОГИРОВАНИЯ ================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ================ ЗАГРУЗКА .ENV ================
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в .env")
    sys.exit(1)

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL не найден в .env")
    sys.exit(1)

ADMIN_IDS = []
if os.getenv('ADMIN_IDS'):
    ADMIN_IDS = [int(x.strip()) for x in os.getenv('ADMIN_IDS').split(',')]

# ================ MARZBAN НАСТРОЙКИ ================
MARZBAN_URL = os.getenv('MARZBAN_URL', 'http://localhost:8443')
MARZBAN_USER = os.getenv('MARZBAN_USER', 'admin')
MARZBAN_PASS = os.getenv('MARZBAN_PASS', '')

# ================ CRYPTOBOT ================
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN', '')

# ================ КОНСТАНТЫ ================
STAR_PRICE_RUB = 1.65
USDT_PRICE_RUB = 90

TARIFFS = {
    'month': {
        'name': '1 месяц',
        'price': 199,
        'days': 30,
        'popular': True
    },
    'quarter': {
        'name': '3 месяца',
        'price': 499,
        'days': 90,
        'popular': False
    },
    'year': {
        'name': '1 год',
        'price': 1499,
        'days': 365,
        'popular': False
    }
}

COUNTRIES = {
    'nl': '🇳🇱 Нидерланды',
    'de': '🇩🇪 Германия',
    'fi': '🇫🇮 Финляндия',
    'us': '🇺🇸 США',
    'sg': '🇸🇬 Сингапур'
}

# ================ FLASK ================
app = Flask(__name__)

# ================ TELEGRAM BOT ================
bot = telebot.TeleBot(BOT_TOKEN)

# ================ БАЗА ДАННЫХ ================
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
    
    # Пользователи
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            balance INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP
        )
    ''')
    
    # Платежи
    cur.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            currency TEXT,
            payment_id TEXT UNIQUE,
            tariff TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
    
    # Подписки
    cur.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            marzban_username TEXT UNIQUE,
            config_link TEXT,
            country TEXT DEFAULT 'nl',
            expires_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            auto_renew BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
    
    # Настройки
    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

init_db()

# ================ MARZBAN API ================
class MarzbanAPI:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.token = None
        self.token_expiry = None
    
    def _auth(self):
        if self.token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.token
        
        try:
            resp = requests.post(
                f'{self.base_url}/api/admin/token',
                json={'username': self.username, 'password': self.password},
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                self.token = data['access_token']
                self.token_expiry = datetime.now() + timedelta(hours=1)
                return self.token
            else:
                logger.error(f"Marzban auth failed: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Marzban connection error: {e}")
            return None
    
    def create_user(self, username, days, data_limit=0):
        token = self._auth()
        if not token:
            return None
        
        headers = {'Authorization': f'Bearer {token}'}
        expire = int((datetime.now() + timedelta(days=days)).timestamp())
        
        user_data = {
            'username': username,
            'proxies': {
                'vless': {},
                'trojan': {},
                'shadowsocks': {}
            },
            'expire': expire,
            'data_limit': data_limit,
            'status': 'active'
        }
        
        try:
            resp = requests.post(
                f'{self.base_url}/api/user',
                headers=headers,
                json=user_data,
                timeout=10
            )
            
            if resp.status_code == 200:
                config_resp = requests.get(
                    f'{self.base_url}/api/user/{username}/config',
                    headers=headers,
                    timeout=10
                )
                
                if config_resp.status_code == 200:
                    return config_resp.json().get('link', '')
            return None
        except Exception as e:
            logger.error(f"Marzban create user error: {e}")
            return None
    
    def extend_user(self, username, days):
        token = self._auth()
        if not token:
            return False
        
        headers = {'Authorization': f'Bearer {token}'}
        
        try:
            resp = requests.get(
                f'{self.base_url}/api/user/{username}',
                headers=headers,
                timeout=10
            )
            
            if resp.status_code == 200:
                user_data = resp.json()
                current_expire = user_data.get('expire', 0)
                
                if current_expire:
                    new_expire = max(current_expire, int(datetime.now().timestamp()))
                    new_expire = int(new_expire) + (days * 86400)
                else:
                    new_expire = int((datetime.now() + timedelta(days=days)).timestamp())
                
                update_resp = requests.put(
                    f'{self.base_url}/api/user/{username}',
                    headers=headers,
                    json={'expire': new_expire},
                    timeout=10
                )
                
                return update_resp.status_code == 200
            return False
        except Exception as e:
            logger.error(f"Marzban extend user error: {e}")
            return False
    
    def delete_user(self, username):
        token = self._auth()
        if not token:
            return False
        
        headers = {'Authorization': f'Bearer {token}'}
        
        try:
            resp = requests.delete(
                f'{self.base_url}/api/user/{username}',
                headers=headers,
                timeout=10
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Marzban delete user error: {e}")
            return False

marzban = MarzbanAPI(MARZBAN_URL, MARZBAN_USER, MARZBAN_PASS)

# ================ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ================
def generate_qr(data):
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

def get_user_balance(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row['balance'] if row else 0

def update_user_balance(user_id, amount):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO users (user_id, balance, last_activity) 
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET 
        balance = balance + ?,
        last_activity = CURRENT_TIMESTAMP
    ''', (user_id, amount, amount))
    conn.commit()
    conn.close()

def add_payment(user_id, amount, currency, payment_id, tariff, status='pending'):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO payments (user_id, amount, currency, payment_id, tariff, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, amount, currency, payment_id, tariff, status))
    conn.commit()
    payment_id_db = cur.lastrowid
    conn.close()
    return payment_id_db

def complete_payment(payment_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        UPDATE payments 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
        WHERE payment_id = ? AND status = 'pending'
    ''', (str(payment_id),))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def create_vpn_subscription(user_id, days, country='nl'):
    username = f"user_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    config_link = marzban.create_user(username, days)
    
    if not config_link:
        return False
    
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO subscriptions (user_id, marzban_username, config_link, country, expires_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        user_id,
        username,
        config_link,
        country,
        (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    ))
    
    conn.commit()
    conn.close()
    
    return {
        'username': username,
        'config_link': config_link,
        'expires_at': datetime.now() + timedelta(days=days)
    }

def extend_vpn_subscription(user_id, days):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        SELECT * FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
        ORDER BY expires_at DESC LIMIT 1
    ''', (user_id,))
    
    sub = cur.fetchone()
    
    if not sub:
        conn.close()
        return None
    
    success = marzban.extend_user(sub['marzban_username'], days)
    
    if success:
        new_expire = datetime.fromisoformat(sub['expires_at']) + timedelta(days=days)
        cur.execute('''
            UPDATE subscriptions 
            SET expires_at = ? 
            WHERE id = ?
        ''', (new_expire.strftime('%Y-%m-%d %H:%M:%S'), sub['id']))
        conn.commit()
        conn.close()
        return new_expire
    
    conn.close()
    return None

def get_user_subscriptions(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT * FROM subscriptions 
        WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
        ORDER BY expires_at DESC
    ''', (user_id,))
    subs = cur.fetchall()
    conn.close()
    return subs

# ================ УСТАНОВКА ВЕБХУКА ================
def setup_webhook():
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")
        
        webhook_info = bot.get_webhook_info()
        logger.info(f"📡 Webhook info: {webhook_info}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки webhook: {e}")

setup_webhook()

# ================ ДЕКОРАТОР АДМИНА ================
def admin_only(func):
    @wraps(func)
    def wrapped(message):
        if message.from_user.id in ADMIN_IDS:
            return func(message)
        else:
            bot.reply_to(message, "⛔ Доступ запрещён")
    return wrapped

# ================ КОМАНДЫ ================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    logger.info(f"🚀 /start от {user_id} (@{username})")
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, last_activity)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, username, first_name, last_name))
    cur.execute('''
        UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = ?
    ''', (user_id,))
    conn.commit()
    conn.close()
    
    welcome_text = (
        f"👋 Привет, {first_name or 'друг'}!\n\n"
        f"🚀 **WhitePrism VPN** — быстрый и стабильный VPN\n"
        f"🌍 Сервера в Европе и США\n"
        f"📱 Поддержка всех устройств\n"
        f"⚡ Скорость до 1 Гбит/с\n\n"
        f"🔐 Протоколы: VLESS, Trojan, Shadowsocks\n\n"
        f"👇 Выбери тариф и подключайся!"
    )
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"),
        InlineKeyboardButton("🌍 Выбрать страну", callback_data="select_country")
    )
    markup.add(
        InlineKeyboardButton("📱 Как подключиться", callback_data="howto"),
        InlineKeyboardButton("💰 Мой баланс", callback_data="balance")
    )
    
    bot.send_message(
        user_id,
        welcome_text,
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    help_text = (
        "📚 **Доступные команды:**\n\n"
        "/start - Главное меню\n"
        "/buy - Купить подписку\n"
        "/balance - Проверить баланс\n"
        "/my_subs - Мои подписки\n"
        "/howto - Инструкция\n"
        "/support - Поддержка\n\n"
        "💬 Если есть вопросы — пиши @admin"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['balance'])
def cmd_balance(message):
    user_id = message.from_user.id
    balance = get_user_balance(user_id)
    
    text = (
        f"💰 **Ваш баланс**\n\n"
        f"Текущий баланс: `{balance} ₽`\n\n"
        f"Баланс можно пополнить при покупке подписки."
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
    
    bot.send_message(
        user_id,
        text,
        parse_mode='Markdown',
        reply_markup=markup
    )

@bot.message_handler(commands=['my_subs'])
def cmd_my_subs(message):
    user_id = message.from_user.id
    subs = get_user_subscriptions(user_id)
    
    if not subs:
        text = "❌ У вас нет активных подписок"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
        bot.send_message(user_id, text, reply_markup=markup)
        return
    
    text = "📋 **Ваши активные подписки:**\n\n"
    
    for sub in subs:
        country_emoji = '🇳🇱' if sub['country'] == 'nl' else '🇩🇪' if sub['country'] == 'de' else '🇫🇮'
        text += f"{country_emoji} **Подписка #{sub['id']}**\n"
        text += f"📅 Действует до: `{sub['expires_at'][:10]}`\n"
        text += f"🔗 [Скачать конфиг]({sub['config_link']})\n\n"
    
    bot.send_message(user_id, text, parse_mode='Markdown', disable_web_page_preview=True)

@bot.message_handler(commands=['howto'])
def cmd_howto(message):
    howto_text = (
        "📱 **Как подключиться:**\n\n"
        "1️⃣ Скачай приложение:\n"
        "   • Android: [v2rayNG](https://play.google.com/store/apps/details?id=com.v2ray.ang)\n"
        "   • iPhone: [Streisand](https://apps.apple.com/app/streisand/id6450534064)\n"
        "   • Windows: [Nekoray](https://github.com/MatsuriDayo/nekoray/releases)\n"
        "   • Mac: [V2RayX](https://github.com/Cenmrev/V2RayX/releases)\n\n"
        "2️⃣ После оплаты ты получишь ссылку-конфиг\n"
        "3️⃣ Скопируй ссылку и вставь в приложение\n"
        "4️⃣ Нажми подключение — всё!\n\n"
        "❓ Если нужна помощь — @admin"
    )
    bot.send_message(message.chat.id, howto_text, parse_mode='Markdown', disable_web_page_preview=True)

# ================ CALLBACKS ================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data
    
    logger.info(f"🔄 Callback {data} от {user_id}")
    
    if data == "buy":
        markup = InlineKeyboardMarkup(row_width=1)
        
        for key, tariff in TARIFFS.items():
            popular = " 🔥" if tariff['popular'] else ""
            markup.add(InlineKeyboardButton(
                f"{tariff['name']} — {tariff['price']} ₽{popular}",
                callback_data=f"tariff_{key}"
            ))
        
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        
        bot.edit_message_text(
            "📦 **Выберите тариф:**\n\n"
            "• Все тарифы включают безлимитный трафик\n"
            "• Поддержка всех устройств\n"
            "• Скорость до 1 Гбит/с",
            user_id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
    
    elif data == "select_country":
        markup = InlineKeyboardMarkup(row_width=2)
        
        for code, name in COUNTRIES.items():
            markup.add(InlineKeyboardButton(
                name,
                callback_data=f"country_{code}"
            ))
        
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        
        bot.edit_message_text(
            "🌍 **Выберите страну сервера:**\n\n"
            "• Нидерланды — оптимальный баланс\n"
            "• Германия — стабильный канал\n"
            "• Финляндия — низкий пинг\n"
            "• США — западное побережье\n"
            "• Сингапур — Азия",
            user_id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
    
    elif data.startswith("country_"):
        country = data.replace("country_", "")
        country_name = COUNTRIES.get(country, country)
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            UPDATE users SET preferred_country = ? WHERE user_id = ?
        ''', (country, user_id))
        conn.commit()
        conn.close()
        
        bot.answer_callback_query(
            call.id,
            f"✅ Страна {country_name} выбрана",
            show_alert=False
        )
        
        bot.edit_message_text(
            f"✅ Страна {country_name} сохранена как предпочтительная.\n\n"
            f"Теперь при покупке подписки сервер будет в {country_name}.",
            user_id,
            call.message.message_id
        )
    
    elif data == "balance":
        balance = get_user_balance(user_id)
        
        bot.edit_message_text(
            f"💰 **Ваш баланс:** `{balance} ₽`\n\n"
            f"Баланс можно пополнить при покупке подписки.",
            user_id,
            call.message.message_id,
            parse_mode='Markdown'
        )
    
    elif data == "howto":
        howto_text = (
            "📱 **Как подключиться:**\n\n"
            "1️⃣ Скачай приложение:\n"
            "   • Android: v2rayNG\n"
            "   • iPhone: Streisand\n"
            "   • Windows: Nekoray\n\n"
            "2️⃣ После оплаты получи ссылку\n"
            "3️⃣ Вставь ссылку в приложение\n"
            "4️⃣ Подключись!"
        )
        
        bot.edit_message_text(
            howto_text,
            user_id,
            call.message.message_id,
            parse_mode='Markdown'
        )
    
    elif data.startswith("tariff_"):
        tariff_key = data.replace("tariff_", "")
        tariff = TARIFFS.get(tariff_key)
        
        if not tariff:
            return
        
        markup = InlineKeyboardMarkup(row_width=1)
        
        stars_amount = math.ceil(tariff['price'] / STAR_PRICE_RUB)
        markup.add(InlineKeyboardButton(
            f"⭐️ Telegram Stars ({stars_amount} ⭐️ = {tariff['price']} ₽)",
            callback_data=f"pay_stars_{tariff_key}_{stars_amount}"
        ))
        
        if CRYPTOBOT_TOKEN:
            markup.add(InlineKeyboardButton(
                "💲 USDT (CryptoBot)",
                callback_data=f"pay_crypto_{tariff_key}"
            ))
        
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="buy"))
        
        bot.edit_message_text(
            f"📌 **Тариф:** {tariff['name']}\n"
            f"💰 **Сумма:** {tariff['price']} ₽\n"
            f"📆 **Период:** {tariff['days']} дней\n"
            f"🌍 **Страна:** по умолчанию Нидерланды\n\n"
            f"Выберите способ оплаты:",
            user_id,
            call.message.message_id,
            parse_mode='Markdown',
            reply_markup=markup
        )
    
    elif data.startswith("pay_stars_"):
        parts = data.split('_')
        tariff_key = parts[2]
        stars = int(parts[3])
        tariff = TARIFFS.get(tariff_key)
        
        if not tariff:
            return
        
        try:
            prices = [telebot.types.LabeledPrice(
                label=tariff['name'],
                amount=stars * 100
            )]
            
            bot.send_invoice(
                user_id,
                title=f'WhitePrism VPN — {tariff["name"]}',
                description=f'Подписка на {tariff["days"]} дней, безлимитный трафик',
                invoice_payload=f'stars_{tariff_key}_{user_id}',
                provider_token='',
                currency='XTR',
                prices=prices,
                start_parameter='create_invoice_stars'
            )
            
            bot.answer_callback_query(call.id, "✅ Счёт создан")
            
        except Exception as e:
            logger.error(f"Stars payment error: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка создания счёта", show_alert=True)
    
    elif data.startswith("pay_crypto_"):
        tariff_key = data.replace("pay_crypto_", "")
        tariff = TARIFFS.get(tariff_key)
        
        if not tariff or not CRYPTOBOT_TOKEN:
            return
        
        amount_usd = round(tariff['price'] / USDT_PRICE_RUB, 2)
        
        try:
            headers = {'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN}
            payload = {
                'asset': 'USDT',
                'amount': amount_usd,
                'description': f'VPN {tariff["name"]}',
                'payload': f'crypto_{tariff_key}_{user_id}',
                'paid_btn_name': 'openBot',
                'paid_btn_url': 'https://t.me/your_bot'
            }
            
            resp = requests.post(
                'https://pay.crypt.bot/api/createInvoice',
                headers=headers,
                json=payload,
                timeout=15
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get('ok'):
                    invoice = data['result']
                    
                    payment_id = add_payment(
                        user_id,
                        tariff['price'],
                        'USDT',
                        str(invoice['invoice_id']),
                        tariff_key,
                        'pending'
                    )
                    
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton(
                        "💳 Оплатить USDT",
                        url=invoice['pay_url']
                    ))
                    
                    bot.edit_message_text(
                        f"💲 **Оплата USDT**\n\n"
                        f"Сумма: `{amount_usd} USDT`\n"
                        f"Тариф: {tariff['name']}\n\n"
                        f"Нажмите кнопку ниже для оплаты.",
                        user_id,
                        call.message.message_id,
                        parse_mode='Markdown',
                        reply_markup=markup
                    )
                    
                    bot.answer_callback_query(call.id, "✅ Счёт создан")
                else:
                    bot.answer_callback_query(call.id, "❌ Ошибка создания счёта", show_alert=True)
            else:
                bot.answer_callback_query(call.id, "❌ Сервис временно недоступен", show_alert=True)
                
        except Exception as e:
            logger.error(f"CryptoBot error: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка", show_alert=True)
    
    elif data == "start":
        cmd_start(call.message)

# ================ УСПЕШНАЯ ОПЛАТА STARS ================
@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout_handler(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment_handler(message):
    user_id = message.from_user.id
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    logger.info(f"💰 Успешная оплата от {user_id}: {payload}")
    
    if payload.startswith('stars_'):
        parts = payload.split('_')
        tariff_key = parts[1]
        tariff = TARIFFS.get(tariff_key)
        
        if not tariff:
            return
        
        amount_stars = payment.total_amount // 100
        rub_amount = int(amount_stars * STAR_PRICE_RUB)
        
        payment_id = add_payment(
            user_id,
            rub_amount,
            'XTR',
            payment.telegram_payment_charge_id,
            tariff_key,
            'completed'
        )
        
        update_user_balance(user_id, rub_amount)
        
        bot.send_message(
            user_id,
            "⏳ **Создаём ваш VPN-ключ...**\nЭто займёт несколько секунд.",
            parse_mode='Markdown'
        )
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT preferred_country FROM users WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        country = row['preferred_country'] if row and row['preferred_country'] else 'nl'
        conn.close()
        
        subscription = create_vpn_subscription(user_id, tariff['days'], country)
        
        if subscription:
            qr_bio = generate_qr(subscription['config_link'])
            
            success_text = (
                f"✅ **VPN-доступ активирован!**\n\n"
                f"📅 Действует до: {subscription['expires_at'].strftime('%d.%m.%Y')}\n"
                f"🌍 Страна: {COUNTRIES.get(country, country)}\n"
                f"📊 Трафик: безлимит\n\n"
                f"🔗 **Ссылка для подключения:**\n"
                f"`{subscription['config_link']}`\n\n"
                f"📱 **Инструкция:**\n"
                f"1. Скопируйте ссылку\n"
                f"2. Вставьте в приложение v2rayNG/Streisand\n"
                f"3. Подключитесь"
            )
            
            bot.send_photo(
                user_id,
                qr_bio,
                caption=success_text,
                parse_mode='Markdown'
            )
        else:
            bot.send_message(
                user_id,
                "❌ **Ошибка при создании VPN-ключа.**\n"
                "Администратор уже уведомлён. Мы вернём деньги в ближайшее время.",
                parse_mode='Markdown'
            )
            
            logger.error(f"Failed to create VPN for user {user_id}")

# ================ CRYPTOBOT WEBHOOK ================
@app.route('/crypto_webhook', methods=['POST'])
def crypto_webhook_handler():
    if not CRYPTOBOT_TOKEN:
        return 'CryptoBot not configured', 400
    
    try:
        data = request.json
        logger.info(f"🔔 CryptoBot webhook: {data.get('event')}")
        
        if data.get('event') == 'invoice_paid':
            invoice_id = data['payload']['invoice_id']
            payload = data['payload'].get('payload', '')
            
            if complete_payment(str(invoice_id)):
                parts = payload.split('_')
                if len(parts) >= 3 and parts[0] == 'crypto':
                    tariff_key = parts[1]
                    user_id = int(parts[2])
                    tariff = TARIFFS.get(tariff_key)
                    
                    if tariff:
                        update_user_balance(user_id, tariff['price'])
                        
                        bot.send_message(
                            user_id,
                            "✅ **Оплата получена!**\n\n"
                            "⏳ Создаём ваш VPN-ключ...",
                            parse_mode='Markdown'
                        )
                        
                        conn = get_db()
                        cur = conn.cursor()
                        cur.execute('SELECT preferred_country FROM users WHERE user_id = ?', (user_id,))
                        row = cur.fetchone()
                        country = row['preferred_country'] if row and row['preferred_country'] else 'nl'
                        conn.close()
                        
                        subscription = create_vpn_subscription(user_id, tariff['days'], country)
                        
                        if subscription:
                            qr_bio = generate_qr(subscription['config_link'])
                            
                            success_text = (
                                f"✅ **VPN-доступ активирован!**\n\n"
                                f"📅 Действует до: {subscription['expires_at'].strftime('%d.%m.%Y')}\n"
                                f"🌍 Страна: {COUNTRIES.get(country, country)}\n"
                                f"🔗 `{subscription['config_link']}`"
                            )
                            
                            bot.send_photo(
                                user_id,
                                qr_bio,
                                caption=success_text,
                                parse_mode='Markdown'
                            )
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"CryptoBot webhook error: {e}")
        return 'Error', 500

# ================ АДМИН-КОМАНДЫ ================
@bot.message_handler(commands=['admin_stats'])
@admin_only
def admin_stats(message):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('SELECT COUNT(*) FROM users')
    users_count = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM users WHERE last_activity > datetime("now", "-7 days")')
    active_week = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM payments WHERE status="completed"')
    payments_count = cur.fetchone()[0]
    
    cur.execute('SELECT SUM(amount) FROM payments WHERE status="completed"')
    total_revenue = cur.fetchone()[0] or 0
    
    cur.execute('SELECT COUNT(*) FROM subscriptions WHERE status="active"')
    subs_total = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM subscriptions WHERE status="active" AND expires_at > datetime("now")')
    subs_active = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM payments WHERE status="pending"')
    pending_payments = cur.fetchone()[0]
    
    conn.close()
    
    stats_text = (
        f"📊 **СТАТИСТИКА БОТА**\n\n"
        f"👥 **Пользователи:**\n"
        f"├ Всего: {users_count}\n"
        f"└ Активные (7д): {active_week}\n\n"
        f"💰 **Финансы:**\n"
        f"├ Выручка: {total_revenue} ₽\n"
        f"├ Всего платежей: {payments_count}\n"
        f"└ Ожидают: {pending_payments}\n\n"
        f"🔐 **Подписки:**\n"
        f"├ Всего: {subs_total}\n"
        f"└ Активных: {subs_active}"
    )
    
    bot.send_message(message.chat.id, stats_text, parse_mode='Markdown')

@bot.message_handler(commands=['admin_broadcast'])
@admin_only
def admin_broadcast(message):
    text = message.text.replace('/admin_broadcast', '').strip()
    
    if not text:
        bot.reply_to(message, "❌ Использование: /admin_broadcast Текст сообщения")
        return
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM users')
    users = cur.fetchall()
    conn.close()
    
    sent = 0
    failed = 0
    
    bot.reply_to(message, f"📨 Начинаю рассылку {len(users)} пользователям...")
    
    for user in users:
        try:
            bot.send_message(
                user['user_id'],
                f"📢 **Рассылка от администрации**\n\n{text}",
                parse_mode='Markdown'
            )
            sent += 1
        except Exception as e:
            failed += 1
    
    bot.send_message(
        message.chat.id,
        f"✅ Рассылка завершена\n"
        f"├ Успешно: {sent}\n"
        f"└ Ошибок: {failed}"
    )

@bot.message_handler(commands=['admin_add_balance'])
@admin_only
def admin_add_balance(message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "❌ Использование: /admin_add_balance user_id сумма")
            return
        
        user_id = int(parts[1])
        amount = int(parts[2])
        
        update_user_balance(user_id, amount)
        
        bot.reply_to(message, f"✅ Баланс пользователя {user_id} пополнен на {amount} ₽")
        
        try:
            bot.send_message(
                user_id,
                f"💰 **Баланс пополнен**\n\n"
                f"Сумма: +{amount} ₽\n"
                f"Текущий баланс: {get_user_balance(user_id)} ₽",
                parse_mode='Markdown'
            )
        except:
            pass
            
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")

# ================ WEBHOOK ================
@app.route('/webhook', methods=['POST'])
def webhook_handler():
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

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ================ ЗАПУСК ================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8444)
