#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KBT License v2 - сервер лицензий + Telegram-бот (платная подписка по дням).

Сценарий:
  1) пользователь открывает KBT -> видит свой HWID (код устройства);
  2) присылает HWID боту;
  3) ты (админ) в боте выдаёшь ключ на N дней (или продлеваешь/блокируешь);
  4) бот даёт код-ключ, пользователь вводит его в браузер -> подписка активна;
  5) время считает СЕРВЕР, поэтому доступ можно продлить/отнять удалённо в любой момент.

Никаких внешних библиотек - только Python 3.8+. Запуск: python3 kbt_license.py
Настройки: рядом файл kbt.conf (см. kbt.conf.example) или переменные окружения.

Команды пользователя:  /start, /id, прислать HWID, /status
Команды админа:        /users, /user <id|@ник|hwid>, /gen <дней> [hwid], /find <hwid>, /stats
                       (а также кнопки под сообщениями: +дни / блок / разблок / обнулить / ключ)
"""

import json, os, re, sqlite3, ssl, threading, time, traceback, secrets, math
import urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400

# ===================== НАСТРОЙКИ =====================
def load_config():
    cfg = {
        "bot_token": "",
        "admin_ids": [],
        "app_secret": "",          # если задан - браузер шлёт его в заголовке X-KBT-Secret
        "port": 8080,
        "db_path": os.path.join(HERE, "kbt.db"),
        "price_per_day": 1,        # рублей за день (для текста в боте; авто-оплаты нет)
        "bot_username": "",        # @имя_бота без @ (для ссылки на экране активации; необязательно)
        "latest_version": "",
        "tg_api": "https://api.telegram.org",  # база Telegram API; на сервере в РФ меняем на Cloudflare-релей
    }
    f = os.path.join(HERE, "kbt.conf")
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8") as fh: cfg.update(json.load(fh))
        except Exception as e: print("kbt.conf не прочитан:", e)
    env = os.environ.get
    if env("BOT_TOKEN"):      cfg["bot_token"] = env("BOT_TOKEN")
    if env("ADMIN_IDS"):      cfg["admin_ids"] = [int(x) for x in re.findall(r"\d+", env("ADMIN_IDS"))]
    if env("APP_SECRET"):     cfg["app_secret"] = env("APP_SECRET")
    if env("PORT"):           cfg["port"] = int(env("PORT"))
    if env("PRICE_PER_DAY"):  cfg["price_per_day"] = int(env("PRICE_PER_DAY"))
    if env("BOT_USERNAME"):   cfg["bot_username"] = env("BOT_USERNAME").lstrip("@")
    if env("LATEST_VERSION"): cfg["latest_version"] = env("LATEST_VERSION")
    if env("TG_API"):         cfg["tg_api"] = env("TG_API").rstrip("/")
    if isinstance(cfg["admin_ids"], (int, str)):
        cfg["admin_ids"] = [int(x) for x in re.findall(r"\d+", str(cfg["admin_ids"]))]
    return cfg

CFG = load_config()
HWID_RE = re.compile(r"^[0-9a-fA-F]{16,128}$")

# ===================== БАЗА =====================
LOCK = threading.Lock()
def now(): return int(time.time())
def iso(ts=None): return datetime.fromtimestamp(ts or now(), timezone.utc).strftime("%Y-%m-%d %H:%M")

def db():
    c = sqlite3.connect(CFG["db_path"], timeout=15); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c

def db_init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY, username TEXT, name TEXT, created INTEGER, note TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, hwid TEXT UNIQUE NOT NULL, tg_id INTEGER,
            expires INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,
            created INTEGER, last_seen INTEGER, last_ver TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS codes(
            code TEXT PRIMARY KEY, hwid TEXT DEFAULT '', days INTEGER,
            used INTEGER DEFAULT 0, used_by TEXT DEFAULT '', created INTEGER)""")
        c.commit()

def q1(sql, args=()):
    with db() as c: return c.execute(sql, args).fetchone()
def qall(sql, args=()):
    with db() as c: return c.execute(sql, args).fetchall()
def run(sql, args=()):
    with LOCK, db() as c: c.execute(sql, args); c.commit()

def upsert_user(tg_id, username, name):
    with LOCK, db() as c:
        c.execute("INSERT INTO users(tg_id,username,name,created) VALUES(?,?,?,?) "
                  "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, name=excluded.name",
                  (tg_id, username or "", name or "", now())); c.commit()

def device_by_hwid(h): return q1("SELECT * FROM devices WHERE hwid=?", (h,))
def device_by_id(i):   return q1("SELECT * FROM devices WHERE id=?", (i,))

