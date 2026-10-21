import os
import time
import hmac
import hashlib
import threading
import urllib.parse
import sqlite3
import telebot
import requests
import urllib3
from hashlib import sha256
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# КОНФИГ
# ============================================================

BOT_TOKEN = os.getenv('BOT_TOKEN')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
ADMIN_ID = int(os.getenv('ADMIN_ID', '8000630493'))

PAY_BASE_URL = "https://bot-1792590140-8792-maks746395.bothost.tech"

MIN_AMOUNT = 50
MAX_AMOUNT = 50000

DB_PATH = "/app/data/donates.db"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

print("[CONFIG] ============================================", flush=True)
print(f"[CONFIG] BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING!'}", flush=True)
print(f"[CONFIG] YOOMONEY_RECEIVER: {'OK' if YOOMONEY_RECEIVER else 'MISSING'}", flush=True)
print(f"[CONFIG] YOOMONEY_SECRET: {'OK' if YOOMONEY_SECRET else 'MISSING'}", flush=True)
print(f"[CONFIG] ADMIN_ID: {ADMIN_ID}", flush=True)
print("[CONFIG] ============================================", flush=True)


# ============================================================
# БАЗА ДАННЫХ (чтобы комментарий не терялся при перезапуске)
# ============================================================

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute('''CREATE TABLE IF NOT EXISTS donates (
        label TEXT PRIMARY KEY,
        chat_id INTEGER,
        amount INTEGER,
        comment TEXT DEFAULT '',
        paid INTEGER DEFAULT 0,
        created INTEGER
    )''')
    conn.commit()
    conn.close()


init_db()
print("[DB] OK", flush=True)


def save_donate(label, chat_id, amount, comment):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO donates (label, chat_id, amount, comment, paid, created) VALUES (?, ?, ?, ?, 0, ?)",
        (label, chat_id, amount, comment, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_donate(label):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, amount, comment, paid FROM donates WHERE label=?", (label,))
    row = c.fetchone()
    conn.close()
    return row


def mark_donate_paid(label):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE donates SET paid=1 WHERE label=?", (label,))
    conn.commit()
    conn.close()


# ============================================================
# БОТ
# ============================================================

@bot.message_handler(commands=['start'])
def start(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "💰 Отправить донат",
        callback_data="donate"
    ))
    bot.send_message(
        message.chat.id,
        "💎 <b>Поддержать проект</b>\n\n"
        "Нажми кнопку ниже, чтобы отправить донат.\n"
        f"Сумма: от <b>{MIN_AMOUNT} ₽</b> до <b>{MAX_AMOUNT} ₽</b>",
        parse_mode='HTML',
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data == "donate")
def donate_click(call):
    bot.answer_callback_query(call.id)
    sent = bot.send_message(
        call.message.chat.id,
        f"💰 Введи сумму доната (от {MIN_AMOUNT} до {MAX_AMOUNT} ₽):"
    )
    bot.register_next_step_handler(sent, process_amount)


def process_amount(message):
    try:
        amount = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.")
        return

    if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
        bot.send_message(
            message.chat.id,
            f"❌ Сумма должна быть от <b>{MIN_AMOUNT}</b> до <b>{MAX_AMOUNT}</b> ₽.",
            parse_mode='HTML'
        )
        return

    sent = bot.send_message(
        message.chat.id,
        "📝 Теперь напиши комментарий к донату\n"
        "(или отправь <code>-</code> чтобы пропустить):",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(sent, process_comment, amount)


def process_comment(message, amount):
    comment = (message.text or "").strip()
    if comment == "-":
        comment = ""

    if len(comment) > 200:
        bot.send_message(message.chat.id, "❌ Комментарий слишком длинный (макс 200 символов).")
        return

    label = f"DONATE-{message.chat.id}-{int(time.time())}"
    save_donate(label, message.chat.id, amount, comment)

    pay_url = f"{PAY_BASE_URL}/pay/{amount}/{label}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        f"💳 Оплатить {amount} ₽",
        url=pay_url
    ))

    comment_text = f"📝 <b>Комментарий:</b> {comment}" if comment else "📝 <i>Без комментария</i>"

    bot.send_message(
        message.chat.id,
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"      🧾 СЧЁТ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Сумма: <b>{amount} ₽</b>\n"
        f"{comment_text}\n"
        f"🆔 <code>{label}</code>\n\n"
        f"Нажми кнопку ниже 👇\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode='HTML',
        reply_markup=markup
    )


# ============================================================
# ОПЛАТА — редирект на ЮMoney
# ============================================================

@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    return f'''<html><head><meta charset="utf-8"><title>Оплата...</title></head>
    <body onload="document.forms[0].submit()">
    <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
    <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
    <input type="hidden" name="quickpay-form" value="button"/>
    <input type="hidden" name="sum" value="{amount}"/>
    <input type="hidden" name="label" value="{label}"/>
    <input type="hidden" name="paymentType" value="AC"/>
    </form></body></html>'''


# ============================================================
# WEBHOOK
# ============================================================

@app.route('/webhook', methods=['POST'])
def yoomoney_webhook():
    data = request.form.to_dict()
    received_sign = data.pop('sign', '')

    if received_sign:
        sorted_items = sorted(data.items())
        check_string = '&'.join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in sorted_items)
        calculated_sign = hmac.new(
            YOOMONEY_SECRET.encode('utf-8'),
            check_string.encode('utf-8'),
            sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated_sign, received_sign):
            return jsonify({"status": "error"}), 403
    else:
        received_hash = data.pop('sha1_hash', '')
        if received_hash:
            check_string = '&'.join([f"{k}={v}" for k, v in sorted(data.items())]) + YOOMONEY_SECRET
            if hashlib.sha1(check_string.encode('utf-8')).hexdigest() != received_hash:
                return jsonify({"status": "error"}), 403

    label = data.get('label', '')
    amount = data.get('amount', '')

    donate = get_donate(label)
    if donate:
        chat_id, real_amount, comment, paid = donate

        if paid:
            return jsonify({"status": "ok"}), 200

        mark_donate_paid(label)

        comment_text = f"\n📝 Комментарий: <b>{comment}</b>" if comment else "\n📝 <i>Без комментария</i>"

        # Уведомляем админа
        try:
            bot.send_message(
                ADMIN_ID,
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"   💰 <b>НОВЫЙ ДОНАТ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 От: <code>{chat_id}</code>\n"
                f"💵 Сумма: <b>{real_amount} ₽</b>{comment_text}\n"
                f"🆔 Заказ: <code>{label}</code>\n"
                f"📅 {time.strftime('%d.%m.%Y %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Admin notify error: {e}")

        # Благодарим пользователя
        try:
            bot.send_message(
                chat_id,
                f"✅ <b>Спасибо за донат!</b>\n\n"
                f"💵 Сумма: <b>{real_amount} ₽</b>{comment_text}\n"
                f"❤️ Ты лучший!",
                parse_mode='HTML'
            )
        except Exception:
            pass

    return jsonify({"status": "ok"}), 200


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# ============================================================
# ЗАПУСК
# ============================================================

def run_flask():
    port = int(os.getenv('PORT', 3000))
    print(f"[FLASK] Starting on 0.0.0.0:{port}", flush=True)
    try:
        app.run(host='0.0.0.0', port=port, threaded=True, debug=False, use_reloader=False)
    except Exception as e:
        print(f"[FLASK] CRASHED: {e}", flush=True)


def run_bot():
    print("[BOT] Starting polling...", flush=True)
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"[BOT] Polling error: {e}", flush=True)
            time.sleep(5)


threading.Thread(target=run_flask, daemon=True, name="flask").start()
run_bot()
