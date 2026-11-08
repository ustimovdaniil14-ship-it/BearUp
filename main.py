import asyncio, hashlib, hmac, json, logging, math, os, re, secrets, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           Message, PreCheckoutQuery, WebAppInfo)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bearup")

ENV = Path(__file__).with_name(".env")
if ENV.exists():
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ADMINS = {1780243476, 1780246042} | {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PORT = int(os.environ.get("PORT", 8080))
API = "https://api.staticgram.top"


# ======================================================================
#   НАСТРОЙКИ ИГР: ВСЕ ШАНСЫ ХРАНЯТСЯ ТОЛЬКО ЗДЕСЬ (файл main.py)
#   index.html сам подтягивает эти значения с сервера, там менять не надо.
# ======================================================================
MAX_BET = 10000                      # максимальная ставка

# --- Апгрейдер (звёзды и NFT): шанс победы = UP_EDGE / множитель
UP_EDGE = 0.9
UP_TOP_CHANCE = 0.57                 # самый высокий шанс: 27%  (выше не бывает, значит 31% и т.п. убраны)
UP_LOW_CHANCE = 0.01                 # самый низкий шанс: 1%
UP_MIN_X = math.ceil(round(UP_EDGE / UP_TOP_CHANCE * 100, 6)) / 100    # минимальный множитель (x1.08 = 1.32%)
UP_MAX_X = math.floor(round(UP_EDGE / UP_LOW_CHANCE * 100, 6)) / 100   # максимальный множитель (x20 = 1%)

# --- Мины
MINES_EDGE = 0.97                    # комиссия: множители умножаются на это число
# Шанс выигрыша раунда. Число 0..1 = раунд заранее назначается выигрышным с таким шансом
# (игрок видит это в интерфейсе). Поставь -1 (или MINES_WIN_CHANCE=-1 в .env), чтобы мины были
# полностью честными: мины ставятся случайно и ничего не подкручивается.
MINES_WIN_CHANCE = float(os.environ.get("MINES_WIN_CHANCE", "0.55"))

# --- Ракетка (краш): момент взрыва считается в crash_point() ниже
CRASH_EDGE = 0.97
GROW, COUNTDOWN = 0.15, 2.1          # скорость роста множителя и обратный отсчёт

# --- Лесенка
LADDER_EDGE = 0.97
LADDER_ROWS = 8                      # ступеней (клиент берёт значение отсюда)
LADDER_MAX_WIN = int(os.environ.get("LADDER_MAX_WIN", "1000000"))
# Лимит вывода звёзд на одного игрока за скользящие 24 часа (0 = без лимита). Меняется в Environment на Render.
WD_DAY_LIMIT = int(os.environ.get("WD_DAY_LIMIT", "0"))

# --- Плинко: множители 13 слотов (12 рядов), шанс слота считается как в биноме
# Сейчас: шанс выиграть (x больше 1) = 27.4%, возврат игрокам (RTP) ≈ 31.5%.
# Число в index.html писать не надо, оно берётся отсюда.
PLINKO = [12, 4, 1.6, 1.2, 1.1, 1.02, .2, 1.02, 1.1, 1.2, 1.6, 4, 12]

# запросы, которые недоступны замороженному игроку
FROZEN_BLOCK = {"/api/up", "/api/mines/start", "/api/crash/start", "/api/ladder/start", "/api/plinko/play",
                "/api/withdraw", "/api/nft/withdraw", "/api/nft/buy", "/api/nft/upgrade"}


def public_cfg():
    """Что отправляем в мини-апп, чтобы интерфейс показывал те же шансы, что и сервер."""
    return dict(max_bet=MAX_BET, up_edge=UP_EDGE, up_top=UP_TOP_CHANCE, up_low=UP_LOW_CHANCE,
                up_min_x=UP_MIN_X, up_max_x=UP_MAX_X,
                mines_edge=MINES_EDGE, mines_win=MINES_WIN_CHANCE if MINES_WIN_CHANCE >= 0 else None,
                ladder_edge=LADDER_EDGE, ladder_rows=LADDER_ROWS, plinko=PLINKO, wd_day_limit=WD_DAY_LIMIT)


NAMES = ["Мешок добычи", "Книга звёзд", "Лол Поп", "Леденец-трость", "Змеиный бокс", "Эскимо", "Плюшевый Пепе", "Сердечный медальон", "Статуя Свободы", "Ушанка", "Лоурайдер", "Фонарь", "Неко-шлем", "Швейцарские часы", "Испуганный кот", "Могучая рука", "Ключ любви", "Зимний кот", "Кепка Дурова", "Готик-мишка", "UFC-бокс", "Вечная роза", "Дымный рог", "Астральный осколок", "Кукла вуду", "Настольный календарь", "Ракета", "Колокольчики", "Печатка", "Плюшевый медведь", "Рюкзак", "С Днём рождения", "Винтажная сигара", "Хоккейная маска", "Клевер удачи", "Тёмные очки", "Снуп Догг", "Сумка Биркин", "Цветок-череп", "Кошки-сердце", "Злой глаз", "Кольцо с бриллиантом", "Тыква-джек", "Оскар", "Слива", "Зайка-маффин", "Золотая рука", "Браслет-гвоздь", "Шутовской колпак", "Весёлый шимпанзе", "Чапл", "Шляпа ведьмы", "Медаль"]
PRICES = [4000, 5700, 4999, 3000, 3000, 3333, 250000, 59500, 6000, 8200, 11000, 6500, 6800, 7600, 42998, 4800, 6000, 32499, 25000, 35000, 6000, 10500, 24500, 6500, 8800, 7350, 6800, 8000, 10000, 20000, 4000, 3600, 11500, 30600, 4270, 7200, 8899, 9600, 9000, 5400, 5400, 4500, 8400, 6900, 11000, 6000, 7200, 6300, 5600, 5669, 199999, 8000, 4999]

bot = Bot(TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(API)))
dp = Dispatcher()
pool: asyncpg.Pool
http: aiohttp.ClientSession
locks: dict[int, asyncio.Lock] = {}
SEEN: dict[int, float] = {}          # uid -> когда последний раз что-то прислал мини-апп
ONLINE_TTL = 45                      # сколько секунд после последнего запроса игрок считается онлайн


