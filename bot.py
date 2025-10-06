import os, hmac, hashlib, secrets, asyncio, aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice, CallbackQuery
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

DB_PATH = os.getenv("DB_PATH", "/data/bank.sqlite")  # persistent on Railway volume
MIN_BET, MAX_BET = 100, 5000
MULT_SIDE = 1.75
MULT_EDGE = 8.0

# Probabilities: 49.5% / 49.5% / 1%
P_HEADS = 0.495
P_EDGE  = 0.010  # interval after heads; tails is the rest

# in-memory choice cache (MVP)
pending_choice = {}  # user_id -> 'heads' or 'tails'

async def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, side TEXT, stake INTEGER,
            server_seed TEXT, client_seed TEXT, nonce INTEGER,
            commit_hash TEXT, outcome TEXT, payout INTEGER,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()

async def get_balance(uid:int)->int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (uid,))
            await db.commit()
            return 0
        return row[0]

async def add_balance(uid:int, delta:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(user_id,balance) VALUES(?,0) ON CONFLICT(user_id) DO NOTHING", (uid,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, uid))
        await db.commit()

def make_commit(server_seed:str)->str:
    return hashlib.sha256(server_seed.encode()).hexdigest()

def rng_roll(server_seed:str, client_seed:str, nonce:int):
    """
    Provably-fair HMAC RNG -> uniform x in [0,1).
    Mapping:
      x < 0.495 -> heads
      0.495 <= x < 0.505 -> edge
      else -> tails
    """
    msg = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), msg, hashlib.sha256).digest()
    n = int.from_bytes(digest[:8], "big")
    x = n / 2**64  # [0,1)
    if x < P_HEADS:
        return ("heads", x)
    if x < P_HEADS + P_EDGE:
        return ("edge", x)
    return ("tails", x)

def payout_for(outcome:str, chosen:str, stake:int)->int:
    if outcome == "edge":
        return int(round(stake * MULT_EDGE))
    if outcome == chosen:
        return int(round(stake * MULT_SIDE))
    return 0

def main_menu_kb(balance:int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Орёл",  callback_data="side:heads")
    kb.button(text="Решка", callback_data="side:tails")
    kb.button(text="Пополнить (1000 XTR)", callback_data="dep:1000")
    kb.button(text="Пополнить (5000 XTR)", callback_data="dep:5000")
    kb.adjust(2,2)
    return kb.as_markup()

@dp.message(Command("start", "help"))
async def start(m: Message):
    bal = await get_balance(m.from_user.id)
    await m.answer(
        "🪙 <b>Монетка</b>\n"
        f"Баланс: <b>{bal}</b> XTR\n"
        "Правила: Орёл 49.5%, Решка 49.5%, Ребро 1%.\n"
        "Выплаты: 1.75× (угадал сторону), 8× (ребро).\n"
        f"Ставка от {MIN_BET} до {MAX_BET} XTR.\n\n"
        "1) Выбери сторону.\n2) Отправь сумму ставки числом.",
        reply_markup=main_menu_kb(bal)
    )

@dp.callback_query(F.data.startswith("side:"))
async def choose_side(cq: CallbackQuery):
    side = cq.data.split(":")[1]
    pending_choice[cq.from_user.id] = side
    await cq.answer("Сторона выбрана")
    await cq.message.edit_text(
        f"Выбор: <b>{'Орёл' if side=='heads' else 'Решка'}</b>\n"
        f"Введи сумму ставки ({MIN_BET}-{MAX_BET} XTR) числом.\n",
        reply_markup=main_menu_kb(await get_balance(cq.from_user.id))
    )

@dp.callback_query(F.data.startswith("dep:"))
async def quick_deposit(cq: CallbackQuery):
    amount = int(cq.data.split(":")[1])
    await cq.answer()
    await bot.send_invoice(
        chat_id=cq.message.chat.id,
        title="Пополнение баланса",
        description=f"Зачисление {amount} XTR",
        payload=f"dep:{amount}",
        provider_token="",      # Stars: leave empty
        currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=amount)]
    )

@dp.message(F.text.regexp(r"^\d+$"))
async def place_bet(m: Message):
    stake = int(m.text)
    uid = m.from_user.id
    if not (MIN_BET <= stake <= MAX_BET):
        await m.reply(f"Неверная сумма. Допустимо {MIN_BET}–{MAX_BET} XTR.")
        return
    side = pending_choice.get(uid)
    if side not in ("heads","tails"):
        await m.reply("Сначала выбери сторону: Орёл или Решка (кнопки внизу).")
        return
    bal = await get_balance(uid)
    if bal < stake:
        await m.reply(f"Недостаточно средств ({bal} XTR). Нажми «Пополнить».")
        return

    # commit–reveal
    server_seed = secrets.token_hex(32)
    client_seed = secrets.token_hex(16)
    nonce = 1
    commit = make_commit(server_seed)

    # списываем ставку
    await add_balance(uid, -stake)

    outcome, x = rng_roll(server_seed, client_seed, nonce)
    prize = payout_for(outcome, side, stake)
    if prize:
        await add_balance(uid, prize)

    # лог бета
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO bets(user_id, side, stake, server_seed, client_seed, nonce, commit_hash, outcome, payout)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                         (uid, side, stake, server_seed, client_seed, nonce, commit, outcome, prize))
        await db.commit()

    bal2 = await get_balance(uid)
    name = {"heads":"Орёл","tails":"Решка","edge":"Ребро"}[outcome]
    await m.answer(
        f"🎲 <b>Результат:</b> {name}\n"
        f"Ставка: {stake} XTR → Выплата: <b>{prize}</b> XTR\n"
        f"Баланс: <b>{bal2}</b> XTR\n\n"
        f"<b>Проверка честности</b>\n"
        f"commit: <code>{commit}</code>\n"
        f"server_seed: <code>{server_seed}</code>\n"
        f"client_seed: <code>{client_seed}</code>\n"
        f"nonce: <code>{nonce}</code>\n"
        f"Правила: x<0.495 → Орёл, 0.495≤x<0.505 → Ребро, иначе → Решка."
    )

@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def on_successful_payment(m: Message):
    sp = m.successful_payment
    uid = m.from_user.id
    amount = sp.total_amount  # in XTR, this is the star count
    await add_balance(uid, amount)
    bal = await get_balance(uid)
    await m.answer(f"✅ Пополнение: +{amount} XTR\nТекущий баланс: <b>{bal}</b> XTR",
                   reply_markup=main_menu_kb(bal))

async def main():
    await db_init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    # --- Render keep-alive workaround ---
import threading
from flask import Flask

def keep_alive():
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Bot is running"

    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

keep_alive()  # ⬅️ вызываем до запуска бота
# --- end workaround ---

if __name__ == "__main__":
    asyncio.run(main())
