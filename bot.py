import os, re, time, uuid, hmac, hashlib, threading, urllib.parse, sqlite3
import telebot, requests, urllib3
from hashlib import sha256
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# КОНФИГ
# ============================================================

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '8000630493'))
GIGACHAT_AUTH_KEY = os.getenv('GIGACHAT_AUTH_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')
CHAT_ID = os.getenv('CHAT_ID')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
PAY_BASE_URL = os.getenv('PAY_BASE_URL', 'https://bot-1792590140-8792-maks746395.bothost.tech')

MIN_TOPUP, MAX_TOPUP = 50, 15000
AD_COST = 500
MAX_CHANNEL_POSTS = 5
MIN_PRICE = 50          # минимальная ставка за смену названия/описания
PRICE_STEP_PERCENT = 10 # каждая следующая смена +10% от предыдущей
TEST_BALANCE = 50000000

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
DB_PATH = "/app/data/studio.db"

print(f"[CONFIG] BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING!'}", flush=True)
print(f"[CONFIG] GIGACHAT: {'OK' if GIGACHAT_AUTH_KEY else 'MISSING'}", flush=True)
print(f"[CONFIG] CHANNEL_ID: {CHANNEL_ID or 'MISSING'}", flush=True)
print(f"[CONFIG] CHAT_ID: {CHAT_ID or 'MISSING'}", flush=True)
print(f"[CONFIG] ADMIN_ID: {ADMIN_ID}", flush=True)


