import os
import asyncio
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from flask import Flask
import threading

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is not set")

DB_PATH = os.getenv("DB_PATH", "/data/bank.sqlite")

# ----- storage -----
def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS bets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER,
            side TEXT,
            stake INTEGER,
            outcome TEXT,
            prize INTEGER,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        con.commit()

def get_balance(uid: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute("SELECT balance FROM users WHERE uid=?", (uid,)).fetchone()
        return row[0] if row else 0

def ensure_user(uid: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("INSERT OR IGNORE INTO users(uid,balance) VALUES(?,0)", (uid,))
        con.commit()

def add_balance(uid: int, amount: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("INSERT OR IGNORE INTO users(uid,balance) VALUES(?,0)", (uid,))
        con.execute("UPDATE users SET balance = balance + ? WHERE uid=?", (amount, uid))
        con.commit()

def sub_balance(uid: int, amount: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute("SELECT balance FROM users WHERE uid=?", (uid,)).fetchone()
        bal = row[0] if row else 0
        if bal < amount:
            return False
        con.execute("UPDATE users SET balance = balance - ? WHERE uid=?", (amount, uid))
        con.commit()
        return True

def save_bet(uid: int, side: str, stake: int, outcome: str, prize: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT INTO bets(uid,side,stake,outcome,prize) VALUES(?,?,?,?,?)",
            (uid, side, stake, outcome, prize)
        )
        con.commit()

# ----- game logic -----
SIDES = {"heads": "Орёл", "tails": "Решка", "edge": "Ребро"}
COEFFS = {"heads": 1.75, "tails": 1.75, "edge": 8.0}

@dataclass
class SpinResult:
    outcome: str
    prize: int

def spin(side: str, stake: int) -> SpinResult:
    # probabilities: heads 49.5%, tails 49.5%, edge 1%
    r = random.random()
    if r < 0.495:
        outcome = "heads"
    elif r < 0.495 + 0.495:
        outcome = "tails"
    else:
        outcome = "edge"
    prize = 0
    if side == outcome:
        prize = int(round(stake * COEFFS[side]))
    return SpinResult(outcome, prize)

# ----- ui helpers -----
def main_menu_kb(balance: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Орёл", callback_data="choose:heads")
    kb.button(text="Решка", callback_data="choose:tails")
    kb.button(text="Ребро", callback_data="choose:edge")
    kb.adjust(3)
    kb.button(text="Пополнить (+1000 тест)", callback_data="topup:1000")
    kb.button(text="Пополнить (+5000 тест)", callback_data="topup:5000")
    kb.adjust(1)
    kb.button(text="Вывод (звёздами)", callback_data="withdraw")
    return kb.as_markup()

# ----- bot -----
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id)
    bal = get_balance(m.from_user.id)
    text = (
        "🪙 <b>Монетка</b>\n"
        f"Баланс: <b>{bal}</b> XTR\n\n"
        "Правила: коэффициенты — Орёл 1.75×, Решка 1.75×, Ребро 8×.\n"
        "Ставка от 100 до 5000 XTR.\n\n"
        "1) Выбери сторону кнопкой.\n"
        "2) Пришли сумму ставки числом."
    )
    await m.answer(text, reply_markup=main_menu_kb(bal))

@dp.callback_query(F.data.startswith("topup:"))
async def cb_topup(c: CallbackQuery):
    amount = int(c.data.split(":")[1])
    add_balance(c.from_user.id, amount)
    bal = get_balance(c.from_user.id)
    await c.message.answer(f"✅ Пополнение: +{amount} XTR\nТекущий баланс: <b>{bal}</b> XTR",
                           reply_markup=main_menu_kb(bal))
    await c.answer()

@dp.callback_query(F.data.startswith("choose:"))
async def cb_choose(c: CallbackQuery):
    side = c.data.split(":")[1]
    name = SIDES.get(side, side)
    await c.message.answer(f"Ты выбрал: <b>{name}</b>\nПришли сумму ставки (100–5000).")
    # сохраним выбор в краткосрочной "памяти" через message id
    await bot.session.storage.set(f"side:{c.from_user.id}", side)  # type: ignore[attr-defined]
    await c.answer()

# простой key-value storage на основе bot.session (в aiogram его нет из коробки),
# поэтому делаем fallback через in-memory dict
_memory = {}

async def _get(key: str):
    return _memory.get(key)

async def _set(key: str, value):
    _memory[key] = value

# подмена set/get, если выше storage не доступен
try:
    # проверим, есть ли у объекта bot.session.storage методы set/get
    getattr(bot.session, "storage")
    # если где-то упадёт — перейдём на _memory
    bot.session.storage.set  # type: ignore[attr-defined]
    bot.session.storage.get  # type: ignore[attr-defined]
except Exception:
    async def patch_get(key): return _get(key)
    async def patch_set(key, value): return _set(key, value)
    bot.session = type("S", (), {"storage": type("T", (), {"get": patch_get, "set": patch_set})})()

@dp.message(F.text.regexp(r"^\d+$"))
async def handle_bet(m: Message):
    ensure_user(m.from_user.id)
    side = await bot.session.storage.get(f"side:{m.from_user.id}")  # type: ignore[attr-defined]
    if not side:
        await m.answer("Сначала выбери сторону: нажми «Орёл», «Решка» или «Ребро».",
                       reply_markup=main_menu_kb(get_balance(m.from_user.id)))
        return

    stake = int(m.text)
    if stake < 100 or stake > 5000:
        await m.answer("Ставка должна быть от 100 до 5000 XTR.")
        return

    if not sub_balance(m.from_user.id, stake):
        await m.answer("Недостаточно средств. Пополни баланс кнопкой ниже.",
                       reply_markup=main_menu_kb(get_balance(m.from_user.id)))
        return

    res = spin(side, stake)
    prize = res.prize
    outcome_name = SIDES[res.outcome]

    if prize > 0:
        add_balance(m.from_user.id, prize)
        result_line = f"🎉 Победа! Выпало: <b>{outcome_name}</b>. Выплата: <b>{prize}</b> XTR"
    else:
        result_line = f"🙈 Проигрыш. Выпало: <b>{outcome_name}</b>."

    save_bet(m.from_user.id, side, stake, res.outcome, prize)
    bal = get_balance(m.from_user.id)
    await m.answer(
        f"{result_line}\nБаланс: <b>{bal}</b> XTR",
        reply_markup=main_menu_kb(bal)
    )

@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(c: CallbackQuery):
    bal = get_balance(c.from_user.id)
    text = (
        "💸 <b>Вывод звёздами</b>\n\n"
        f"Текущий баланс: <b>{bal}</b> XTR.\n"
        "Нажми кнопку ниже — бот отправит тебе форму-заявку. "
        "После проверки админ переведёт звёзды вручную."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Оформить заявку", callback_data="withdraw_form")
    ]])
    await c.message.answer(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "withdraw_form")
async def cb_withdraw_form(c: CallbackQuery):
    bal = get_balance(c.from_user.id)
    if bal <= 0:
        await c.message.answer("На балансе нет средств для вывода.")
        await c.answer()
        return
    # тут можно сохранить заявку в таблицу или отправить админу
    await c.message.answer(
        "✅ Заявка на вывод создана.\n"
        "Админ свяжется с тобой и переведёт звёзды.\n"
        "Для ускорения напиши @your_admin_username."
    )
    await c.answer()

async def main():
    db_init()
    await dp.start_polling(bot)

# ---- Flask keep-alive for Fly.io ----
def keep_alive():
    app = Flask(__name__)

    @app.route("/")
    def home():
        return "Bot is running"

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.run(main())