def ensure_device(hwid, tg_id=None):
    d = device_by_hwid(hwid)
    if d:
        if tg_id and not d["tg_id"]: run("UPDATE devices SET tg_id=? WHERE id=?", (tg_id, d["id"]))
        return device_by_hwid(hwid)
    run("INSERT INTO devices(hwid,tg_id,expires,created) VALUES(?,?,0,?)", (hwid, tg_id, now()))
    return device_by_hwid(hwid)

def dev_active(d): return (not d["blocked"]) and d["expires"] > now()
def days_left(d):  return max(0, math.ceil((d["expires"] - now()) / DAY)) if d["expires"] > now() else 0

def add_days(dev_id, days):
    d = device_by_id(dev_id)
    if not d: return None
    base = max(now(), d["expires"])          # продлеваем от текущего конца, не сжигаем остаток
    run("UPDATE devices SET expires=?, blocked=0 WHERE id=?", (base + days * DAY, dev_id))
    return device_by_id(dev_id)

def set_expiry(dev_id, ts):   run("UPDATE devices SET expires=? WHERE id=?", (ts, dev_id))
def set_blocked(dev_id, b):   run("UPDATE devices SET blocked=? WHERE id=?", (1 if b else 0, dev_id))

def gen_code(days, hwid=""):
    code = "KBT-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))   # KBT-XXXX-XXXX-XXXX
    run("INSERT INTO codes(code,hwid,days,created) VALUES(?,?,?,?)", (code, hwid or "", days, now()))
    return code

def redeem_code(code, hwid):
    c = q1("SELECT * FROM codes WHERE code=?", (code,))
    if not c:        return (False, "ключ не найден")
    if c["used"]:    return (False, "ключ уже использован")
    if c["hwid"] and c["hwid"].lower() != hwid.lower(): return (False, "ключ выдан для другого устройства")
    ensure_device(hwid)
    d = device_by_hwid(hwid)
    add_days(d["id"], c["days"])
    run("UPDATE codes SET used=1, used_by=? WHERE code=?", (hwid, code))
    return (True, device_by_hwid(hwid))

# ===================== HTTP (его зовёт браузер) =====================
class Api(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("Content-Length", "0"));
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    def _auth_ok(self):
        return (not CFG["app_secret"]) or self.headers.get("X-KBT-Secret", "") == CFG["app_secret"]
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/healthz"): self._send(200, {"ok": True, "service": "kbt-license"})
        elif p == "/api/version": self._send(200, {"latest": CFG["latest_version"]})
        else: self._send(404, {"error": "not_found"})
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p not in ("/api/check", "/api/activate"): return self._send(404, {"error": "not_found"})
        if not self._auth_ok(): return self._send(403, {"status": "error", "reason": "forbidden"})
        try: data = self._body()
        except Exception: return self._send(400, {"status": "error", "reason": "bad_json"})
        hwid = str(data.get("hwid", "")).strip().lower()
        if not HWID_RE.match(hwid): return self._send(200, {"status": "error", "reason": "bad_hwid"})
        try:
            if p == "/api/check":
                d = device_by_hwid(hwid)
                if d: run("UPDATE devices SET last_seen=?, last_ver=? WHERE id=?",
                          (now(), str(data.get("ver", "")), d["id"]))
                if not d:               res = {"status": "unknown"}
                elif d["blocked"]:      res = {"status": "blocked"}
                elif d["expires"] > now(): res = {"status": "active", "expires": d["expires"], "days_left": days_left(d)}
                else:                   res = {"status": "expired", "expires": d["expires"]}
            else:  # /api/activate
                ok, r = redeem_code(str(data.get("code", "")).strip(), hwid)
                if ok: res = {"status": "active", "expires": r["expires"], "days_left": days_left(r)}
                else:  res = {"status": "error", "reason": r}
            res["latest"] = CFG["latest_version"]
            self._send(200, res)
        except Exception:
            traceback.print_exc(); self._send(500, {"status": "error", "reason": "server"})

def run_http():
    ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Api).serve_forever()