# ============================================================
# БАЗА
# ============================================================

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = get_conn(); cur = c.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("""CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
        balance INTEGER DEFAULT 0, reputation INTEGER DEFAULT 0, channel_posts INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0, ban_reason TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, chat_id INTEGER,
        amount INTEGER, paid INTEGER DEFAULT 0, created INTEGER)""")
    # Цены за смену названия/описания для 4 объектов
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (
        target TEXT PRIMARY KEY,
        current_price INTEGER DEFAULT 50,
        last_user INTEGER DEFAULT 0,
        last_value TEXT DEFAULT '',
        updated INTEGER DEFAULT 0
    )""")
    for t in ("channel_name", "channel_desc", "chat_name", "chat_desc"):
        cur.execute("INSERT OR IGNORE INTO prices (target, current_price) VALUES (?, ?)", (t, MIN_PRICE))
    c.commit(); c.close()


init_db()
print("[DB] OK", flush=True)


def grant_admin_test_balance():
    c = get_conn(); cur = c.cursor()
    cur.execute("SELECT balance FROM users WHERE chat_id=?", (ADMIN_ID,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (chat_id, balance, reputation) VALUES (?, ?, 0)", (ADMIN_ID, TEST_BALANCE))
        c.commit()
        print(f"[TEST] ADMIN_ID {ADMIN_ID} создан с балансом {TEST_BALANCE} ₽", flush=True)
    elif row[0] == 0:
        cur.execute("UPDATE users SET balance=? WHERE chat_id=?", (TEST_BALANCE, ADMIN_ID))
        c.commit()
        print(f"[TEST] ADMIN_ID {ADMIN_ID} получил тестовый баланс {TEST_BALANCE} ₽", flush=True)
    else:
        print(f"[TEST] ADMIN_ID {ADMIN_ID} уже имеет баланс {row[0]} ₽", flush=True)
    c.close()


grant_admin_test_balance()


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

def get_user(cid):
    c = get_conn(); cur = c.cursor()
    cur.execute("""SELECT balance, reputation, channel_posts, banned, ban_reason
        FROM users WHERE chat_id=?""", (cid,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (cid,)); c.commit()
        r = (0, 0, 0, 0, '')
    c.close(); return r


def upd(cid, field, val):
    c = get_conn(); c.cursor().execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (val, cid))
    c.commit(); c.close()


def add_bal(cid, amt):
    c = get_conn(); c.cursor().execute("UPDATE users SET balance=balance+? WHERE chat_id=?", (amt, cid))
    c.commit(); c.close()


def get_price(target):
    c = get_conn(); cur = c.cursor()
    cur.execute("SELECT current_price, last_user, last_value FROM prices WHERE target=?", (target,))
    r = cur.fetchone(); c.close()
    return r if r else (MIN_PRICE, 0, '')


def set_price(target, price, user, val):
    c = get_conn(); c.cursor().execute(
        "UPDATE prices SET current_price=?, last_user=?, last_value=?, updated=? WHERE target=?",
        (price, user, val, int(time.time()), target))
    c.commit(); c.close()


# ============================================================
# GIGACHAT (LITE)
# ============================================================

_giga = {"t": None, "e": 0}
_glock = threading.Lock()


def giga_token():
    now = time.time()
    if _giga["t"] and now < _giga["e"]:
        return _giga["t"]
    with _glock:
        try:
            r = requests.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                         "RqUID": str(uuid.uuid4()), "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"},
                data={"scope": "GIGACHAT_API_PERS"}, verify=False, timeout=30)
            t = r.json().get("access_token")
            if t:
                _giga["t"] = t; _giga["e"] = now + 1800; return t
        except Exception as e:
            print(f"[GIGA] {e}")
    return None


def ai_check(text, ctx="channel"):
    if not text:
        return True, "OK"
    if re.search(r'@\w+', text):
        return False, "Ники (@username) запрещены"
    if re.search(r'https?://|t\.me/|www\.', text, re.I):
        return False, "Ссылки запрещены"
    if re.search(r'\b(карт[ау]|кошел[её]к|звезд[ыа]|рубл[ьия]|реквизит|перевод)\b', text, re.I):
        return False, "Реквизиты для оплаты запрещены"
    if ctx in ("title", "description", "channel"):
        for w in ["хуй", "пизд", "ебал", "бляд", "сука", "fuck", "shit"]:
            if w in text.lower():
                return False, "Мат запрещён"
    if ctx in ("ad", "channel", "chat"):
        tok = giga_token()
        if not tok:
            return True, "OK"
        url = "https://api.giga.chat/v1/chat/completions"
        hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}
        prompt = (f"Проверь текст на запрещённое: наркотики, оружие, казино, скам, "
                  f"финансовые пирамиды, экстремизм. Ответь только 'BAD' или 'OK'.\nТекст: {text}")
        data = {"model": "GigaChat", "messages": [{"role": "user", "content": prompt}], "max_tokens": 5}
        bad = 0
        for _ in range(3):
            try:
                a = requests.post(url, headers=hdr, json=data, verify=False, timeout=20).json()
                if "BAD" in a.get("choices", [{}])[0].get("message", {}).get("content", "").upper():
                    bad += 1
            except Exception:
                pass
            time.sleep(0.3)
        if bad >= 1:
            return False, "ИИ нашёл нарушение"
    return True, "OK"


def notify_admin(t):
    try:
        bot.send_message(ADMIN_ID, t, parse_mode='HTML')
    except Exception:
        pass


def ban_user(uid, reason):
    for cid in (CHANNEL_ID, CHAT_ID):
        if not cid:
            continue
        try:
            bot.ban_chat_member(cid, uid, revoke_messages=True)
        except Exception:
            pass
    upd(uid, 'banned', 1); upd(uid, 'ban_reason', reason)


# ============================================================
# ОПЛАТА
# ============================================================

@app.route('/pay/<amount>/<label>')
def pay_page(amount, label):
    return f'''<html><head><meta charset="utf-8"></head><body onload="document.forms[0].submit()">
    <form method="POST" action="https://yoomoney.ru/quickpay/confirm">
    <input type="hidden" name="receiver" value="{YOOMONEY_RECEIVER}"/>
    <input type="hidden" name="quickpay-form" value="button"/>
    <input type="hidden" name="sum" value="{amount}"/>
    <input type="hidden" name="label" value="{label}"/>
    <input type="hidden" name="paymentType" value="AC"/>
    </form></body></html>'''


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.form.to_dict()
    sign = data.pop('sign', '')
    if sign and YOOMONEY_SECRET:
        items = sorted(data.items())
        check = '&'.join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in items)
        calc = hmac.new(YOOMONEY_SECRET.encode(), check.encode(), sha256).hexdigest()
        if not hmac.compare_digest(calc, sign):
            return jsonify({"error": "sign"}), 403
    label = data.get('label', '')
    if label.startswith('TOPUP-'):
        p = label.split('-')
        try:
            cid, amt = int(p[1]), int(p[2])
        except (IndexError, ValueError):
            return jsonify({"ok": True}), 200
        c = get_conn(); cur = c.cursor()
        cur.execute("SELECT paid FROM orders WHERE order_id=?", (label,))
        row = cur.fetchone()
        if row and not row[0]:
            cur.execute("UPDATE orders SET paid=1 WHERE order_id=?", (label,)); c.commit(); c.close()
            add_bal(cid, amt)
            nb = get_user(cid)[0]
            try:
                bot.send_message(cid, f"✅ Баланс пополнен на <b>{amt} ₽</b>\n💎 Баланс: <b>{nb} ₽</b>", parse_mode='HTML')
            except Exception:
                pass
            notify_admin(f"💰 <b>Пополнение</b>\n👤 <code>{cid}</code>\n💵 {amt} ₽\n💎 {nb} ₽")
        else:
            c.close()
    return jsonify({"ok": True}), 200


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# ============================================================
# МЕНЮ (с удалением старого сообщения)
# ============================================================

def main_menu():
    m = telebot.types.InlineKeyboardMarkup()
    m.add(telebot.types.InlineKeyboardButton("💰 Пополнить баланс", callback_data="topup"))
    m.add(telebot.types.InlineKeyboardButton("📺 Управлять каналом", callback_data="channel_menu"))
    m.add(telebot.types.InlineKeyboardButton("💬 Управлять чатом", callback_data="chat_menu"))
    return m


def channel_menu():
    m = telebot.types.InlineKeyboardMarkup()
    m.add(telebot.types.InlineKeyboardButton("✏️ Изменить название", callback_data="ch_name"))
    m.add(telebot.types.InlineKeyboardButton("📝 Изменить описание", callback_data="ch_desc"))
    m.add(telebot.types.InlineKeyboardButton("📢 Опубликовать пост", callback_data="ch_post"))
    m.add(telebot.types.InlineKeyboardButton(f"📣 Реклама ({AD_COST}₽)", callback_data="ch_ad"))
    m.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return m


def chat_menu():
    m = telebot.types.InlineKeyboardMarkup()
    m.add(telebot.types.InlineKeyboardButton("✏️ Изменить название", callback_data="chat_name"))
    m.add(telebot.types.InlineKeyboardButton("📝 Изменить описание", callback_data="chat_desc"))
    m.add(telebot.types.InlineKeyboardButton("⭐ Репутация", callback_data="chat_rep"))
    m.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return m


def back_kb():
    return telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton("⬅️ В меню", callback_data="back_main"))


def delete_prev(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def main_text(cid):
    u = get_user(cid)
    return (f"💎 <b>Studio</b>\n\n💰 Баланс: <b>{u[0]} ₽</b>\n⭐ Репутация: <b>{u[1]}</b>\n\n"
            f"Выбери действие 👇")


@bot.message_handler(commands=['start'])
def start(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if message.from_user.username:
        upd(message.chat.id, 'username', f"@{message.from_user.username}")
    bot.send_message(message.chat.id, main_text(message.chat.id),
                     parse_mode='HTML', reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back(c):
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text(main_text(c.message.chat.id), chat_id=c.message.chat.id,
                              message_id=c.message.message_id, parse_mode='HTML', reply_markup=main_menu())
    except Exception:
        pass


# ============================================================
# ПОПОЛНЕНИЕ
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data == "topup")
def cb_topup(c):
    bot.answer_callback_query(c.id)
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, f"💰 Введи сумму (от {MIN_TOPUP} до {MAX_TOPUP} ₽):")
    bot.register_next_step_handler(s, topup_amt)


def topup_amt(message):
    try:
        amt = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=back_kb()); return
    if not (MIN_TOPUP <= amt <= MAX_TOPUP):
        bot.send_message(message.chat.id, f"❌ От {MIN_TOPUP} до {MAX_TOPUP} ₽.", reply_markup=back_kb()); return
    label = f"TOPUP-{message.chat.id}-{amt}-{int(time.time())}"
    c = get_conn(); c.cursor().execute("INSERT INTO orders VALUES (?, ?, ?, 0, ?)",
        (label, message.chat.id, amt, int(time.time())))
    c.commit(); c.close()
    m = telebot.types.InlineKeyboardMarkup()
    m.add(telebot.types.InlineKeyboardButton(f"💳 Оплатить {amt} ₽", url=f"{PAY_BASE_URL}/pay/{amt}/{label}"))
    m.add(telebot.types.InlineKeyboardButton("⬅️ В меню", callback_data="back_main"))
    bot.send_message(message.chat.id, f"🧾 <b>Счёт</b>\n\n💵 <b>{amt} ₽</b>\n🆔 <code>{label}</code>",
        parse_mode='HTML', reply_markup=m)


# ============================================================
# МЕНЮ КАНАЛА/ЧАТА
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data == "channel_menu")
def cb_chmenu(c):
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text("📺 <b>Управление каналом</b>", chat_id=c.message.chat.id,
            message_id=c.message.message_id, parse_mode='HTML', reply_markup=channel_menu())
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "chat_menu")
def cb_chatmenu(c):
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text("💬 <b>Управление чатом</b>", chat_id=c.message.chat.id,
            message_id=c.message.message_id, parse_mode='HTML', reply_markup=chat_menu())
    except Exception:
        pass


# ============================================================
# СМЕНА НАЗВАНИЯ / ОПИСАНИЯ (фикс цена +10%)
# ============================================================

def calc_min_price(target):
    """Минимальная цена для следующей смены: 50 при первой, иначе +10% от прошлой."""
    price, last_user, last_value = get_price(target)
    if not last_value:
        return MIN_PRICE
    return int(price * (1 + PRICE_STEP_PERCENT / 100.0))


def do_change(message, target, kind):
    """kind: 'title' или 'description'"""
    new = (message.text or "").strip()
    if kind == 'title' and not (1 <= len(new) <= 128):
        bot.send_message(message.chat.id, "❌ 1–128 символов.", reply_markup=back_kb()); return
    if kind == 'description' and not (1 <= len(new) <= 255):
        bot.send_message(message.chat.id, "❌ 1–255 символов.", reply_markup=back_kb()); return

    ctx = 'title' if kind == 'title' else 'description'
    ok, r = ai_check(new, ctx)
    if not ok:
        bot.send_message(message.chat.id, f"❌ ИИ: {r}", reply_markup=back_kb()); return

    min_price = calc_min_price(target)
    balance = get_user(message.chat.id)[0]

    if balance < min_price:
        bot.send_message(message.chat.id,
            f"❌ <b>Недостаточно средств!</b>\n\n"
            f"Минимальная ставка: <b>{min_price} ₽</b>\n"
            f"У тебя на балансе: <b>{balance} ₽</b>\n\n"
            f"Пополни баланс и попробуй снова.",
            parse_mode='HTML', reply_markup=back_kb())
        return

    # Спрашиваем, сколько он хочет заплатить (не меньше min_price)
    sent = bot.send_message(message.chat.id,
        f"💰 Минимальная ставка: <b>{min_price} ₽</b>\n"
        f"Баланс: <b>{balance} ₽</b>\n\n"
        f"Введи сумму, которую готов заплатить (не меньше {min_price} ₽):",
        parse_mode='HTML')
    bot.register_next_step_handler(sent, lambda m: do_pay_and_change(m, target, kind, new, min_price))


def do_pay_and_change(message, target, kind, new, min_price):
    try:
        pay = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.", reply_markup=back_kb()); return

    if pay < min_price:
        bot.send_message(message.chat.id,
            f"❌ Ставка должна быть не меньше <b>{min_price} ₽</b>.",
            parse_mode='HTML', reply_markup=back_kb()); return

    balance = get_user(message.chat.id)[0]
    if balance < pay:
        bot.send_message(message.chat.id,
            f"❌ Не хватает <b>{pay - balance} ₽</b>.\n"
            f"Баланс: <b>{balance} ₽</b>, нужно: <b>{pay} ₽</b>.",
            parse_mode='HTML', reply_markup=back_kb()); return

    add_bal(message.chat.id, -pay)
    set_price(target, pay, message.chat.id, new)

    try:
        if target == "channel_name":
            bot.set_chat_title(CHANNEL_ID, new)
        elif target == "channel_desc":
            bot.set_chat_description(CHANNEL_ID, new)
        elif target == "chat_name":
            bot.set_chat_title(CHAT_ID, new)
        elif target == "chat_desc":
            bot.set_chat_description(CHAT_ID, new)
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ {e}", reply_markup=back_kb()); return

    next_price = int(pay * 1.1)
    bot.send_message(message.chat.id,
        f"✅ Изменено!\n\n"
        f"📝 Новое значение: <b>{new}</b>\n"
        f"💸 Заплачено: <b>{pay} ₽</b>\n"
        f"💰 Остаток: <b>{get_user(message.chat.id)[0]} ₽</b>\n\n"
        f"Следующая смена будет стоить от <b>{next_price} ₽</b>.",
        parse_mode='HTML', reply_markup=back_kb())
    notify_admin(f"✏️ <b>Смена {target}</b>\n👤 <code>{message.chat.id}</code>\n💵 {pay} ₽\n📝 {new}")


@bot.callback_query_handler(func=lambda c: c.data == "ch_name")
def cb_chname(c):
    bot.answer_callback_query(c.id)
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, f"✏️ Новое название канала (мин. {calc_min_price('channel_name')} ₽):")
    bot.register_next_step_handler(s, lambda m: do_change(m, "channel_name", "title"))


@bot.callback_query_handler(func=lambda c: c.data == "ch_desc")
def cb_chdesc(c):
    bot.answer_callback_query(c.id)
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, f"📝 Новое описание канала (мин. {calc_min_price('channel_desc')} ₽):")
    bot.register_next_step_handler(s, lambda m: do_change(m, "channel_desc", "description"))


@bot.callback_query_handler(func=lambda c: c.data == "chat_name")
def cb_chname2(c):
    bot.answer_callback_query(c.id)
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, f"✏️ Новое название чата (мин. {calc_min_price('chat_name')} ₽):")
    bot.register_next_step_handler(s, lambda m: do_change(m, "chat_name", "title"))


@bot.callback_query_handler(func=lambda c: c.data == "chat_desc")
def cb_chdesc2(c):
    bot.answer_callback_query(c.id)
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, f"📝 Новое описание чата (мин. {calc_min_price('chat_desc')} ₽):")
    bot.register_next_step_handler(s, lambda m: do_change(m, "chat_desc", "description"))


# ============================================================
# ПОСТЫ И РЕКЛАМА
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data == "ch_post")
def cb_chpost(c):
    bot.answer_callback_query(c.id)
    if get_user(c.message.chat.id)[2] >= MAX_CHANNEL_POSTS:
        bot.send_message(c.message.chat.id, f"❌ Лимит {MAX_CHANNEL_POSTS} постов.", reply_markup=back_kb()); return
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id, "📢 Текст поста (ИИ проверит):")
    bot.register_next_step_handler(s, chpost_step)


def chpost_step(message):
    t = (message.text or "").strip()
    if not t:
        bot.send_message(message.chat.id, "❌ Пусто.", reply_markup=back_kb()); return
    ok, r = ai_check(t, "channel")
    if not ok:
        bot.send_message(message.chat.id, f"❌ ИИ: {r}", reply_markup=back_kb()); return
    try:
        bot.send_message(CHANNEL_ID, t)
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ {e}", reply_markup=back_kb()); return
    c = get_conn(); c.cursor().execute("UPDATE users SET channel_posts=channel_posts+1 WHERE chat_id=?", (message.chat.id,))
    c.commit(); c.close()
    u = get_user(message.chat.id)
    bot.send_message(message.chat.id, f"✅ Опубликовано. Постов: <b>{u[2]}/{MAX_CHANNEL_POSTS}</b>",
        parse_mode='HTML', reply_markup=back_kb())
    notify_admin(f"📢 <b>Пост</b>\n👤 <code>{message.chat.id}</code>\n\n{t[:200]}")


@bot.callback_query_handler(func=lambda c: c.data == "ch_ad")
def cb_chad(c):
    bot.answer_callback_query(c.id)
    if get_user(c.message.chat.id)[0] < AD_COST:
        bot.send_message(c.message.chat.id, f"❌ Нужно {AD_COST} ₽.", reply_markup=back_kb()); return
    delete_prev(c.message.chat.id, c.message.message_id)
    s = bot.send_message(c.message.chat.id,
        f"📣 Отправь текст рекламы или фото с подписью ({AD_COST} ₽, ИИ проверит):")
    bot.register_next_step_handler(s, chad_step)


def chad_step(message):
    text = (message.text or message.caption or "").strip()
    if not text:
        bot.send_message(message.chat.id, "❌ Нужен текст или подпись к фото.", reply_markup=back_kb()); return
    ok, r = ai_check(text, "ad")
    if not ok:
        bot.send_message(message.chat.id, f"❌ ИИ: {r}", reply_markup=back_kb()); return
    if get_user(message.chat.id)[0] < AD_COST:
        bot.send_message(message.chat.id, "❌ Мало средств.", reply_markup=back_kb()); return
    add_bal(message.chat.id, -AD_COST)
    try:
        if message.photo:
            photo = message.photo[-1].file_id
            bot.send_photo(CHANNEL_ID, photo, caption=f"📣 <b>Реклама</b>\n\n{text}", parse_mode='HTML')
        else:
            bot.send_message(CHANNEL_ID, f"📣 <b>Реклама</b>\n\n{text}", parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ {e}", reply_markup=back_kb()); return
    bot.send_message(message.chat.id, f"✅ Реклама опубликована за {AD_COST} ₽.", reply_markup=back_kb())
    notify_admin(f"📣 <b>Реклама</b>\n👤 <code>{message.chat.id}</code>\n💵 {AD_COST} ₽\n\n{text[:200]}")


@bot.callback_query_handler(func=lambda c: c.data == "chat_rep")
def cb_rep(c):
    bot.answer_callback_query(c.id)
    u = get_user(c.message.chat.id)
    free = u[1] // 10
    bot.send_message(c.message.chat.id,
        f"⭐ <b>Репутация</b>\n\nТекущая: <b>{u[1]}</b>\nБесплатных постов в чат: <b>{free}</b>\n\n"
        f"Репутация растёт за активность.",
        parse_mode='HTML', reply_markup=back_kb())


# ============================================================
# МОДЕРАЦИЯ
# ============================================================

@bot.channel_post_handler(func=lambda m: True)
def on_channel_post(m):
    if not m.text:
        return
    uid = m.from_user.id if m.from_user else 0
    ok, r = ai_check(m.text, "channel")
    if not ok:
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        if uid:
            ban_user(uid, r)
            notify_admin(f"🚫 <b>Бан в канале</b>\n👤 <code>{uid}</code>\nПричина: {r}")


@bot.message_handler(content_types=['text'], func=lambda m: m.chat.id == int(CHAT_ID or 0))
def on_chat_msg(m):
    uid = m.from_user.id
    if get_user(uid)[3]:
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        return
    ok, r = ai_check(m.text, "chat")
    if not ok:
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        notify_admin(f"⚠️ Удалено в чате\n👤 <code>{uid}</code>\n{r}\n{m.text[:150]}")


@bot.chat_join_request_handler(func=lambda r: True)
def on_join(r):
    try:
        bot.approve_chat_join_request(r.chat.id, r.user.id)
    except Exception as e:
        print(f"[JOIN] {e}")
    uid = r.user.id
    name = f"{r.user.first_name or ''} {r.user.last_name or ''}".strip()
    uname = f"@{r.user.username}" if r.user.username else ""
    ok, reason = ai_check(f"{name} {uname}", "title")
    if not ok:
        ban_user(uid, reason)
        notify_admin(f"🚫 Заявка отклонена и бан\n👤 <code>{uid}</code> {uname}\n{reason}")


# ============================================================
# АДМИН-КОМАНДЫ
# ============================================================

@bot.message_handler(commands=['bonus'])
def cmd_bonus(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Использование: /bonus <chat_id> <сумма>"); return
    try:
        cid, amt = int(parts[1]), int(parts[2])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверные числа"); return
    add_bal(cid, amt)
    bot.send_message(message.chat.id, f"✅ Бонус {amt} ₽ выдан {cid}")


@bot.message_handler(commands=['rep'])
def cmd_rep(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Использование: /rep <chat_id> <кол-во>"); return
    try:
        cid, amt = int(parts[1]), int(parts[2])
    except ValueError:
        return
    c = get_conn(); c.cursor().execute("UPDATE users SET reputation=reputation+? WHERE chat_id=?", (amt, cid))
    c.commit(); c.close()
    bot.send_message(message.chat.id, f"✅ +{amt} репутации {cid}")


# ============================================================
# ЗАПУСК
# ============================================================

def run_flask():
    port = int(os.getenv('PORT', 3000))
    print(f"[FLASK] 0.0.0.0:{port}", flush=True)
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False, use_reloader=False)


def run_bot():
    print("[BOT] polling...", flush=True)
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"[BOT] {e}", flush=True); time.sleep(5)


threading.Thread(target=run_flask, daemon=True).start()
run_bot()
