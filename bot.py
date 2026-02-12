import os
import sys
import logging
import sqlite3
import json
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
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

# ================ MARZBAN ================
MARZBAN_URL = os.getenv('MARZBAN_URL', 'http://localhost:8443')
MARZBAN_USER = os.getenv('MARZBAN_USER', 'admin')
MARZBAN_PASS = os.getenv('MARZBAN_PASS', '')

# ================ ВНЕШНИЙ URL ПАНЕЛИ (для подписки) ================
MARZBAN_EXTERNAL_URL = os.getenv('MARZBAN_EXTERNAL_URL', '')
if not MARZBAN_EXTERNAL_URL:
    logger.warning("⚠️ MARZBAN_EXTERNAL_URL не задан, subscription_url может быть недоступен!")

# ================ CRYPTOBOT ================
CRYPTOBOT_TOKEN = os.getenv('CRYPTOBOT_TOKEN', '')

# ================ КОНСТАНТЫ ================
USDT_PRICE_RUB = 90

TARIFFS = {
    'month': {
        'name': '1 месяц',
        'price_rub': 199,
        'price_stars': 120,
        'days': 30,
        'popular': True
    },
    'quarter': {
        'name': '3 месяца',
        'price_rub': 499,
        'price_stars': 300,
        'days': 90,
        'popular': False
    },
    'year': {
        'name': '1 год',
        'price_rub': 1499,
        'price_stars': 900,
        'days': 365,
        'popular': False
    }
}

SERVER_COUNTRY = {
    'code': 'de',
    'name': '🇩🇪 Германия (Франкфурт)',
    'flag': '🇩🇪'
}

VLESS_INBOUND_TAG = "VLESS TCP"  # Убедись, что совпадает с твоим inbound!

# ================ FLASK ================
app = Flask(__name__)

# ================ TELEGRAM BOT ================
bot = telebot.TeleBot(BOT_TOKEN)

# ================ БАЗА ДАННЫХ ================
def get_db():
    if os.environ.get('RENDER'):
        db_path = '/tmp/mer.db'
    else:
        db_path = 'mer.db'
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
            first_name TEXT,
            last_name TEXT,
            balance INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP
        );
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
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            marzban_username TEXT UNIQUE,
            subscription_url TEXT,
            country TEXT DEFAULT 'de',
            expires_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

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
                data={'username': self.username, 'password': self.password},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                self.token = data['access_token']
                self.token_expiry = datetime.now() + timedelta(hours=1)
                return self.token
            else:
                logger.error(f"Marzban auth failed: {resp.status_code} - {resp.text}")
                return None
        except Exception as e:
            logger.error(f"Marzban connection error: {e}")
            return None

    def create_user(self, user_id, days):
        token = self._auth()
        if not token:
            logger.error("❌ Не удалось получить токен Marzban")
            return None, None

        headers = {'Authorization': f'Bearer {token}'}
        expire = int((datetime.now() + timedelta(days=days)).timestamp())
        
        # ✅ УНИКАЛЬНОЕ ИМЯ: user_{user_id}_{days}_{timestamp}
        timestamp = int(datetime.now().timestamp())
        username = f"user_{user_id}_{days}_{timestamp}"

        user_data = {
            'username': username,
            'proxies': {'vless': {}},
            'inbounds': {
                'vless': [VLESS_INBOUND_TAG]
            },
            'expire': expire,
            'data_limit': 0,
            'status': 'active'
        }

        logger.info(f"📤 Отправка запроса в Marzban: {json.dumps(user_data)}")
        try:
            resp = requests.post(
                f'{self.base_url}/api/user',
                headers=headers,
                json=user_data,
                timeout=10
            )
            logger.info(f"📦 Marzban create user status: {resp.status_code}")
            logger.info(f"📦 Marzban create user response: {resp.text[:500]}")
            if resp.status_code == 200:
                data = resp.json()
                sub_url = data.get('subscription_url', '')
                if sub_url:
                    if sub_url.startswith('/'):
                        if MARZBAN_EXTERNAL_URL:
                            sub_url = MARZBAN_EXTERNAL_URL.rstrip('/') + sub_url
                        else:
                            sub_url = self.base_url + sub_url
                            logger.warning("⚠️ MARZBAN_EXTERNAL_URL не задан, subscription_url может быть недоступен!")
                    logger.info(f"✅ Получена подписка: {sub_url}")
                    return username, sub_url
                else:
                    logger.error("❌ В ответе нет subscription_url")
                    return None, None
            else:
                logger.error(f"❌ Ошибка Marzban: {resp.status_code} - {resp.text}")
                return None, None
        except Exception as e:
            logger.error(f"❌ Ошибка создания пользователя Marzban: {e}")
            return None, None

marzban = MarzbanAPI(MARZBAN_URL, MARZBAN_USER, MARZBAN_PASS)