# ===================== TELEGRAM =====================
API = CFG["tg_api"] + "/bot{t}/{m}"
# браузерный User-Agent: иначе Cloudflare (релей) режет запросы Python как «бота» (403)
TG_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
def tg(method, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API.format(t=CFG["bot_token"], m=method), data=data,
                                 headers={"User-Agent": TG_UA, "Accept": "application/json, text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[tg]", method, e); return {"ok": False}

def is_admin(uid): return uid in CFG["admin_ids"]
def acc_link(d):
    if d and d["username"]: return "https://t.me/" + d["username"]
    if d: return "tg://user?id=%d" % d["tg_id"]
    return ""

# действия: ("send",chat,text,markup) | ("answer",cbid,text)
def kb(rows): return {"inline_keyboard": rows}
def main_menu(uid):
    if is_admin(uid):
        rows = [["📊 Статистика", "👥 Пользователи"], ["🔑 Выдать ключ"], ["ℹ️ Мой статус", "🆔 Мой ID"]]
    else:
        rows = [["ℹ️ Мой статус"], ["💳 Купить доступ", "❓ Как активировать"]]
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True}
def genkey_kb():
    return kb([[{"text": "7д", "callback_data": "gf:7"},
                {"text": "30д", "callback_data": "gf:30"},
                {"text": "90д", "callback_data": "gf:90"}]])
def manage_kb(dev_id):
    return kb([
        [{"text": "+7 дней", "callback_data": f"d:{dev_id}:7"},
         {"text": "+30", "callback_data": f"d:{dev_id}:30"},
         {"text": "+90", "callback_data": f"d:{dev_id}:90"}],
        [{"text": "🔑 Ключ 30д", "callback_data": f"g:{dev_id}:30"},
         {"text": "Ключ 7д", "callback_data": f"g:{dev_id}:7"}],
        [{"text": "⛔ Блок", "callback_data": f"blk:{dev_id}"},
         {"text": "✅ Разблок", "callback_data": f"ubl:{dev_id}"},
         {"text": "🧹 Обнулить", "callback_data": f"rst:{dev_id}"}],
    ])

def dev_card(d):
    u = q1("SELECT * FROM users WHERE tg_id=?", (d["tg_id"],)) if d["tg_id"] else None
    who = ("@" + u["username"]) if (u and u["username"]) else (str(d["tg_id"]) if d["tg_id"] else "не привязан")
    st = "⛔ заблокирован" if d["blocked"] else (f"✅ активен, осталось {days_left(d)} дн. (до {iso(d['expires'])})"
                                                 if d["expires"] > now() else "⌛ не активен / истёк")
    link = acc_link(u) if u else ""
    return (f"Устройство #{d['id']}\nСтатус: {st}\nПользователь: {who}" + (f"\nАккаунт: {link}" if link else "")
            + f"\nHWID: {d['hwid']}\nПоследний вход: {iso(d['last_seen']) if d['last_seen'] else '-'}")

