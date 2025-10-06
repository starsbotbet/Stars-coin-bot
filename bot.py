import os
import asyncio
import random
import aiosqlite
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

DB_PATH = os.getenv("DB_PATH", "/tmp/bank.sqlite")
MIN_BET = 100
MAX_BET = 5000

P_HEADS = 0.495
P_TAILS = 0.495
P_EDGE  = 0.01
K_MAIN = 1.75
K_EDGE = 8.0

@asynccontextmanager
async def db_conn():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db

async def db_init():
    async with db_conn() as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
            side TEXT, stake INTEGER, outcome TEXT, prize INTEGER,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        await db.commit()

async def get_balance(uid:int)->int:
    async with db_conn() as db:
        cur = await db.execute("SELECT balance FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO users(uid,balance) VALUES(?,0)", (uid,))
            await db.commit()
            return 0
        return int(row["balance"])

async def add_balance(uid:int, delta:int):
    async with db_conn() as db:
        await db.execute("""INSERT INTO users(uid,balance) VALUES(?,?)
            ON CONFLICT(uid) DO UPDATE SET balance=balance+excluded.balance""",
            (uid, delta))
        await db.commit()

bot = Bot(TOKEN, parse_mode="HTML")
dp = Dispatcher()

def main_menu_kb(balance:int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Орёл", callback_data="side:heads")
    kb.button(text="Решка", callback_data="side:tails")
    kb.button(text="Ребро", callback_data="side:edge")
    kb.row()
    kb.button(text="Пополнить (1000 тест)", callback_data="topup:1000")
    kb.button(text="Пополнить (5000 тест)", callback_data="topup:5000")
    kb.row()
    kb.button(text="Вывести (тест)", callback_data="withdraw")
    return kb.as_markup()

@dp.message(Command("start"))
async def cmd_start(m: Message):
    bal = await get_balance(m.from_user.id)
    txt = ("🪙 <b>Монетка</b>\n"
           f"Баланс: <b>{bal}</b> XTR\n\n"
           "Правила:\n"
           f"• Орёл/Решка — 49.5% / 49.5% → выплата <b>{K_MAIN}×</b>\n"
           f"• Ребро — 1% → выплата <b>{K_EDGE}×</b>\n"
           f"Ставка от {MIN_BET} до {MAX_BET} XTR.\n\n"
           "1) Выбери сторону кнопкой.\n"
           "2) Отправь сумму ставки числом.")
    await m.answer(txt, reply_markup=main_menu_kb(bal))

USER_SIDE = {}

@dp.callback_query(F.data.startswith("side:"))
async def choose_side(c: CallbackQuery):
    _, side = c.data.split(":")
    USER_SIDE[c.from_user.id] = side
    names = {"heads":"Орёл","tails":"Решка","edge":"Ребро"}
    await c.answer()
    await c.message.answer(f"Выбрано: <b>{names.get(side, side)}</b>.\nТеперь пришли сумму ставки.")

@dp.callback_query(F.data.startswith("topup:"))
async def cb_topup(c: CallbackQuery):
    amount = int(c.data.split(":")[1])
    await add_balance(c.from_user.id, amount)
    bal = await get_balance(c.from_user.id)
    await c.answer()
    await c.message.answer(f"✅ Пополнение: +{amount} XTR\nБаланс: <b>{bal}</b> XTR",
                           reply_markup=main_menu_kb(bal))

@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(c: CallbackQuery):
    bal = await get_balance(c.from_user.id)
    await c.answer()
    await c.message.answer("🔄 Вывод пока тестовый (внутренняя валюта).",
                           reply_markup=main_menu_kb(bal))

@dp.message(F.text.regexp(r"^\d+$"))
async def place_bet(m: Message):
    uid = m.from_user.id
    if uid not in USER_SIDE:
        await m.answer("Сначала выбери: «Орёл», «Решка» или «Ребро».")
        return
    stake = int(m.text)
    if not (MIN_BET <= stake <= MAX_BET):
        await m.answer(f"Ставка должна быть от {MIN_BET} до {MAX_BET} XTR.")
        return
    bal = await get_balance(uid)
    if bal < stake:
        await m.answer(f"Недостаточно средств. Баланс: {bal} XTR.")
        return

    rnd = random.random()
    if rnd < 0.495:
        outcome = "heads"
    elif rnd < 0.99:
        outcome = "tails"
    else:
        outcome = "edge"

    chosen = USER_SIDE[uid]
    win = (chosen == outcome)
    coef = (K_EDGE if outcome=="edge" else K_MAIN) if win else 0.0
    prize = int(round(stake * coef))

    await add_balance(uid, -stake + prize)
    bal2 = await get_balance(uid)
    names = {"heads":"Орёл","tails":"Решка","edge":"Ребро"}
    await m.answer(f"🎲 <b>Результат:</b> {names[outcome]}\n"
                   f"Ставка: {stake} → Выплата: <b>{prize}</b>\n"
                   f"Баланс: <b>{bal2}</b> XTR")

async def main():
    await db_init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    # keep-alive tiny web for Render
    import threading
    from flask import Flask
    app = Flask(__name__)
    @app.get("/")
    def home():
        return "Bot is running"
    def run_web():
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
