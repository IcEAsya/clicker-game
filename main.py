# main.py — единый сервер: FastAPI (WebApp) + aiogram (Bot)
import os
import asyncio
import sqlite3
import time
import random
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import uvicorn

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", 8000))
# Render сам подставит этот URL
WEBAPP_URL = os.getenv("RENDER_EXTERNAL_URL", "")
DB_PATH = "game.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- БАЗА ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 0,
        total_earned INTEGER DEFAULT 0,
        clicks INTEGER DEFAULT 0,
        last_passive INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS businesses (
        user_id INTEGER,
        business_id INTEGER,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, business_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS upgrades (
        user_id INTEGER,
        upgrade_id INTEGER,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, upgrade_id)
    )""")
    conn.commit()
    conn.close()


BUSINESSES = [
    {"id": 1, "emoji": "☕", "name": "Кофейня",         "price": 100,        "income": 1},
    {"id": 2, "emoji": "🍕", "name": "Пиццерия",        "price": 800,        "income": 8},
    {"id": 3, "emoji": "🏪", "name": "Магазин",         "price": 6000,       "income": 60},
    {"id": 4, "emoji": "🏢", "name": "Офис",            "price": 50000,      "income": 500},
    {"id": 5, "emoji": "🏭", "name": "Завод",           "price": 400000,     "income": 4000},
    {"id": 6, "emoji": "🏦", "name": "Банк",            "price": 3000000,    "income": 30000},
    {"id": 7, "emoji": "🚀", "name": "Космопорт",       "price": 25000000,   "income": 250000},
    {"id": 8, "emoji": "💎", "name": "Планета-рудник",  "price": 2000000000, "income": 20000000},
]

UPGRADES = [
    {"id": 1, "emoji": "👆", "name": "Тренировка",     "price": 50,    "bonus": 1},
    {"id": 2, "emoji": "💪", "name": "Быстрые пальцы", "price": 500,   "bonus": 5},
    {"id": 3, "emoji": "🦾", "name": "Кибер-рука",     "price": 5000,  "bonus": 30},
    {"id": 4, "emoji": "⚡", "name": "Нейроускоритель","price": 50000, "bonus": 200},
]

LEVELS = [
    (0, "Новичок", "🐣"),
    (1_000, "Стажёр", "📎"),
    (10_000, "Работник", "🔧"),
    (100_000, "Менеджер", "💼"),
    (1_000_000, "Директор", "🎩"),
    (10_000_000, "Магнат", "💰"),
    (100_000_000, "Миллиардер", "💎"),
    (1_000_000_000, "Олигарх", "👑"),
]


def get_level(total):
    lvl = 0
    for i, (t, _, _) in enumerate(LEVELS):
        if total >= t:
            lvl = i
    return lvl


def calc_per_second(c, uid):
    total = 0
    c.execute("SELECT business_id, count FROM businesses WHERE user_id=?", (uid,))
    for bid, cnt in c.fetchall():
        for b in BUSINESSES:
            if b["id"] == bid:
                total += b["income"] * cnt
    return total


def calc_per_click(c, uid):
    base = 1
    c.execute("SELECT upgrade_id, count FROM upgrades WHERE user_id=?", (uid,))
    for up_id, cnt in c.fetchall():
        for u in UPGRADES:
            if u["id"] == up_id:
                base += u["bonus"] * cnt
    return base


def update_passive(uid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_passive FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    last = row[0]
    now = int(time.time())
    elapsed = now - last
    if elapsed < 1:
        conn.close()
        return
    ps = calc_per_second(c, uid)
    earned = ps * elapsed
    c.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+?, last_passive=? WHERE user_id=?",
              (earned, earned, now, uid))
    conn.commit()
    conn.close()


def ensure_user(uid, username=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if not c.fetchone():
        c.execute("INSERT INTO users (user_id, username, last_passive) VALUES (?,?,?)",
                  (uid, username or "", int(time.time())))
        conn.commit()
    conn.close()


def business_price(uid, bid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM businesses WHERE user_id=? AND business_id=?", (uid, bid))
    row = c.fetchone()
    count = row[0] if row else 0
    conn.close()
    for b in BUSINESSES:
        if b["id"] == bid:
            return int(b["price"] * (1.15 ** count))
    return 0


def upgrade_price(uid, upg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM upgrades WHERE user_id=? AND upgrade_id=?", (uid, upg_id))
    row = c.fetchone()
    count = row[0] if row else 0
    conn.close()
    for u in UPGRADES:
        if u["id"] == upg_id:
            return int(u["price"] * (1.5 ** count))
    return 0


# ---------- TELEGRAM BOT ----------
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not WEBAPP_URL:
        await message.answer("⚠️ WEBAPP_URL не настроен")
        return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(
        text="🎮 Играть",
        web_app=WebAppInfo(url=WEBAPP_URL)
    ))
    await message.answer(
        "💰 <b>Финансовая Империя</b>\n\n"
        "Кликни, заработай, купи бизнес и стань олигархом!\n\n"
        "Нажми кнопку ниже, чтобы начать 👇",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("DB инициализирована")
    if bot:
        asyncio.create_task(dp.start_polling(bot))
        logger.info("Бот запущен (polling)")
    # Keep-alive, чтобы Render не усыплял
    asyncio.create_task(keep_alive())
    yield


app = FastAPI(lifespan=lifespan)


async def keep_alive():
    """Пингует сам себя, чтобы Render не засыпал."""
    if not WEBAPP_URL:
        return
    await asyncio.sleep(30)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(WEBAPP_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logger.info(f"keep-alive: {r.status}")
            except Exception as e:
                logger.warning(f"keep-alive error: {e}")
            await asyncio.sleep(600)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("webapp/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/state")
async def api_state(user_id: int, username: str = ""):
    ensure_user(user_id, username)
    update_passive(user_id)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance, total_earned, clicks FROM users WHERE user_id=?", (user_id,))
    balance, total, clicks = c.fetchone()
    pc = calc_per_click(c, user_id)
    ps = calc_per_second(c, user_id)

    c.execute("SELECT business_id, count FROM businesses WHERE user_id=?", (user_id,))
    biz = {bid: cnt for bid, cnt in c.fetchall()}
    c.execute("SELECT upgrade_id, count FROM upgrades WHERE user_id=?", (user_id,))
    upg = {u_id: cnt for u_id, cnt in c.fetchall()}
    conn.close()

    lvl = get_level(total)
    if lvl + 1 < len(LEVELS):
        next_t = LEVELS[lvl + 1][0]
        progress = min((total / next_t) * 100, 100) if next_t else 0
    else:
        progress = 100

    businesses_list = [
        {**b, "owned": biz.get(b["id"], 0),
         "current_price": business_price(user_id, b["id"])}
        for b in BUSINESSES
    ]
    upgrades_list = [
        {**u, "owned": upg.get(u["id"], 0),
         "current_price": upgrade_price(user_id, u["id"])}
        for u in UPGRADES
    ]

    return {
        "balance": balance, "total_earned": total, "clicks": clicks,
        "per_click": pc, "per_second": ps,
        "level": lvl, "level_name": LEVELS[lvl][1], "level_emoji": LEVELS[lvl][2],
        "progress": progress,
        "businesses": businesses_list, "upgrades": upgrades_list,
    }


@app.post("/api/click")
async def api_click(request: Request):
    data = await request.json()
    uid = data["user_id"]
    ensure_user(uid)
    update_passive(uid)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    pc = calc_per_click(c, uid)
    lucky = random.random() < 0.05
    gain = pc * 5 if lucky else pc
    c.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+?, clicks=clicks+1 WHERE user_id=?",
              (gain, gain, uid))
    conn.commit()
    conn.close()
    return {"gain": gain, "lucky": lucky}


@app.post("/api/buy_business")
async def api_buy_business(request: Request):
    data = await request.json()
    uid = data["user_id"]
    bid = data["business_id"]
    update_passive(uid)
    price = business_price(uid, bid)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    balance = c.fetchone()[0]
    if balance < price:
        conn.close()
        return JSONResponse({"ok": False, "error": "no money"}, status_code=400)
    c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (price, uid))
    c.execute("""INSERT INTO businesses (user_id, business_id, count) VALUES (?, ?, 1)
                 ON CONFLICT(user_id, business_id) DO UPDATE SET count=count+1""",
              (uid, bid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/buy_upgrade")
async def api_buy_upgrade(request: Request):
    data = await request.json()
    uid = data["user_id"]
    upg_id = data["upgrade_id"]
    update_passive(uid)
    price = upgrade_price(uid, upg_id)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    balance = c.fetchone()[0]
    if balance < price:
        conn.close()
        return JSONResponse({"ok": False, "error": "no money"}, status_code=400)
    c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (price, uid))
    c.execute("""INSERT INTO upgrades (user_id, upgrade_id, count) VALUES (?, ?, 1)
                 ON CONFLICT(user_id, upgrade_id) DO UPDATE SET count=count+1""",
              (uid, upg_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/top")
async def api_top():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, total_earned FROM users ORDER BY total_earned DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return [{"username": r[0], "total": r[1]} for r in rows]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)