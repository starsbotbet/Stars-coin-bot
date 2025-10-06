import os, hmac, hashlib, secrets, asyncio, aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice, CallbackQuery
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# ==== CONFIG ====
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Временная БД по умолчанию (Render free). Если подключишь диск -> задай DB_PATH=/data/bank.sqlite в Environment.
DB_PATH = os.getenv("DB_PATH", "/tmp/bank.sqlite")

MIN_BET, MAX_BET = 100, 5000
MULT_SIDE = 1.75
MULT_EDGE = 8.0
P_HEADS = 0.495       # 49.5%
P_EDGE  = 0.010       # 1%  (tails = 0.495)

ADMIN_ID = 634522146  # <-- поставь свой Telegram user_id для /give

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# in-memory выбор стороны
pending_choice = {}  # user_id -> 'heads'|'tails'|'edge'


# ==== DB ====
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
        # депозиты Stars (для автоворота через refund)
        await db.execute("""CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            charge_id TEXT,
            amount INTEGER,
            refunded INTEGER NOT NULL DEFAULT 0,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        # заявки на вывод сверх депозитов
        await db.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            auto_refunded INTEGER,
            status TEXT DEFAULT 'pending',
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


# ==== Provably-fair RNG ====
def make_commit(server_seed:str)->str:
    import hashlib as _hl
    return _hl.sha256(server_seed.encode()).hexdigest()

def rng_roll(server_seed:str, client_seed:str, nonce:int):
    """
    HMAC-SHA256 -> x in [0,1)
      x < 0.495 -> heads
      0.495 <= x < 0.505 -> edge
      else -> tails
    """
    import hmac, hashlib
    msg = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), msg, hashlib.sha256).digest()
    n = int.from_bytes(digest[:8], "big")
    x = n / 2**64
    if x < P_HEADS: return ("heads", x)
    if x < P_HEADS + P_EDGE: return ("edge", x)
    return ("tails", x)

def payout_for(outcome:str, chosen:str, stake:int)->int:
    if outcome == "edge":
        return int(round(stake * MULT_EDGE))
    if outcome == chosen:
        return int(round(stake * MULT_SIDE))
    return 0


# ==== UI ====
def main_menu_kb(balance:int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Орёл",  callback_data="side:heads")
    kb.button(text="Решка", callback_data="side:tails")
    kb.button(text="Ребро", callback_data="side:edge")  # NEW
    kb.button(text="Пополнить (1000 XTR)", callback_data="dep:1000")
    kb.button(text="Пополнить (5000 XTR)", callback_data="dep:5000")
    kb.button(text=f"Вывести ({balance} XTR)", callback_data="withdraw")  # NEW
    kb.adjust(3,2,1)
    return kb.as_markup()


@dp.message(Command("start","help"))
async def start(m: Message):
    bal = await get_balance(m.from_user.id)
    await m.answer(
        "🪙 <b>Монетка</b>\n"
        f"Баланс: <b>{bal}</b> XTR\n"
        "Выплаты: 1.75× за сторону, 8× за ребро.\n"
        f"Ставка: {MIN_BET}-{MAX_BET} XTR.\n\n"
        "1) Выбери сторону (Орёл/Решка/Ребро).\n"
        "2) Отправь сумму ставки числом.",
        reply_markup=main_menu_kb(bal)
    )


@dp.callback_query(F.data.startswith("side:"))
async def choose_side(cq: CallbackQuery):
    side = cq.data.split(":")[1]  # heads|tails|edge
    pending_choice[cq.from_user.id] = side
    await cq.answer("Сторона выбрана")
    await cq.message.edit_text(
        f"Выбор: <b>{'Орёл' if side=='heads' else ('Решка' if side=='tails' else 'Ребро')}</b>\n"
        f"Введи сумму ({MIN_BET}-{MAX_BET} XTR).",
        reply_markup=main_menu_kb(await get_balance(cq.from_user.id))
    )


@dp.callback_query(F.data == "withdraw")
async def ask_withdraw(cq: CallbackQuery):
    bal = await get_balance(cq.from_user.id)
    await cq.answer()
    await cq.message.answer(
        f"💸 На балансе: <b>{bal}</b> XTR\n"
        "Отправь сумму командой: <code>/withdraw 1000</code>\n"
        "Автовыплата делается рефандом твоих депозитов Stars.\n"
        "Если хочешь вывести больше, чем внесёшь — разница уйдёт администратору на ручной вывод."
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
        provider_token="",      # Stars
        currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=amount)]
    )


@dp.message(F.text.regexp(r"^\d+$"))
async def place_bet(m: Message):
    stake = int(m.text)
    uid = m.from_user.id
    if not (MIN_BET <= stake <= MAX_BET):
        await m.reply(f"Неверная сумма. Допустимо {MIN_BET}–{MAX_BET} XTR."); return
    side = pending_choice.get(uid)
    if side not in ("heads","tails","edge"):
        await m.reply("Сначала выбери: Орёл / Решка / Ребро."); return
    bal = await get_balance(uid)
    if bal < stake:
        await m.reply(f"Недостаточно средств ({bal} XTR). Нажми «Пополнить»."); return

    # commit-reveal
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
        f"x<0.495→Орёл, 0.495≤x<0.505→Ребро, иначе→Решка."
    )


# ==== Stars payments ====
@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def on_successful_payment(m: Message):
    sp = m.successful_payment
    uid = m.from_user.id
    amount = sp.total_amount  # XTR
    await add_balance(uid, amount)

    # лог депозита (для автоворота)
    charge_id = getattr(sp, "telegram_payment_charge_id", None)
    if charge_id:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO deposits(user_id, charge_id, amount, refunded) VALUES(?,?,?,0)",
                             (uid, charge_id, amount))
            await db.commit()

    bal = await get_balance(uid)
    await m.answer(f"✅ Пополнение: +{amount} XTR\nБаланс: <b>{bal}</b> XTR",
                   reply_markup=main_menu_kb(bal))


# ==== Withdraw ====
@dp.message(Command("withdraw"))
async def withdraw_cmd(m: Message):
    uid = m.from_user.id
    parts = m.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await m.reply("Использование: /withdraw 1000"); return
    amount = int(parts[1])
    bal = await get_balance(uid)
    if amount <= 0 or amount > bal:
        await m.reply(f"Недостаточно средств. Баланс: {bal} XTR"); return

    # сколько можно вернуть авто (депозиты - уже refunded)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, charge_id, amount, refunded FROM deposits WHERE user_id=? ORDER BY ts ASC", (uid,))
        rows = await cur.fetchall()

    to_refund = amount
    auto_refunded = 0

    for dep_id, charge_id, dep_amount, refunded in rows:
        left = dep_amount - refunded
        if left <= 0 or to_refund <= 0: 
            continue
        chunk = min(left, to_refund)

        # Пытаемся частично рефандить Stars (может быть не доступно во всех клиентах)
        try:
            # aiogram 3: snake_case обёртка
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id, amount=chunk)
            new_refunded = refunded + chunk
        except Exception:
            # fallback: если не вышло частично — пробуем целиком
            if refunded == 0 and to_refund >= dep_amount:
                await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
                new_refunded = dep_amount
                chunk = dep_amount
            else:
                continue

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE deposits SET refunded=? WHERE id=?", (new_refunded, dep_id))
            await db.commit()

        to_refund -= chunk
        auto_refunded += chunk
        if to_refund <= 0:
            break

    if auto_refunded > 0:
        await add_balance(uid, -auto_refunded)

    rest = amount - auto_refunded
    if rest > 0:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO withdrawals(user_id, amount, auto_refunded, status) VALUES(?,?,?,'pending')",
                             (uid, amount, auto_refunded))
            await db.commit()
        await m.reply(
            f"✅ Автовыплата Stars: {auto_refunded} XTR.\n"
            f"📝 Остаток {rest} XTR отправлен администратору на ручной вывод."
        )
    else:
        await m.reply(f"✅ Выплата Stars завершена: {auto_refunded} XTR.")


# ==== Admin test topup ====
@dp.message(Command("give"))
async def admin_give(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        amount = int(m.text.split(maxsplit=1)[1])
    except Exception:
        await m.reply("Использование: /give 10000"); return
    await add_balance(m.from_user.id, amount)
    bal = await get_balance(m.from_user.id)
    await m.reply(f"Начислено {amount} XTR. Баланс: {bal} XTR")


# ==== Run ====
async def main():
    await db_init()
    await dp.start_polling(bot)

# --- Render keep-alive (Web Service) ---
import threading
from flask import Flask
def keep_alive():
    app = Flask(__name__)
    @app.route("/")
    def home():
        return "Bot is running"
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()
keep_alive()
# --- end workaround ---

if __name__ == "__main__":
    asyncio.run(main())