# ================ ФУНКЦИИ РАБОТЫ С БАЛАНСОМ ================
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

def deduct_user_balance(user_id, amount):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    if not row or row['balance'] < amount:
        conn.close()
        return False
    cur.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return True

# ================ ФУНКЦИИ ПЛАТЕЖЕЙ ================
def add_payment(user_id, amount, currency, payment_id, tariff, status='pending'):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO payments (user_id, amount, currency, payment_id, tariff, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, amount, currency, str(payment_id), tariff, status))
    conn.commit()
    return cur.lastrowid

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

def verify_payment(payment_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT status FROM payments WHERE payment_id = ?', (str(payment_id),))
    row = cur.fetchone()
    conn.close()
    if row and row['status'] == 'completed':
        return False
    return True

# ================ ФУНКЦИИ VPN ================
def create_vpn_subscription(user_id, days):
    marzban_username, subscription_url = marzban.create_user(user_id, days)
    if not subscription_url:
        logger.error(f"❌ Не удалось создать VPN для user {user_id}")
        return None
    
    try:
        conn = get_db()
        cur = conn.cursor()
        # INSERT OR REPLACE — если запись уже есть (но username теперь уникальный, конфликта не будет)
        cur.execute('''
            INSERT OR REPLACE INTO subscriptions 
            (user_id, marzban_username, subscription_url, country, expires_at, status)
            VALUES (?, ?, ?, ?, ?, 'active')
        ''', (
            user_id,
            marzban_username,
            subscription_url,
            'de',
            (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
        conn.close()
        logger.info(f"✅ Подписка сохранена/обновлена в БД для user {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения подписки в БД: {e}")
        return None
    
    return {
        'username': marzban_username,
        'subscription_url': subscription_url,
        'expires_at': datetime.now() + timedelta(days=days),
        'country': SERVER_COUNTRY['name']
    }

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
    logger.info(f"🚀 /start от {user_id}")
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_activity)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, username, first_name))
    cur.execute('UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    balance = get_user_balance(user_id)
    welcome_text = (
        f"👋 Привет, {first_name or 'друг'}!\n\n"
        f"🚀 **MER VPN** — быстрый и стабильный VPN\n"
        f"🌍 **Сервер:** {SERVER_COUNTRY['name']}\n"
        f"💰 **Твой баланс:** `{balance} ₽`\n\n"
        f"👇 Выбери действие:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"),
        InlineKeyboardButton("💰 Баланс", callback_data="balance")
    )
    markup.add(
        InlineKeyboardButton("📱 Мои подписки", callback_data="my_subs"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data="help")
    )
    bot.send_message(user_id, welcome_text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['help'])
def cmd_help(message):
    help_text = (
        "📚 **Доступные команды:**\n\n"
        "/start - Главное меню\n"
        "/balance - Проверить баланс\n"
        "/my_subs - Мои подписки\n\n"
        "💬 По всем вопросам: @admin"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['balance'])
def cmd_balance(message):
    user_id = message.from_user.id
    balance = get_user_balance(user_id)
    text = f"💰 **Твой баланс:** `{balance} ₽`"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
    bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['my_subs'])
def cmd_my_subs(message):
    user_id = message.from_user.id
    subs = get_user_subscriptions(user_id)
    if not subs:
        text = "❌ У тебя нет активных подписок"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
        bot.send_message(user_id, text, reply_markup=markup)
        return
    text = "📋 **Твои подписки:**\n\n"
    for sub in subs:
        text += f"🌍 {SERVER_COUNTRY['name']}\n"
        text += f"📅 Действует до: {sub['expires_at'][:10]}\n"
        text += f"🔗 [Ссылка на подписку]({sub['subscription_url']})\n\n"
    bot.send_message(user_id, text, parse_mode='Markdown', disable_web_page_preview=True)