def handle_message(msg):
    out = []; chat = msg["chat"]["id"]; frm = msg.get("from") or {}
    uid = frm.get("id"); uname = frm.get("username") or ""; name = frm.get("first_name") or ""
    text = (msg.get("text") or "").strip()
    if not uid: return out
    upsert_user(uid, uname, name)
    adm = is_admin(uid)

    BTN = {"📊 Статистика": "/stats", "👥 Пользователи": "/users", "🆔 Мой ID": "/id", "ℹ️ Мой статус": "/status"}
    if text in BTN: text = BTN[text]
    if text in ("🔑 Выдать ключ", "Выдать ключ"):
        if not adm: out.append(("send", chat, "Это только для администратора.", None)); return out
        out.append(("send", chat, "Выбери срок плавающего ключа (сработает на первом устройстве, кто введёт),\nлибо команда: /gen <дней> [hwid]", genkey_kb())); return out
    if text in ("💳 Купить доступ", "Купить доступ"):
        out.append(("send", chat, f"Подписка: {CFG['price_per_day']} ₽ за день.\n\nКак купить:\n1) открой KBT -> Настройки -> «Активация», скопируй код устройства (HWID);\n2) пришли HWID сюда;\n3) напиши @Kirusha_btw и оплати нужное число дней;\n4) получишь ключ и введёшь его в KBT.", main_menu(uid))); return out
    if text in ("❓ Как активировать", "Как активировать"):
        out.append(("send", chat, "Активация по шагам:\n1) KBT -> Настройки -> «Активация»;\n2) скопируй код устройства (HWID);\n3) пришли его сюда одним сообщением;\n4) получи ключ (после оплаты);\n5) введи ключ в KBT -> Активация. Готово.", main_menu(uid))); return out

    if text.startswith("/start"):
        d = q1("SELECT * FROM devices WHERE tg_id=? ORDER BY id DESC LIMIT 1", (uid,))
        hello = ("Привет! Это бот активации KBT.\n\n"
                 "Как активировать:\n"
                 "1) открой KBT -> Настройки -> «Активация»;\n"
                 "2) скопируй свой «код устройства» (HWID);\n"
                 "3) пришли его сюда одним сообщением;\n"
                 "4) получишь ключ и введёшь его в браузере.\n\n"
                 f"Подписка: {CFG['price_per_day']} ₽ за день.")
        out.append(("send", chat, hello + ("\n\n" + dev_card(d) if d else ""), main_menu(uid)))
        if adm: out.append(("send", chat, "Ты администратор. Кнопки снизу, либо команды: /users, /user <id|@ник|hwid>, /gen <дней> [hwid], /stats", None))
        return out

    if text.startswith("/id"):
        out.append(("send", chat, f"Твой Telegram id: {uid}", None)); return out

    if text.startswith("/status"):
        d = q1("SELECT * FROM devices WHERE tg_id=? ORDER BY id DESC LIMIT 1", (uid,))
        out.append(("send", chat, dev_card(d) if d else "Устройство ещё не привязано. Пришли HWID из KBT.", None)); return out

    # ---- админские команды ----
    if adm and text.startswith("/stats"):
        t = q1("SELECT COUNT(*) n FROM devices")["n"]; a = q1("SELECT COUNT(*) n FROM devices WHERE blocked=0 AND expires>?", (now(),))["n"]
        us = q1("SELECT COUNT(*) n FROM users")["n"]
        out.append(("send", chat, f"Пользователей: {us}\nУстройств: {t}, активных: {a}", None)); return out

    if adm and text.startswith("/users"):
        m = re.search(r"\d+", text); page = int(m.group()) if m else 1; per = 10
        rows = qall("SELECT * FROM users ORDER BY created DESC LIMIT ? OFFSET ?", (per, (page - 1) * per))
        if not rows: out.append(("send", chat, "Пусто.", None)); return out
        for u in rows:
            d = q1("SELECT * FROM devices WHERE tg_id=? ORDER BY id DESC LIMIT 1", (u["tg_id"],))
            who = ("@" + u["username"]) if u["username"] else str(u["tg_id"])
            st = "нет устройства"
            if d: st = ("⛔" if d["blocked"] else (f"✅ {days_left(d)}д" if d["expires"] > now() else "⌛ истёк"))
            line = f"{who} - {st}\n{acc_link(u)}"
            out.append(("send", chat, line, manage_kb(d["id"]) if d else None))
        out.append(("send", chat, f"Страница {page}. Дальше: /users {page+1}", None)); return out

    if adm and (text.startswith("/user") or text.startswith("/find")):
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        d = None
        if HWID_RE.match(arg): d = device_by_hwid(arg.lower())
        elif arg.startswith("@"):
            u = q1("SELECT * FROM users WHERE lower(username)=?", (arg[1:].lower(),))
            if u: d = q1("SELECT * FROM devices WHERE tg_id=? ORDER BY id DESC LIMIT 1", (u["tg_id"],))
        elif arg.isdigit():
            d = q1("SELECT * FROM devices WHERE tg_id=? ORDER BY id DESC LIMIT 1", (int(arg),)) or device_by_id(int(arg))
        if not d: out.append(("send", chat, "Не найдено. /user <tg_id | @ник | hwid>", None)); return out
        out.append(("send", chat, dev_card(d), manage_kb(d["id"]))); return out

    if adm and text.startswith("/gen"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            out.append(("send", chat, "Как: /gen <дней> [hwid]\nБез hwid - «плавающий» ключ (сработает на первом устройстве).", None)); return out
        days = int(parts[1]); hwid = parts[2].lower() if len(parts) > 2 and HWID_RE.match(parts[2]) else ""
        code = gen_code(days, hwid)
        out.append(("send", chat, f"Ключ на {days} дн.{(' для ' + hwid) if hwid else ' (плавающий)'}:\n`{code}`\n\n"
                                  "Пользователь вводит его в KBT -> Настройки -> Активация.", None))
        return out

    # ---- обычное сообщение: считаем HWID ----
    hwid = text.lower()
    if HWID_RE.match(hwid):
        d = device_by_hwid(hwid)
        if d and d["tg_id"] and d["tg_id"] != uid:
            out.append(("send", chat, "Этот код устройства уже привязан к другому аккаунту.", None)); return out
        d = ensure_device(hwid, uid)
        out.append(("send", chat, "Готово, устройство привязано к тебе.\n\n" + dev_card(d)
                    + "\n\nЧтобы активировать подписку - оплати и дождись ключа.", None))
        for aid in CFG["admin_ids"]:
            out.append(("send", aid, "🆕 Привязано устройство:\n" + dev_card(d), manage_kb(d["id"])))
        return out

    out.append(("send", chat, "Пришли код устройства (HWID) из KBT -> Настройки -> Активация, либо /start.", None))
    return out

def handle_callback(cq):
    out = []; uid = (cq.get("from") or {}).get("id"); data = cq.get("data", "")
    if not is_admin(uid): return [("answer", cq["id"], "Только для администратора.")]
    mg = re.match(r"^gf:(\d+)$", data)
    if mg:
        days = int(mg.group(1)); code = gen_code(days, "")
        return [("answer", cq["id"], "Ключ создан"), ("send", uid, f"Плавающий ключ на {days} дн.:\n`{code}`\nСработает на первом устройстве, которое его введёт.", None)]
    m = re.match(r"^(d|g|blk|ubl|rst):(\d+)(?::(\d+))?$", data)
    if not m: return [("answer", cq["id"], "")]
    act, dev_id, n = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    d = device_by_id(dev_id)
    if not d: return [("answer", cq["id"], "Устройство исчезло.")]
    if act == "d":
        d = add_days(dev_id, n); out.append(("answer", cq["id"], f"+{n} дн."))
        if d["tg_id"]: out.append(("send", d["tg_id"], f"✅ Подписка продлена на {n} дн. Осталось {days_left(d)} дн.", None))
    elif act == "blk":
        set_blocked(dev_id, True); out.append(("answer", cq["id"], "Заблокировано"))
        if d["tg_id"]: out.append(("send", d["tg_id"], "⛔ Доступ к KBT отключён.", None))
    elif act == "ubl":
        set_blocked(dev_id, False); out.append(("answer", cq["id"], "Разблокировано"))
    elif act == "rst":
        set_expiry(dev_id, 0); out.append(("answer", cq["id"], "Время обнулено"))
        if d["tg_id"]: out.append(("send", d["tg_id"], "⌛ Время подписки обнулено.", None))
    elif act == "g":
        code = gen_code(n, d["hwid"])
        out.append(("answer", cq["id"], "Ключ создан"))
        out.append(("send", uid, f"Ключ на {n} дн. для #{dev_id}:\n`{code}`", None))
        if d["tg_id"]: out.append(("send", d["tg_id"], f"🔑 Твой ключ на {n} дн.:\n`{code}`\nВведи его в KBT -> Настройки -> Активация.", None))
    d = device_by_id(dev_id)
    if act in ("blk", "ubl", "rst", "d"):
        out.append(("edit", cq["message"]["chat"]["id"], cq["message"]["message_id"], dev_card(d), manage_kb(dev_id)))
    return out

def process_update(upd):
    if "message" in upd and upd["message"].get("from"): return handle_message(upd["message"])
    if "callback_query" in upd: return handle_callback(upd["callback_query"])
    return []

def _pm(text): return "Markdown" if ("`" in (text or "")) else None   # разметка только когда есть код в ``
def execute(actions):
    for a in actions:
        if a[0] == "send":
            r = tg("sendMessage", chat_id=a[1], text=a[2], reply_markup=a[3], parse_mode=_pm(a[2]), disable_web_page_preview=True)
            if not r.get("ok"): tg("sendMessage", chat_id=a[1], text=a[2], reply_markup=a[3], disable_web_page_preview=True)
        elif a[0] == "edit":
            r = tg("editMessageText", chat_id=a[1], message_id=a[2], text=a[3], reply_markup=a[4], parse_mode=_pm(a[3]), disable_web_page_preview=True)
            if not r.get("ok"): tg("editMessageText", chat_id=a[1], message_id=a[2], text=a[3], reply_markup=a[4], disable_web_page_preview=True)
        elif a[0] == "answer": tg("answerCallbackQuery", callback_query_id=a[1], text=a[2])

def run_bot():
    if not CFG["bot_token"]:
        print("[bot] токен не задан - бот выключен, сервер лицензий работает.")
        while True: time.sleep(3600)
    me = tg("getMe")
    print(f"[bot] запущен как @{me['result']['username']}" if me.get("ok") else "[bot] токен/интернет?")
    if not me.get("ok"):
        while True: time.sleep(3600)
    offset = None
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
            if not r.get("ok"): time.sleep(3); continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                try: execute(process_update(upd))
                except Exception: traceback.print_exc()
        except Exception:
            traceback.print_exc(); time.sleep(3)

def main():
    db_init()
    print("=== KBT License v2 ===  админов:", len(CFG["admin_ids"]), "| секрет:", "да" if CFG["app_secret"] else "нет",
          "| порт:", CFG["port"])
    threading.Thread(target=run_http, daemon=True).start()
    print(f"[http] сервер лицензий на порту {CFG['port']}")
    run_bot()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\nОстановлено.")