def online_now() -> int:
    t = time.time()
    for u in [u for u, v in SEEN.items() if t - v > 600]:
        del SEEN[u]
    return sum(1 for v in SEEN.values() if t - v <= ONLINE_TTL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, name TEXT, balance BIGINT NOT NULL DEFAULT 0 CHECK(balance>=0),
  wag BIGINT NOT NULL DEFAULT 0, ups INT NOT NULL DEFAULT 0, wins INT NOT NULL DEFAULT 0,
  best BIGINT NOT NULL DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS ledger(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, delta BIGINT, kind TEXT,
  ref TEXT UNIQUE, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS withdrawals(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, status TEXT, error TEXT,
  created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS bets(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, created TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS bets_created ON bets(created);
CREATE TABLE IF NOT EXISTS nfts(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, k INT, n INT, status TEXT NOT NULL DEFAULT 'own',
  created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS games(
  user_id BIGINT, kind TEXT, data TEXT, PRIMARY KEY(user_id, kind));
ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS frozen BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS promos(
  code TEXT PRIMARY KEY, amount BIGINT NOT NULL, uses INT NOT NULL DEFAULT 0, used INT NOT NULL DEFAULT 0,
  expires TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS promo_uses(
  code TEXT, user_id BIGINT, PRIMARY KEY(code, user_id));
"""


class Err(Exception):
    pass


bg: set = set()


def spawn(coro):
    """Фоновая задача (чтобы запрос админа не ждал отправку сообщений)."""
    t = asyncio.create_task(coro)
    bg.add(t)
    t.add_done_callback(bg.discard)


async def tell(uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


def rnd():
    return secrets.randbelow(2 ** 53) / 2 ** 53


def as_int(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise Err("Неверное число")
    return int(v)


# ---------- StaticGram HTTP ----------
async def sg(method: str, **params):
    async with http.post(f"{API}/bot{TOKEN}/{method}", json=params,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
        return await r.json()


# ---------- DB helpers ----------
async def ensure_user(uid: int, name: str):
    await pool.execute("INSERT INTO users(id,name) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET name=$2", uid, name)


async def balance(uid: int) -> int:
    return await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid) or 0


async def credit(uid: int, amount: int, kind: str, ref: str | None = None) -> bool:
    """Идемпотентно по ref. True если начислили."""
    async with pool.acquire() as c, c.transaction():
        if ref:
            ok = await c.fetchval("INSERT INTO ledger(user_id,delta,kind,ref) VALUES($1,$2,$3,$4) "
                                  "ON CONFLICT(ref) DO NOTHING RETURNING id", uid, amount, kind, ref)
            if not ok:
                return False
        else:
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,$3)", uid, amount, kind)
        await c.execute("UPDATE users SET balance=balance+$2 WHERE id=$1", uid, amount)
    return True


async def take_bet(c, uid, bet, ups=0):
    if not 1 <= bet <= MAX_BET:
        raise Err("Ставка от 1 до %d" % MAX_BET)
    bal = await c.fetchval("UPDATE users SET balance=balance-$2, wag=wag+$2, ups=ups+$3 WHERE id=$1 "
                           "AND balance>=$2 RETURNING balance", uid, bet, ups)
    if bal is None:
        raise Err("Недостаточно звёзд")
    await c.execute("INSERT INTO bets(user_id,amount) VALUES($1,$2)", uid, bet)
    return bal


async def pay_win(c, uid, pay):
    return await c.fetchval("UPDATE users SET balance=balance+$2, wins=wins+1, best=GREATEST(best,$2) "
                            "WHERE id=$1 RETURNING balance", uid, pay)


async def get_game(c, uid, kind):
    d = await c.fetchval("SELECT data FROM games WHERE user_id=$1 AND kind=$2 FOR UPDATE", uid, kind)
    return json.loads(d) if d else None


async def put_game(c, uid, kind, g):
    await c.execute("INSERT INTO games(user_id,kind,data) VALUES($1,$2,$3) "
                    "ON CONFLICT(user_id,kind) DO UPDATE SET data=$3", uid, kind, json.dumps(g))


async def del_game(c, uid, kind):
    await c.execute("DELETE FROM games WHERE user_id=$1 AND kind=$2", uid, kind)


# ---------- Auth ----------
def check_init(raw: str):
    data = dict(parse_qsl(raw, keep_blank_values=True))
    h = data.pop("hash", None)
    if not h:
        return None
    s = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, s.encode(), hashlib.sha256).hexdigest(), h):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        return None
    return json.loads(data["user"])


@web.middleware
async def auth_mw(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    user = check_init(request.headers.get("X-Init-Data", ""))
    if not user:
        return web.json_response({"error": "Ошибка авторизации. Откройте игру из бота."}, status=401)
    uid = int(user["id"])
    if request.path != "/api/sync":          # частый опрос не пишет в базу
        await ensure_user(uid, user.get("username") or user.get("first_name") or str(uid))
    row = await pool.fetchrow("SELECT banned,ban_until,frozen,is_admin FROM users WHERE id=$1", uid)
    request["uid"] = uid
    SEEN[uid] = time.time()
    request["admin"] = uid in ADMINS or bool(row and row["is_admin"])
    if not request["admin"] and row:
        bu = row["ban_until"]
        if row["banned"] and (bu is None or bu > datetime.now(timezone.utc)):
            return web.json_response({"error": "Аккаунт заблокирован" + (f" до {str(bu)[:16]} UTC" if bu else "")})
        if row["frozen"] and request.path in FROZEN_BLOCK:
            return web.json_response({"error": "Аккаунт заморожен администратором"})
    try:
        async with locks.setdefault(uid, asyncio.Lock()):
            return await handler(request)
    except Err as e:
        return web.json_response({"error": str(e)})
    except Exception:
        log.exception("api error")
        return web.json_response({"error": "Ошибка сервера"}, status=500)


def ok(**kw):
    return web.json_response({"ok": True, **kw})


# ---------- Профиль / пополнение / вывод ----------
async def api_state(r):
    uid = r["uid"]
    u = await pool.fetchrow("SELECT name,balance,wag,ups,wins,best FROM users WHERE id=$1", uid)
    nfts = await pool.fetch("SELECT id,k,n FROM nfts WHERE user_id=$1 AND status='own' ORDER BY id DESC", uid)
    out = dict(id=uid, name=u["name"], balance=u["balance"], wag=u["wag"], ups=u["ups"],
               wins=u["wins"], best=u["best"], nft=[dict(x) for x in nfts], admin=r["admin"], cfg=public_cfg(),
               online=online_now())
    mg = await pool.fetchval("SELECT data FROM games WHERE user_id=$1 AND kind='mines'", uid)
    if mg:
        g = json.loads(mg)
        out["mines"] = {"bet": g["bet"], "m": g["m"], "open": g["open"]}
    lg = await pool.fetchval("SELECT data FROM games WHERE user_id=$1 AND kind='ladder'", uid)
    if lg:
        g = json.loads(lg)
        out["ladder"] = {"bet": g["bet"], "c": g["c"], "picks": g["picks"]}
    return ok(**out)


async def api_sync(r):
    """Лёгкий опрос: мини-апп каждые пару секунд подтягивает баланс и NFT (выдачи от админа)."""
    uid = r["uid"]
    u = await pool.fetchrow("SELECT balance,wag,ups,wins,best FROM users WHERE id=$1", uid)
    if not u:
        raise Err("Профиль не найден")
    nfts = await pool.fetch("SELECT id,k,n FROM nfts WHERE user_id=$1 AND status='own' ORDER BY id DESC", uid)
    return ok(balance=u["balance"], wag=u["wag"], ups=u["ups"], wins=u["wins"], best=u["best"],
              nft=[dict(x) for x in nfts], admin=r["admin"], online=online_now())


async def api_top(r):
    days = 1 if r.query.get("p") == "day" else 7
    rows = await pool.fetch(
        "SELECT u.name, SUM(b.amount)::bigint AS wag FROM bets b JOIN users u ON u.id=b.user_id "
        "WHERE b.created > now() - make_interval(days => $1) GROUP BY u.id, u.name "
        "ORDER BY wag DESC LIMIT 10", days)
    return ok(top=[dict(x) for x in rows])


async def api_deposit(r):
    amount = as_int((await r.json()).get("amount"))
    if not 1 <= amount <= 100000:
        raise Err("Сумма от 1 до 100000")
    link = await bot.create_invoice_link(
        title="Пополнение BearUp", description=f"{amount} ⭐ на игровой счёт", payload=f"dep:{r['uid']}",
        currency="XTR", prices=[LabeledPrice(label="Пополнение", amount=amount)])
    return ok(link=link)


async def api_withdraw(r):
    uid = r["uid"]
    amount = as_int((await r.json()).get("amount"))
    if amount < 1:
        raise Err("Неверная сумма")
    async with pool.acquire() as c, c.transaction():
        if WD_DAY_LIMIT > 0:
            await c.execute("SELECT 1 FROM users WHERE id=$1 FOR UPDATE", uid)  # параллельные запросы не обойдут лимит
            used = await c.fetchval(
                "SELECT COALESCE(SUM(amount),0)::bigint FROM withdrawals WHERE user_id=$1 "
                "AND created > now() - interval '24 hours' AND status IN ('processing','pending','done')", uid)
            if used + amount > WD_DAY_LIMIT:
                raise Err(f"Лимит вывода {WD_DAY_LIMIT} ⭐ за 24 часа. Сейчас доступно: {max(0, WD_DAY_LIMIT - used)} ⭐")
        new = await c.fetchval("UPDATE users SET balance=balance-$2 WHERE id=$1 AND balance>=$2 "
                               "RETURNING balance", uid, amount)
        if new is None:
            raise Err("Недостаточно звёзд")
        wid = await c.fetchval("INSERT INTO withdrawals(user_id,amount,status) VALUES($1,$2,'processing') "
                               "RETURNING id", uid, amount)
        await c.execute("INSERT INTO ledger(user_id,delta,kind,ref) VALUES($1,$2,'withdraw',$3)",
                        uid, -amount, f"wd:{wid}")
    res = None
    for _ in range(3):  # request_id делает повтор безопасным
        try:
            res = await sg("withdrawStarBalance", user_id=uid, star_count=amount, request_id=f"wd{wid}")
            break
        except Exception as e:
            log.warning("withdraw net error %s", e)
            await asyncio.sleep(1)
    if res is None:
        await pool.execute("UPDATE withdrawals SET status='pending' WHERE id=$1", wid)
        raise Err("Вывод в обработке, проверьте баланс позже")
    if res.get("ok"):
        await pool.execute("UPDATE withdrawals SET status='done' WHERE id=$1", wid)
        return ok(balance=await balance(uid))
    desc = res.get("description", "error")
    await credit(uid, amount, "withdraw_refund", f"wdr:{wid}")
    await pool.execute("UPDATE withdrawals SET status='failed', error=$2 WHERE id=$1", wid, desc)
    await notify_admins(f"⚠️ Вывод #{wid} user {uid} на {amount}⭐ не прошёл: {desc}")
    raise Err("Вывод не прошёл, сумма возвращена на баланс")


async def notify_admins(text):
    for a in ADMINS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


# ---------- Апгрейдер ----------
async def api_up(r):
    d = await r.json()
    bet, x = as_int(d.get("bet")), d.get("x")
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        raise Err("Неверный множитель")
    x = round(float(x), 2)
    if x < UP_MIN_X - 1e-9:
        raise Err("Минимум x%.2f (шанс не выше %d%%)" % (UP_MIN_X, round(UP_TOP_CHANCE * 100)))
    if x > UP_MAX_X + 1e-9:
        raise Err("Максимум x%.2f (шанс не ниже %d%%)" % (UP_MAX_X, round(UP_LOW_CHANCE * 100)))
    ch = UP_EDGE / x
    roll = rnd()
    win = roll < ch
    async with pool.acquire() as c, c.transaction():
        bal = await take_bet(c, r["uid"], bet, ups=1)
        pay = int(bet * x) if win else 0
        if win:
            bal = await pay_win(c, r["uid"], pay)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'up')", r["uid"], pay - bet)
    return ok(r=roll, win=win, pay=pay, balance=bal)


# ---------- Мины ----------
def mines_mult(n, m):
    r = MINES_EDGE
    for i in range(n):
        r *= (25 - i) / (25 - m - i)
    return r


async def mines_cashout(c, uid, g):
    x = mines_mult(len(g["open"]), g["m"])
    pay = int(g["bet"] * x)
    bal = await pay_win(c, uid, pay)
    await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'mines')", uid, pay - g["bet"])
    await del_game(c, uid, "mines")
    return ok(payout=pay, mult=round(x, 4), mines=sorted(g["mines"]), balance=bal)


async def api_mines_start(r):
    d = await r.json()
    bet, m = as_int(d.get("bet")), as_int(d.get("mines"))
    if m not in (1, 3, 5, 10, 20):
        raise Err("Неверное число мин")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        if await get_game(c, uid, "mines"):
            raise Err("Игра уже идёт")
        bal = await take_bet(c, uid, bet)
        cells = list(range(25))
        mines = [cells.pop(secrets.randbelow(len(cells))) for _ in range(m)]
        await put_game(c, uid, "mines", {"bet": bet, "m": m, "mines": mines, "open": [],
                                         "fate": "win" if rnd() < MINES_WIN_CHANCE else "lose"})
    return ok(balance=bal)


async def api_mines_open(r):
    i = (await r.json()).get("i")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "mines")
        if not g or not isinstance(i, int) or not 0 <= i < 25 or i in g["open"]:
            raise Err("Нельзя открыть клетку")
        mines = set(g["mines"])
        if MINES_WIN_CHANCE >= 0 and not g["open"]:  # шанс раунда (показан игроку в интерфейсе)
            if g["fate"] == "win" and i in mines:
                free = [x for x in range(25) if x != i and x not in mines]
                mines.discard(i)
                mines.add(free[secrets.randbelow(len(free))])
            elif g["fate"] != "win" and i not in mines:
                mines.discard(sorted(mines)[secrets.randbelow(len(mines))])
                mines.add(i)
            g["mines"] = sorted(mines)
        if i in mines:
            await del_game(c, uid, "mines")
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'mines')", uid, -g["bet"])
            return ok(boom=True, mines=sorted(mines), balance=await c.fetchval(
                "SELECT balance FROM users WHERE id=$1", uid))
        g["open"].append(i)
        if len(g["open"]) == 25 - g["m"]:
            return await mines_cashout(c, uid, g)
        await put_game(c, uid, "mines", g)
        return ok(safe=True, mult=round(mines_mult(len(g["open"]), g["m"]), 4))


async def api_mines_cashout(r):
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "mines")
        if not g or not g["open"]:
            raise Err("Откройте хотя бы одну клетку")
        return await mines_cashout(c, uid, g)


# ---------- Краш ----------
def crash_point():
    return min(1000.0, max(1.0, math.floor(100 * CRASH_EDGE / (1 - rnd())) / 100))


async def crash_settle(c, uid, g, now):
    """Авто-вывод и взрыв считает сервер. Возвращает (g, crashed)."""
    cp = g["cp"]
    m = math.exp(GROW * max(0.0, now - g["t0"]))
    if not g["cashed"] and g["auto"] >= 1.01 and g["auto"] < cp and m >= g["auto"]:
        pay = int(g["bet"] * g["auto"])
        await pay_win(c, uid, pay)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'crash')", uid, pay - g["bet"])
        g["cashed"] = pay
    if m >= cp:
        if not g["cashed"]:
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'crash')", uid, -g["bet"])
        await del_game(c, uid, "crash")
        return g, True
    await put_game(c, uid, "crash", g)
    return g, False


async def api_crash_start(r):
    d = await r.json()
    bet = as_int(d.get("bet"))
    auto = d.get("auto") or 0
    auto = round(float(auto), 2) if isinstance(auto, (int, float)) and auto >= 1.01 else 0
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        old = await get_game(c, uid, "crash")
        if old:
            _, crashed = await crash_settle(c, uid, old, time.time())
            if not crashed:
                raise Err("Игра уже идёт")
        bal = await take_bet(c, uid, bet)
        await put_game(c, uid, "crash", {"bet": bet, "auto": auto, "cp": crash_point(),
                                         "t0": time.time() + COUNTDOWN, "cashed": 0})
    return ok(balance=bal)


async def api_crash_status(r):
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "crash")
        if not g:
            raise Err("Нет игры")
        g, crashed = await crash_settle(c, uid, g, time.time())
        bal = await c.fetchval("SELECT balance FROM users WHERE id=$1", uid)
    m = math.exp(GROW * max(0.0, time.time() - g["t0"]))
    out = dict(m=round(min(m, g["cp"]), 4), crashed=crashed, cashed=g["cashed"], balance=bal)
    if crashed:
        out["cp"] = g["cp"]
    return ok(**out)


async def api_crash_cash(r):
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "crash")
        if not g:
            raise Err("Нет игры")
        now = time.time()
        g, crashed = await crash_settle(c, uid, g, now)
        if crashed:
            return ok(crashed=True, cp=g["cp"])
        if g["cashed"]:
            raise Err("Уже забрано")
        if now < g["t0"]:
            raise Err("Ракета ещё не взлетела")
        mult = math.floor(math.exp(GROW * (now - g["t0"])) * 100) / 100
        pay = int(g["bet"] * mult)
        bal = await pay_win(c, uid, pay)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'crash')", uid, pay - g["bet"])
        g["cashed"] = pay
        await put_game(c, uid, "crash", g)
    return ok(pay=pay, mult=mult, balance=bal)


# ---------- Лесенка ----------
def ladder_mult(n, cells):
    return LADDER_EDGE * (cells / (cells - 1)) ** n


async def ladder_cashout(c, uid, g):
    x = ladder_mult(len(g["picks"]), g["c"])
    pay = min(int(g["bet"] * x), LADDER_MAX_WIN)
    bal = await pay_win(c, uid, pay)
    await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'ladder')", uid, pay - g["bet"])
    await del_game(c, uid, "ladder")
    return ok(payout=pay, mult=round(x, 4), traps=g["traps"], balance=bal)


async def api_ladder_start(r):
    d = await r.json()
    bet, cells = as_int(d.get("bet")), as_int(d.get("c"))
    if cells not in (2, 3, 4):
        raise Err("Неверный режим")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        if await get_game(c, uid, "ladder"):
            raise Err("Игра уже идёт")
        bal = await take_bet(c, uid, bet)
        # ловушки раскладываются честно, один раз, при старте
        await put_game(c, uid, "ladder", {"bet": bet, "c": cells, "picks": [],
                                          "traps": [secrets.randbelow(cells) for _ in range(LADDER_ROWS)]})
    return ok(balance=bal)


async def api_ladder_pick(r):
    i = (await r.json()).get("i")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "ladder")
        if not g or isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < g["c"]:
            raise Err("Нельзя выбрать клетку")
        row = len(g["picks"])
        if i == g["traps"][row]:
            await del_game(c, uid, "ladder")
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'ladder')", uid, -g["bet"])
            return ok(boom=True, traps=g["traps"],
                      balance=await c.fetchval("SELECT balance FROM users WHERE id=$1", uid))
        g["picks"].append(i)
        if len(g["picks"]) == LADDER_ROWS:
            return await ladder_cashout(c, uid, g)
        await put_game(c, uid, "ladder", g)
        return ok(safe=True, mult=round(ladder_mult(len(g["picks"]), g["c"]), 4))


async def api_ladder_cashout(r):
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        g = await get_game(c, uid, "ladder")
        if not g or not g["picks"]:
            raise Err("Поднимитесь хотя бы на одну ступень")
        return await ladder_cashout(c, uid, g)


# ---------- Плинко ----------
async def api_plinko_play(r):
    bet = as_int((await r.json()).get("bet"))
    uid = r["uid"]
    path = [secrets.randbelow(2) for _ in range(len(PLINKO) - 1)]
    mult = PLINKO[sum(path)]
    pay = int(bet * mult)
    async with pool.acquire() as c, c.transaction():
        bal = await take_bet(c, uid, bet)
        if pay > bet:
            bal = await pay_win(c, uid, pay)
        elif pay > 0:
            bal = await c.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 RETURNING balance", uid, pay)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'plinko')", uid, pay - bet)
    return ok(path=path, mult=mult, payout=pay, balance=bal)


# ---------- Промокоды (игрок) ----------
async def api_promo_use(r):
    code = str((await r.json()).get("code", "")).strip().upper()
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        p = await c.fetchrow("SELECT amount,uses,used FROM promos WHERE code=$1 "
                             "AND (expires IS NULL OR expires>now()) FOR UPDATE", code)
        if not p or (p["uses"] and p["used"] >= p["uses"]):
            raise Err("Промокод недействителен")
        if not await c.fetchval("INSERT INTO promo_uses(code,user_id) VALUES($1,$2) "
                                "ON CONFLICT DO NOTHING RETURNING 1", code, uid):
            raise Err("Вы уже активировали этот промокод")
        await c.execute("UPDATE promos SET used=used+1 WHERE code=$1", code)
        bal = await c.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 RETURNING balance", uid, p["amount"])
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'promo')", uid, p["amount"])
    return ok(amount=p["amount"], balance=bal)


# ---------- NFT ----------
async def api_nft_buy(r):
    k = as_int((await r.json()).get("k"))
    if not 0 <= k < len(PRICES):
        raise Err("Нет такого NFT")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        bal = await c.fetchval("UPDATE users SET balance=balance-$2 WHERE id=$1 AND balance>=$2 "
                               "RETURNING balance", uid, PRICES[k])
        if bal is None:
            raise Err("Недостаточно звёзд")
        n = 1000 + secrets.randbelow(9000)
        nid = await c.fetchval("INSERT INTO nfts(user_id,k,n) VALUES($1,$2,$3) RETURNING id", uid, k, n)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'nft_buy')", uid, -PRICES[k])
    return ok(nft={"id": nid, "k": k, "n": n}, balance=bal)


async def own_nft(c, uid, nid):
    row = await c.fetchrow("SELECT id,k,n FROM nfts WHERE id=$1 AND user_id=$2 AND status='own' FOR UPDATE",
                           nid, uid)
    if not row:
        raise Err("NFT не найден")
    return row


async def api_nft_sell(r):
    nid, uid = as_int((await r.json()).get("id")), r["uid"]
    async with pool.acquire() as c, c.transaction():
        row = await own_nft(c, uid, nid)
        await c.execute("UPDATE nfts SET status='sold' WHERE id=$1", nid)
        bal = await c.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 RETURNING balance",
                               uid, PRICES[row["k"]])
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'nft_sell')", uid, PRICES[row["k"]])
    return ok(balance=bal)


async def api_nft_upgrade(r):
    """Улучшение NFT: свой NFT -> более дорогой. Шанс = UP_EDGE * цена_своего / цена_нового, только от 1% до 57%.
    Победа: получаешь новый NFT. Поражение: свой NFT сгорает."""
    d = await r.json()
    nid, k = as_int(d.get("id")), as_int(d.get("k"))
    if not 0 <= k < len(PRICES):
        raise Err("Нет такого NFT")
    uid = r["uid"]
    async with pool.acquire() as c, c.transaction():
        row = await own_nft(c, uid, nid)
        p, q = PRICES[row["k"]], PRICES[k]
        if q <= p:
            raise Err("Выберите NFT дороже")
        ch = UP_EDGE * p / q
        if ch > UP_TOP_CHANCE + 1e-9:
            raise Err("Шанс выше %d%%, выберите NFT подороже" % round(UP_TOP_CHANCE * 100))
        if ch < UP_LOW_CHANCE - 1e-9:
            raise Err("Шанс ниже %d%%, выберите NFT подешевле" % round(UP_LOW_CHANCE * 100))
        roll = rnd()
        win = roll < ch
        await c.execute("UPDATE nfts SET status=$2 WHERE id=$1", nid, "upgraded" if win else "burned")
        new = None
        if win:
            n = 1000 + secrets.randbelow(9000)
            new = {"id": await c.fetchval("INSERT INTO nfts(user_id,k,n) VALUES($1,$2,$3) RETURNING id", uid, k, n),
                   "k": k, "n": n}
        await c.execute("UPDATE users SET ups=ups+1, wins=wins+$2 WHERE id=$1", uid, 1 if win else 0)
        await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,0,'nft_up')", uid)
    return ok(r=roll, win=win, chance=ch, nft=new)


async def api_nft_withdraw(r):
    nid, uid = as_int((await r.json()).get("id")), r["uid"]
    async with pool.acquire() as c, c.transaction():
        row = await own_nft(c, uid, nid)
        await c.execute("UPDATE nfts SET status='withdraw' WHERE id=$1", nid)
        name = await c.fetchval("SELECT name FROM users WHERE id=$1", uid)
    await notify_admins(f"🎁 Заявка на вывод NFT: {NAMES[row['k']]} #{row['n']} (nft_id {nid})\n"
                        f"Игрок: @{name} (id {uid})")
    return ok()


# ---------- Админка ----------
def adm(r):
    if not r.get("admin"):
        raise Err("Нет доступа")


async def find_uid(d):
    """Игрок по id или @username (поля id / q)."""
    if d.get("id") is not None:
        uid = as_int(d["id"])
    else:
        q = str(d.get("q", "")).strip().lstrip("@")
        if not q:
            raise Err("Введите ID или @username")
        uid = int(q) if q.isdigit() else await pool.fetchval(
            "SELECT id FROM users WHERE lower(name)=lower($1) ORDER BY created DESC LIMIT 1", q)
    if not uid or not await pool.fetchval("SELECT 1 FROM users WHERE id=$1", uid):
        raise Err("Игрок не найден")
    return uid


async def api_admin_stats(r):
    adm(r)
    q = pool.fetchval
    bot_bal = None
    try:
        bot_bal = (await sg("getMyStarBalance")).get("result")
        if isinstance(bot_bal, dict):
            bot_bal = bot_bal.get("amount")
    except Exception:
        pass
    gk = ["up", "mines", "crash", "ladder", "plinko"]
    rows = await pool.fetch("SELECT kind, COALESCE(-SUM(delta),0)::bigint AS p FROM ledger "
                            "WHERE kind = ANY($1::text[]) GROUP BY kind", gk)
    pg = {k: 0 for k in gk}
    pg.update({x["kind"]: int(x["p"]) for x in rows})
    return ok(
        users=await q("SELECT count(*) FROM users"),
        debt=await q("SELECT COALESCE(SUM(balance),0)::bigint FROM users"),
        online=online_now(),
        new24=await q("SELECT count(*) FROM users WHERE created > now() - interval '24 hours'"),
        wag24=await q("SELECT COALESCE(SUM(amount),0)::bigint FROM bets WHERE created > now() - interval '24 hours'"),
        wag_total=await q("SELECT COALESCE(SUM(amount),0)::bigint FROM bets"),
        dep_total=await q("SELECT COALESCE(SUM(delta),0)::bigint FROM ledger WHERE kind='deposit'"),
        dep24=await q("SELECT COALESCE(SUM(delta),0)::bigint FROM ledger WHERE kind='deposit' "
                      "AND created > now() - interval '24 hours'"),
        wd_total=await q("SELECT COALESCE(SUM(amount),0)::bigint FROM withdrawals WHERE status='done'"),
        wd24=await q("SELECT COALESCE(SUM(amount),0)::bigint FROM withdrawals WHERE status='done' "
                     "AND created > now() - interval '24 hours'"),
        wd_pend=await q("SELECT COALESCE(SUM(amount),0)::bigint FROM withdrawals "
                        "WHERE status IN ('pending','processing')"),
        given=await q("SELECT COALESCE(SUM(delta),0)::bigint FROM ledger "
                      "WHERE kind IN ('admin','admin_all','promo') AND delta>0"),
        nft_own=await q("SELECT count(*) FROM nfts WHERE status='own'"),
        pg=pg, profit=sum(pg.values()),
        pend=await q("SELECT count(*) FROM withdrawals WHERE status IN ('pending','processing')"),
        nftreq=await q("SELECT count(*) FROM nfts WHERE status='withdraw'"),
        bot=bot_bal)


async def api_admin_user(r):
    adm(r)
    uid = await find_uid({"q": (await r.json()).get("q", "")})
    u = await pool.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    n = await pool.fetchval("SELECT count(*) FROM nfts WHERE user_id=$1 AND status='own'", uid)
    bu = u["ban_until"]
    banned = bool(u["banned"]) and (bu is None or bu > datetime.now(timezone.utc))
    return ok(user=dict(id=u["id"], name=u["name"], balance=u["balance"], wag=u["wag"], ups=u["ups"],
                        wins=u["wins"], best=u["best"], banned=banned,
                        ban_until=str(bu)[:16] + " UTC" if (banned and bu) else None,
                        frozen=u["frozen"], admin=uid in ADMINS or u["is_admin"], nft=n))


async def api_admin_balance(r):
    adm(r)
    d = await r.json()
    uid, amount = await find_uid(d), as_int(d.get("amount"))
    if amount == 0:
        raise Err("Сумма не должна быть 0")
    if amount > 0:
        await credit(uid, amount, "admin")
    else:
        async with pool.acquire() as c, c.transaction():
            if await c.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 AND balance>=$3 RETURNING id",
                                uid, amount, -amount) is None:
                raise Err("У игрока меньше звёзд")
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'admin')", uid, amount)
    bal = await balance(uid)
    log.info("admin %s balance %s %+d", r["uid"], uid, amount)
    spawn(tell(uid, f"💰 Вам начислено {amount} ⭐\nБаланс: {bal} ⭐" if amount > 0
               else f"ℹ️ Администратор списал {-amount} ⭐\nБаланс: {bal} ⭐"))
    return ok(balance=bal)


async def api_admin_zero(r):
    adm(r)
    uid = await find_uid(await r.json())
    async with pool.acquire() as c, c.transaction():
        old = await c.fetchval("SELECT balance FROM users WHERE id=$1 FOR UPDATE", uid)
        if old:
            await c.execute("UPDATE users SET balance=0 WHERE id=$1", uid)
            await c.execute("INSERT INTO ledger(user_id,delta,kind) VALUES($1,$2,'admin')", uid, -old)
    log.info("admin %s zero %s (-%s)", r["uid"], uid, old)
    return ok()


async def api_admin_ban(r):
    adm(r)
    d = await r.json()
    uid = await find_uid(d)
    if uid in ADMINS:
        raise Err("Админа нельзя забанить")
    on, until = bool(d.get("on")), None
    if on:
        n = as_int(d.get("duration") or 0)
        if not 0 <= n <= 100000:
            raise Err("Неверный срок бана")
        if n:
            until = datetime.now(timezone.utc) + (timedelta(days=n) if d.get("unit") == "d" else timedelta(hours=n))
    await pool.execute("UPDATE users SET banned=$2, ban_until=$3 WHERE id=$1", uid, on, until)
    return ok()


async def api_admin_freeze(r):
    adm(r)
    d = await r.json()
    await pool.execute("UPDATE users SET frozen=$2 WHERE id=$1", await find_uid(d), bool(d.get("on")))
    return ok()


async def api_admin_frozen(r):
    adm(r)
    rows = await pool.fetch("SELECT id,name,balance FROM users WHERE frozen ORDER BY id LIMIT 50")
    return ok(items=[dict(x) for x in rows])


async def api_admin_setadmin(r):
    adm(r)
    d = await r.json()
    uid, on = await find_uid(d), bool(d.get("on"))
    if not on and uid in ADMINS:
        raise Err("Это главный админ, снять нельзя")
    await pool.execute("UPDATE users SET is_admin=$2 WHERE id=$1", uid, on)
    return ok()


async def api_admin_giveall(r):
    adm(r)
    amount = as_int((await r.json()).get("amount"))
    if not 1 <= amount <= 10_000_000:
        raise Err("Сумма от 1 до 10 000 000")
    async with pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO ledger(user_id,delta,kind) SELECT id,$1::bigint,'admin_all' "
                        "FROM users WHERE NOT banned", amount)
        res = await c.execute("UPDATE users SET balance=balance+$1 WHERE NOT banned", amount)
    log.info("admin %s giveall %d", r["uid"], amount)
    return ok(count=int(res.split()[-1]))


async def api_admin_zeroall(r):
    adm(r)
    async with pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO ledger(user_id,delta,kind) SELECT id,-balance,'admin_zero' "
                        "FROM users WHERE balance>0")
        res = await c.execute("UPDATE users SET balance=0 WHERE balance>0")
    log.info("admin %s zeroall", r["uid"])
    return ok(count=int(res.split()[-1]))


async def api_admin_pstats(r):
    adm(r)
    uid = await find_uid(await r.json())
    u = await pool.fetchrow("SELECT name,wag FROM users WHERE id=$1", uid)
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(delta) FILTER (WHERE kind='deposit'),0)::bigint AS dep,"
        " COALESCE(-SUM(delta) FILTER (WHERE kind IN ('withdraw','withdraw_refund')),0)::bigint AS wd,"
        " COALESCE(SUM(delta) FILTER (WHERE kind IN ('up','mines','crash','ladder','plinko') AND delta>0),0)::bigint AS won,"
        " COALESCE(-SUM(delta) FILTER (WHERE kind IN ('up','mines','crash','ladder','plinko') AND delta<0),0)::bigint AS lost "
        "FROM ledger WHERE user_id=$1", uid)
    games = await pool.fetchval("SELECT count(*) FROM bets WHERE user_id=$1", uid)
    return ok(name=u["name"], stats=dict(deposited=row["dep"], withdrawn=row["wd"], won=row["won"],
                                         lost=row["lost"], wagered=u["wag"], games=games))


async def api_admin_givenft(r):
    adm(r)
    d = await r.json()
    uid = await find_uid(d)
    if d.get("k") is not None:                       # выбор из меню: точный номер NFT
        k = as_int(d["k"])
        if not 0 <= k < len(NAMES):
            raise Err("Нет такого NFT")
        ks = [k]
    else:                                            # старый способ: часть названия
        name = str(d.get("name", "")).lower().strip()
        ks = [i for i, n in enumerate(NAMES) if name and name in n.lower()]
        if not ks:
            raise Err("Не нашёл такой NFT")
    n = 1000 + secrets.randbelow(9000)
    await pool.execute("INSERT INTO nfts(user_id,k,n) VALUES($1,$2,$3)", uid, ks[0], n)
    spawn(tell(uid, f"🎁 Вам выдан NFT: {NAMES[ks[0]]} #{n}"))
    return ok(name=NAMES[ks[0]])


# --- промокоды (админ)
async def api_admin_promo_create(r):
    adm(r)
    d = await r.json()
    amount = as_int(d.get("amount"))
    uses, hours = as_int(d.get("uses") or 0), as_int(d.get("hours") or 0)
    if not 1 <= amount <= 10_000_000 or uses < 0 or hours < 0:
        raise Err("Неверные параметры")
    code = str(d.get("code", "")).strip().upper() or secrets.token_hex(4).upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{3,32}", code):
        raise Err("Код: 3-32 символа, латиница/цифры/-/_")
    exp = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
    if not await pool.fetchval("INSERT INTO promos(code,amount,uses,expires) VALUES($1,$2,$3,$4) "
                               "ON CONFLICT DO NOTHING RETURNING 1", code, amount, uses, exp):
        raise Err("Такой код уже существует")
    return ok(code=code)


async def api_admin_promo_list(r):
    adm(r)
    rows = await pool.fetch("SELECT code,amount,uses,used,expires FROM promos ORDER BY created DESC LIMIT 20")
    return ok(items=[dict(code=x["code"], amount=x["amount"], uses=x["uses"], used=x["used"],
                          expires=str(x["expires"])[:16] + " UTC" if x["expires"] else None) for x in rows])


async def api_admin_promo_delete(r):
    adm(r)
    await pool.execute("DELETE FROM promos WHERE code=$1", str((await r.json()).get("code", "")).upper())
    return ok()


async def api_admin_nftreq(r):
    adm(r)
    rows = await pool.fetch("SELECT n.id,n.k,n.n,n.user_id,u.name FROM nfts n JOIN users u ON u.id=n.user_id "
                            "WHERE n.status='withdraw' ORDER BY n.id LIMIT 30")
    return ok(items=[dict(id=x["id"], title=f"{NAMES[x['k']]} #{x['n']}", uid=x["user_id"], name=x["name"]) for x in rows])


async def api_admin_nftdone(r):
    adm(r)
    await pool.execute("UPDATE nfts SET status='withdrawn' WHERE id=$1 AND status='withdraw'",
                       as_int((await r.json()).get("id")))
    return ok()


async def api_admin_broadcast(r):
    adm(r)
    text = str((await r.json()).get("text", "")).strip()
    if not 1 <= len(text) <= 1000:
        raise Err("Текст от 1 до 1000 символов")
    ids = [x["id"] for x in await pool.fetch("SELECT id FROM users WHERE NOT banned")]

    async def run():
        for i in ids:
            try:
                await bot.send_message(i, text)
            except Exception:
                pass
            await asyncio.sleep(0.05)
    asyncio.create_task(run())
    return ok(count=len(ids))



async def index(_):
    return web.FileResponse(Path(__file__).with_name("index.html"))


async def health(_):
    return web.Response(text="ok")


# ---------- Бот ----------
@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🐻 Играть", web_app=WebAppInfo(url=BASE_URL + "/"))]])
    await m.answer("Добро пожаловать в BearUp! Жми кнопку ниже.", reply_markup=kb)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    if sp.currency != "XTR":
        return
    uid = m.from_user.id
    await ensure_user(uid, m.from_user.username or m.from_user.first_name)
    if await credit(uid, sp.total_amount, "deposit", f"dep:{sp.telegram_payment_charge_id}"):
        await m.answer(f"✅ Зачислено {sp.total_amount} ⭐. Баланс: {await balance(uid)} ⭐")


@dp.message(Command("stat"))
async def stat(m: Message):
    if m.from_user.id not in ADMINS:
        return
    bal = await sg("getMyStarBalance")
    users = await pool.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
    pend = await pool.fetchval("SELECT count(*) FROM withdrawals WHERE status='pending'")
    await m.answer(f"Баланс бота: {bal.get('result')}\nДолг игрокам: {users} ⭐\nВыводов pending: {pend}")


@dp.message(Command("givenft"))
async def givenft(m: Message):
    """/givenft <user_id> <часть названия> — зачислить NFT в инвентарь."""
    if m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await m.answer("Формат: /givenft <user_id> <название>")
    ks = [i for i, n in enumerate(NAMES) if parts[2].lower() in n.lower()]
    if not ks:
        return await m.answer("Не нашёл такой NFT")
    uid, k = int(parts[1]), ks[0]
    await ensure_user(uid, str(uid))
    await pool.execute("INSERT INTO nfts(user_id,k,n) VALUES($1,$2,$3)", uid, k, 1000 + secrets.randbelow(9000))
    await m.answer(f"✅ {NAMES[k]} зачислен игроку {uid}")


# ---------- Запуск ----------
async def main():
    global pool, http
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5, statement_cache_size=0)
    await pool.execute(SCHEMA)
    http = aiohttp.ClientSession()
    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/api/state", api_state), web.get("/api/sync", api_sync), web.get("/api/top", api_top),
        web.post("/api/deposit", api_deposit), web.post("/api/withdraw", api_withdraw),
        web.post("/api/up", api_up),
        web.post("/api/mines/start", api_mines_start), web.post("/api/mines/open", api_mines_open),
        web.post("/api/mines/cashout", api_mines_cashout),
        web.post("/api/crash/start", api_crash_start), web.get("/api/crash/status", api_crash_status),
        web.post("/api/crash/cash", api_crash_cash),
        web.post("/api/ladder/start", api_ladder_start), web.post("/api/ladder/pick", api_ladder_pick),
        web.post("/api/ladder/cashout", api_ladder_cashout),
        web.post("/api/plinko/play", api_plinko_play), web.post("/api/promo/use", api_promo_use),
        web.post("/api/nft/buy", api_nft_buy), web.post("/api/nft/sell", api_nft_sell),
        web.post("/api/nft/withdraw", api_nft_withdraw), web.post("/api/nft/upgrade", api_nft_upgrade),
        web.get("/api/admin/stats", api_admin_stats), web.post("/api/admin/user", api_admin_user),
        web.post("/api/admin/balance", api_admin_balance), web.post("/api/admin/ban", api_admin_ban),
        web.post("/api/admin/givenft", api_admin_givenft), web.get("/api/admin/nftreq", api_admin_nftreq),
        web.post("/api/admin/nftdone", api_admin_nftdone), web.post("/api/admin/broadcast", api_admin_broadcast),
        web.post("/api/admin/zero", api_admin_zero), web.post("/api/admin/freeze", api_admin_freeze),
        web.get("/api/admin/frozen", api_admin_frozen), web.post("/api/admin/setadmin", api_admin_setadmin),
        web.post("/api/admin/giveall", api_admin_giveall), web.post("/api/admin/zeroall", api_admin_zeroall),
        web.post("/api/admin/pstats", api_admin_pstats),
        web.post("/api/admin/promo/create", api_admin_promo_create),
        web.get("/api/admin/promo/list", api_admin_promo_list),
        web.post("/api/admin/promo/delete", api_admin_promo_delete),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.delete_webhook()
    log.info("started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