# ================ CALLBACKS ================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"🔄 Callback: {data} от {user_id}")

    if data == "buy":
        balance = get_user_balance(user_id)
        text = f"📦 **Выбери тариф:**\n\n💰 Твой баланс: `{balance} ₽`\n\n"
        markup = InlineKeyboardMarkup(row_width=1)
        for key, tariff in TARIFFS.items():
            popular = " 🔥" if tariff.get('popular') else ""
            can_afford = balance >= tariff['price_rub']
            emoji = "✅" if can_afford else "⚡"
            markup.add(InlineKeyboardButton(
                f"{emoji} {tariff['name']} — {tariff['price_rub']} ₽{popular}",
                callback_data=f"tariff_{key}"
            ))
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

    elif data.startswith("tariff_"):
        tariff_key = data.split('_')[1]
        tariff = TARIFFS.get(tariff_key)
        if not tariff:
            return
        balance = get_user_balance(user_id)
        if balance >= tariff['price_rub']:
            bot.answer_callback_query(call.id, "✅ Оплачено с баланса")
            if not deduct_user_balance(user_id, tariff['price_rub']):
                bot.answer_callback_query(call.id, "❌ Ошибка списания", show_alert=True)
                return
            bot.edit_message_text(
                "⏳ **Создаём VPN-подписку...**\nЭто займёт несколько секунд.",
                user_id, call.message.message_id, parse_mode='Markdown'
            )
            subscription = create_vpn_subscription(user_id, tariff['days'])
            if subscription:
                logger.info(f"🚀 БЛОК ОТПРАВКИ: subscription получен, пробуем отправить...")
                logger.info(f"📎 subscription_url = {subscription['subscription_url']}")
                # HTML-версия (надёжнее, не ломается от спецсимволов)
                text_html = (
                    f"✅ <b>VPN подписка активирована!</b>\n\n"
                    f"📅 Действует до: {subscription['expires_at'].strftime('%d.%m.%Y')}\n"
                    f"🌍 Страна: {subscription['country']}\n\n"
                    f"🔗 <b>Ссылка на подписку:</b>\n"
                    f"<code>{subscription['subscription_url']}</code>\n\n"
                    f"➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
                    f"📱 <b>Как подключиться:</b>\n\n"
                    f"1️⃣ Скачай приложение:\n"
                    f"   • Android: <a href='https://play.google.com/store/apps/details?id=com.v2ray.ang'>v2rayNG</a>\n"
                    f"   • iOS: <a href='https://apps.apple.com/app/streisand/id6450534064'>Streisand</a>\n"
                    f"   • Windows: <a href='https://github.com/MatsuriDayo/nekoray/releases'>Nekoray</a>\n"
                    f"   • macOS: <a href='https://github.com/Cenmrev/V2RayX/releases'>V2RayX</a>\n\n"
                    f"2️⃣ В приложении выбери <b>«Добавить подписку»</b> или <b>«URL подписки»</b>\n"
                    f"3️⃣ Вставь ссылку из сообщения выше\n"
                    f"4️⃣ Нажми подключение — всё! 🔥"
                )
                try:
                    bot.send_message(user_id, text_html, parse_mode='HTML', disable_web_page_preview=True)
                    logger.info(f"✅ Сообщение с подпиской отправлено пользователю {user_id}")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения пользователю {user_id}: {e}")
                    # fallback на обычный текст без разметки
                    text_fallback = (
                        f"✅ VPN подписка активирована!\n\n"
                        f"📅 Действует до: {subscription['expires_at'].strftime('%d.%m.%Y')}\n"
                        f"🌍 Страна: {subscription['country']}\n\n"
                        f"🔗 Ссылка на подписку:\n{subscription['subscription_url']}\n\n"
                        f"Инструкция по подключению — смотрите в меню /help."
                    )
                    bot.send_message(user_id, text_fallback)
                    logger.info(f"✅ Fallback-сообщение отправлено пользователю {user_id}")
            else:
                update_user_balance(user_id, tariff['price_rub'])
                bot.send_message(user_id, "❌ Ошибка создания VPN. Деньги возвращены на баланс.")
            return
        # Не хватает баланса
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton(
            f"⭐️ Пополнить {tariff['price_stars']} Stars",
            callback_data=f"pay_stars_{tariff_key}"
        ))
        if CRYPTOBOT_TOKEN:
            markup.add(InlineKeyboardButton(
                '💲 USDT (CryptoBot)',
                callback_data=f'pay_crypto_{tariff_key}'
            ))
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="buy"))
        bot.edit_message_text(
            f"📌 **Тариф:** {tariff['name']}\n"
            f"💰 **Стоимость:** {tariff['price_rub']} ₽\n"
            f"💳 **Твой баланс:** {balance} ₽\n"
            f"❌ **Не хватает:** {tariff['price_rub'] - balance} ₽\n\n"
            f"Выбери способ оплаты:",
            user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup
        )

    elif data.startswith("pay_stars_"):
        tariff_key = data.split('_')[2]
        tariff = TARIFFS.get(tariff_key)
        if not tariff:
            return
        try:
            stars = tariff['price_stars']
            prices = [telebot.types.LabeledPrice(label=tariff['name'], amount=stars * 100)]
            bot.send_invoice(
                user_id,
                title=f'MER VPN — {tariff["name"]}',
                description=f'Подписка на {tariff["days"]} дней',
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
        amount_usd = round(tariff['price_rub'] / USDT_PRICE_RUB, 2)
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
            resp = requests.post('https://pay.crypt.bot/api/createInvoice', headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('ok'):
                    invoice = data['result']
                    add_payment(user_id, tariff['price_rub'], 'USDT', str(invoice['invoice_id']), tariff_key, 'pending')
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("💳 Оплатить USDT", url=invoice['pay_url']))
                    bot.edit_message_text(
                        f"💲 **Оплата USDT**\n\nСумма: `{amount_usd} USDT`\nТариф: {tariff['name']}\n\nНажми кнопку ниже для оплаты.",
                        user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup
                    )
                    bot.answer_callback_query(call.id, "✅ Счёт создан")
                else:
                    bot.answer_callback_query(call.id, "❌ Ошибка создания счёта", show_alert=True)
            else:
                bot.answer_callback_query(call.id, "❌ Сервис временно недоступен", show_alert=True)
        except Exception as e:
            logger.error(f"CryptoBot error: {e}")
            bot.answer_callback_query(call.id, "❌ Ошибка", show_alert=True)

    elif data == "balance":
        balance = get_user_balance(user_id)
        text = f"💰 **Твой баланс:** `{balance} ₽`"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

    elif data == "my_subs":
        subs = get_user_subscriptions(user_id)
        if not subs:
            text = "❌ У тебя нет активных подписок"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🛒 Купить подписку", callback_data="buy"))
            markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
            bot.edit_message_text(text, user_id, call.message.message_id, reply_markup=markup)
            return
        text = "📋 **Твои подписки:**\n\n"
        for sub in subs:
            text += f"🌍 {SERVER_COUNTRY['name']}\n"
            text += f"📅 До: {sub['expires_at'][:10]}\n"
            text += f"🔗 [Ссылка на подписку]({sub['subscription_url']})\n\n"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)

    elif data == "help":
        help_text = (
            "📚 **Помощь**\n\n"
            "1. Пополни баланс или оплати тариф звёздами/USDT.\n"
            "2. После оплаты ты получишь ссылку на подписку.\n"
            "3. Вставь эту ссылку в приложение (v2rayNG, Streisand, Nekoray) как URL подписки.\n"
            "4. Подключение произойдёт автоматически!\n\n"
        )
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="start"))
        bot.edit_message_text(help_text, user_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

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
    logger.info(f"💰 Успешная оплата Stars от {user_id}, payload: {payload}")
    if not payload.startswith('stars_'):
        return
    if not verify_payment(payment.telegram_payment_charge_id):
        bot.send_message(user_id, "⚠️ Этот платёж уже был обработан.")
        return
    parts = payload.split('_')
    if len(parts) < 3:
        return
    tariff_key = parts[1]
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        return
    stars_amount = payment.total_amount // 100
    if stars_amount != tariff['price_stars']:
        logger.warning(f"⚠️ Неверная сумма звёзд: {stars_amount} вместо {tariff['price_stars']}")
    rub_amount = tariff['price_rub']
    add_payment(user_id, rub_amount, 'XTR', payment.telegram_payment_charge_id, tariff_key, 'completed')
    update_user_balance(user_id, rub_amount)
    bot.send_message(
        user_id,
        f"✅ Баланс пополнен на {rub_amount} ₽\nТеперь ты можешь купить подписку.",
        parse_mode='Markdown'
    )

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
            if not verify_payment(str(invoice_id)):
                logger.info(f"Платёж {invoice_id} уже обработан")
                return 'OK', 200
            if complete_payment(str(invoice_id)):
                parts = payload.split('_')
                if len(parts) >= 3 and parts[0] == 'crypto':
                    tariff_key = parts[1]
                    user_id = int(parts[2])
                    tariff = TARIFFS.get(tariff_key)
                    if tariff:
                        update_user_balance(user_id, tariff['price_rub'])
                        bot.send_message(
                            user_id,
                            f"✅ Баланс пополнен на {tariff['price_rub']} ₽ через USDT!\nТеперь ты можешь купить подписку.",
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
    conn.close()
    stats_text = (
        f"📊 **СТАТИСТИКА MER VPN**\n\n"
        f"👥 **Пользователи:**\n"
        f"├ Всего: {users_count}\n"
        f"└ Активные (7д): {active_week}\n\n"
        f"💰 **Финансы:**\n"
        f"├ Выручка: {total_revenue} ₽\n"
        f"└ Всего платежей: {payments_count}\n\n"
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
            bot.send_message(user['user_id'], f"📢 **Рассылка от администрации**\n\n{text}", parse_mode='Markdown')
            sent += 1
        except:
            failed += 1
    bot.send_message(message.chat.id, f"✅ Рассылка завершена\n├ Успешно: {sent}\n└ Ошибок: {failed}")

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
                f"💰 **Баланс пополнен**\n\nСумма: +{amount} ₽\nТекущий баланс: {get_user_balance(user_id)} ₽\n\nИспользуй /start для обновления.",
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
    return 'MER VPN Bot is running!'

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ================ ЗАПУСК ================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8444))
    app.run(host='0.0.0.0', port=port)
