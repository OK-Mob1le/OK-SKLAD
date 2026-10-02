#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OK-Mobile · бот входа и подписки (тарифы)

Как это работает
  1. Новый пользователь регистрируется в приложении → видит окно «Выберите тариф»:
       • 3 дня бесплатно (один раз на пользователя)
       • 1 месяц — $9.99
       • 3 месяца — $8.88/мес = $26.64
       • 6 месяцев — $7.77/мес = $46.62
  2. Нажимает кнопку → попадает в бота, бот пишет, какой тариф выбран.
       • Пробный период → бот сразу включает 3 дня (и сообщает админу).
       • Платный тариф → бот присылает реквизиты (карта, сумма, комментарий) и ждёт чек 10 минут.
  3. Нет чека за 10 минут → «запрос отменён» + кнопка «Повторить».
  4. Пришёл чек → пользователю «платёж обрабатывается», админу — чек, имя, номер,
     ТАРИФ и кнопки «Подтвердить» / «Отказать».
  5. Админ подтвердил → бот пишет в Firebase  premium/<telegram-id> =
       {until: мс, from: мс, plan: tr|m1|m3|m6}
     Приложение показывает обратный отсчёт (дни/часы/минуты) от момента покупки до конца
     подписки, а по окончании закрывает все функции замком.

  6. Номер уже зарегистрирован на другой Telegram → бот не даёт код, а присылает ID, имя, @username
     и ссылку на чат того аккаунта, на который номер был зарегистрирован.
  7. Остатки: приложение кладёт новые «мало / закончилось» в Firebase  alerts/<telegram-id>,
     бот забирает их и присылает пользователю сообщение (см. process_alerts).
  8. Telegram привязан к одному номеру: при первой регистрации бот запоминает chat id + номер
     (SQLite phones и Firebase phones/<номер>, tgphone/<uid>). Если с этого же Telegram пытаются
     зарегистрировать ДРУГОЙ номер — бот не даёт код, а пишет «Этот Telegram уже привязан к номеру
     +992 92 ***** 6»; приложение показывает ту же ошибку. Отвязать номер может админ: /unbind <id>.

Запуск:  pip install -r requirements.txt  →  заполнить .env  →  python bot.py
"""
from __future__ import annotations

import base64
import html
import json
import logging
import math
import os
import re
import secrets
import hashlib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

log = logging.getLogger("okmobile-bot")
TZ = timezone(timedelta(hours=5))  # Таджикистан, UTC+5
DAY_MS = 86_400_000

# ───────────────────────────── тарифы ─────────────────────────────
# id совпадает с id в приложении (index.html); 'tr' — бесплатный пробный период.
PLANS: dict[str, dict[str, Any]] = {
    "tr": {"name": "3 дня бесплатно", "days": 3, "usd": 0.0, "per": None},
    "m1": {"name": "1 месяц", "days": 30, "usd": 9.99, "per": None},
    "m3": {"name": "3 месяца", "days": 90, "usd": 26.64, "per": 8.88},
    "m6": {"name": "6 месяцев", "days": 180, "usd": 46.62, "per": 7.77},
}
PAID = ("m1", "m3", "m6")
FB_PLAN = {"tr": "trial", "m1": "m1", "m3": "m3", "m6": "m6"}  # что пишем в Firebase


def usd(x: float) -> str:
    return f"${x:.2f}"


def plan_line(pid: str) -> str:
    p = PLANS[pid]
    if pid == "tr":
        return "3 дня бесплатно"
    extra = f" ({usd(p['per'])}/мес)" if p["per"] else ""
    return f"{p['name']} — {usd(p['usd'])}{extra}"


# ───────────────────────────── настройки ─────────────────────────────
def load_env(path: str = ".env") -> None:
    """Мини-загрузчик .env (без внешних библиотек)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Cfg:
    def __init__(self, env=os.environ):
        self.token = env.get("BOT_TOKEN", "").strip()
        self.admins = [int(x) for x in re.split(r"[,\s]+", env.get("ADMIN_IDS", "")) if re.fullmatch(r"-?\d+", x or "")]
        self.db_url = env.get("FIREBASE_DB_URL", "").strip().rstrip("/")
        self.cred_path = env.get("GOOGLE_APPLICATION_CREDENTIALS", "serviceAccount.json").strip()
        # Ключ Firebase можно передать не файлом, а переменной (удобно для Railway): JSON целиком или его base64
        self.cred_json = env.get("FIREBASE_CREDENTIALS_JSON", "").strip()
        self.ns = env.get("APP_NS", "okm").strip()
        self.card = env.get("CARD_NUMBER", "4444 8888 1227 1025").strip()
        self.holder = env.get("CARD_HOLDER", "EHSON IDIEV").strip()
        # Комментарий к переводу; {plan} подставится названием тарифа
        self.note = env.get("PAY_NOTE", "Подписка премиума: {plan}").strip()
        # Курс сомони за $1 — если задан, рядом с суммой в $ показывается «≈ N сомони» (0 = не показывать)
        self.usd_tjs = float(env.get("USD_TJS", "0") or 0)
        self.wait_min = float(env.get("WAIT_MINUTES", "10"))
        self.app_url = env.get("APP_URL", "").strip()
        self.sqlite = env.get("SQLITE_PATH", "premium.sqlite3").strip()

    def check(self) -> None:
        if not self.token:
            raise SystemExit("Не задан BOT_TOKEN (токен платёжного бота из @BotFather).")
        if not self.admins:
            raise SystemExit("Не задан ADMIN_IDS (Telegram ID админа; узнать можно у @userinfobot).")


# ───────────────────────────── утилиты ─────────────────────────────
def fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d.%m.%Y")


def fmt_dm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_left(ms: int) -> str:
    ms = max(0, int(ms))
    d, h, m = ms // DAY_MS, ms % DAY_MS // 3_600_000, ms % 3_600_000 // 60_000
    return f"{d} д {h} ч {m} мин"


def fmt_phone(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")[-9:]
    if len(d) == 9:
        return f"+992 {d[:2]} {d[2:5]} {d[5:7]} {d[7:9]}"
    return f"+992 {d}" if d else "не указан"


def mask_phone(p: str | None) -> str:
    """+992 92 ***** 6 — две первые цифры и последняя."""
    d = re.sub(r"\D", "", p or "")[-9:]
    return f"+992 {d[:2]} ***** {d[-1]}" if len(d) == 9 else "+992 ***** *"


def parse_payload(payload: str) -> tuple[str, str, str]:
    """start-параметр из приложения: 'p' + base64url('<телефон>|<тариф>|<имя>') → (телефон, имя, тариф).
    Поддерживается и старый формат '<телефон>|<имя>' (тариф пустой)."""
    if not payload or payload[0] != "p":
        return "", "", ""
    raw = payload[1:]
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return "", "", ""
    parts = text.split("|", 2)
    phone = re.sub(r"\D", "", parts[0])[-9:]
    if len(parts) == 3:
        plan, name = parts[1].strip(), parts[2]
    else:
        plan, name = "", (parts[1] if len(parts) > 1 else "")
    return phone, name.strip()[:60], plan if plan in PLANS else ""


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Инлайн-клавиатура: (текст, callback_data | 'url:https://…')."""
    def btn(t: str, d: str) -> dict:
        return {"text": t, "url": d[4:]} if d.startswith("url:") else {"text": t, "callback_data": d}
    return {"inline_keyboard": [[btn(t, d) for t, d in row] for row in rows]}


# ───────────────────────────── Telegram API ─────────────────────────────
class Telegram:
    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self.url = f"{base}/bot{token}/"
        self.s = requests.Session()

    def call(self, method: str, _t: int = 30, **params: Any) -> Any:
        """Вызов метода. Ошибки не роняют бота: вернётся None."""
        for _ in range(3):
            try:
                r = self.s.post(self.url + method, json=params, timeout=_t)
                j = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("Telegram %s: %s", method, e)
                return None
            if j.get("ok"):
                return j["result"]
            if r.status_code == 429:
                time.sleep(min(int(j.get("parameters", {}).get("retry_after", 1)), 10))
                continue
            log.warning("Telegram %s → %s %s", method, r.status_code, j.get("description"))
            return None
        return None

    def get_updates(self, offset: Optional[int], timeout: int) -> list[dict]:
        body: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            body["offset"] = offset
        r = self.s.post(self.url + "getUpdates", timeout=timeout + 15, json=body)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"getUpdates: {j.get('description')}")
        return j["result"]


# ───────────────────────────── Firebase ─────────────────────────────
class Firebase:
    """Запись статуса подписки в Realtime Database (через service account)."""

    def __init__(self, db_url: str, cred_path: str, cred_json: str = "", ns: str = "okm"):
        self.ns = ns
        import firebase_admin
        from firebase_admin import credentials, db

        if cred_json:
            raw = cred_json if cred_json.lstrip().startswith("{") else base64.b64decode(cred_json).decode("utf-8")
            cred = credentials.Certificate(json.loads(raw))
        else:
            cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {"databaseURL": db_url})
        self._db = db

    def set_premium(self, uid: int, until_ms: int, plan: str = "m1", since_ms: Optional[int] = None) -> None:
        """premium/<uid> = {until, from, plan}. from — момент покупки (для отсчёта и полосы прогресса)."""
        now = int(time.time() * 1000)
        self._db.reference(f"{self.ns}/premium/{uid}").set(
            {"until": int(until_ms), "from": int(since_ms or now), "plan": plan, "updatedAt": now})

    def set_verify(self, token: str, data: dict) -> None:
        self._db.reference(f"{self.ns}/verify/{token}").set(data)

    def clear_premium(self, uid: int) -> None:
        self._db.reference(f"{self.ns}/premium/{uid}").delete()

    # Владелец номера телефона (переживает потерю SQLite)
    def phone_owner(self, phone: str) -> Optional[int]:
        v = self._db.reference(f"{self.ns}/phones/{phone}").get()
        return int(v["uid"]) if isinstance(v, dict) and str(v.get("uid", "")).lstrip("-").isdigit() else None

    def set_phone_owner(self, phone: str, uid: int) -> None:
        self._db.reference(f"{self.ns}/phones/{phone}").set({"uid": int(uid), "ts": int(time.time() * 1000)})

    # Номер, к которому привязан Telegram: tgphone/<uid> = {phone, ts}
    def uid_phone(self, uid: int) -> Optional[str]:
        v = self._db.reference(f"{self.ns}/tgphone/{uid}").get()
        return str(v["phone"]) if isinstance(v, dict) and v.get("phone") else None

    def set_uid_phone(self, uid: int, phone: str) -> None:
        self._db.reference(f"{self.ns}/tgphone/{uid}").set({"phone": phone, "ts": int(time.time() * 1000)})

    def unbind(self, uid: int) -> None:
        p = self.uid_phone(uid)
        if p:
            self._db.reference(f"{self.ns}/phones/{p}").delete()
        self._db.reference(f"{self.ns}/tgphone/{uid}").delete()

    # Очередь уведомлений об остатках: alerts/<uid>/<key> = {ts, items:[{n,q,s}]}
    def get_alerts(self) -> dict:
        v = self._db.reference(f"{self.ns}/alerts").get()
        return v if isinstance(v, dict) else {}

    def del_alerts(self, uid: str, key: Optional[str] = None) -> None:
        self._db.reference(f"{self.ns}/alerts/{uid}" + (f"/{key}" if key else "")).delete()

    def has_premium(self, uid: int) -> bool:
        return self._db.reference(f"{self.ns}/premium/{uid}").get() is not None

    # Метка «пробный период уже использован» — переживает перезапуск/потерю SQLite
    def has_trial(self, uid: int) -> bool:
        return self._db.reference(f"{self.ns}/trial/{uid}").get() is not None

    def set_trial(self, uid: int) -> None:
        self._db.reference(f"{self.ns}/trial/{uid}").set({"ts": int(time.time() * 1000)})


# ───────────────────────────── хранилище ─────────────────────────────
class Store:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, phone TEXT, username TEXT, tg_name TEXT);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,                -- awaiting | review | approved | rejected | expired
            created REAL NOT NULL, expires REAL NOT NULL,
            prompt_msg INTEGER, file_id TEXT, file_type TEXT, extras INTEGER DEFAULT 0,
            decided_by INTEGER, decided REAL, plan TEXT DEFAULT 'm1');
        CREATE TABLE IF NOT EXISTS admin_msgs(
            payment_id INTEGER, admin_id INTEGER, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS premium(
            user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL,      -- миллисекунды
            warned INTEGER DEFAULT 0, ended INTEGER DEFAULT 0,
            since INTEGER DEFAULT 0, plan TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS trials(user_id INTEGER PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS phones(phone TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
        """)
        # миграция старой базы (до тарифов)
        def cols(t: str) -> set[str]:
            return {r[1] for r in self.c.execute(f"PRAGMA table_info({t})")}
        if "plan" not in cols("payments"):
            self.c.execute("ALTER TABLE payments ADD COLUMN plan TEXT DEFAULT 'm1'")
        if "since" not in cols("premium"):
            self.c.execute("ALTER TABLE premium ADD COLUMN since INTEGER DEFAULT 0")
        if "plan" not in cols("premium"):
            self.c.execute("ALTER TABLE premium ADD COLUMN plan TEXT DEFAULT ''")
        self.c.commit()

    # users
    def upsert_user(self, uid: int, name: str, phone: str, username: str, tg_name: str) -> None:
        old = self.user(uid)
        self.c.execute(
            "INSERT INTO users(id,name,phone,username,tg_name) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=?, phone=?, username=?, tg_name=?",
            (uid, name or (old["name"] if old else ""), phone or (old["phone"] if old else ""), username, tg_name,
             name or (old["name"] if old else ""), phone or (old["phone"] if old else ""), username, tg_name))
        self.c.commit()

    def user(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    # владельцы номеров
    def phone_owner(self, phone: str) -> Optional[int]:
        r = self.c.execute("SELECT user_id FROM phones WHERE phone=?", (phone,)).fetchone()
        return int(r["user_id"]) if r else None

    def phone_of(self, uid: int) -> Optional[str]:
        """Номер, к которому уже привязан этот Telegram."""
        r = self.c.execute("SELECT phone FROM phones WHERE user_id=?", (uid,)).fetchone()
        return r["phone"] if r else None

    def claim_phone(self, phone: str, uid: int) -> None:
        self.c.execute("DELETE FROM phones WHERE user_id=?", (uid,))  # у одного Telegram — один номер
        self.c.execute("INSERT OR REPLACE INTO phones(phone,user_id) VALUES(?,?)", (phone, uid))
        self.c.commit()

    def unbind(self, uid: int) -> None:
        self.c.execute("DELETE FROM phones WHERE user_id=?", (uid,))
        self.c.commit()

    # trial
    def trial_used(self, uid: int) -> bool:
        return self.c.execute("SELECT 1 FROM trials WHERE user_id=?", (uid,)).fetchone() is not None

    def mark_trial(self, uid: int) -> None:
        self.c.execute("INSERT OR IGNORE INTO trials(user_id,ts) VALUES(?,?)", (uid, int(time.time() * 1000)))
        self.c.commit()

    # payments
    def payment(self, pid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()

    def active_request(self, uid: int, now: float) -> Optional[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='awaiting' AND expires>? ORDER BY id DESC LIMIT 1",
            (uid, now)).fetchone()

    def review_request(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='review' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()

    def last_plan(self, uid: int) -> str:
        r = self.c.execute("SELECT plan FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return (r["plan"] if r and r["plan"] in PAID else "") or ""

    def all_review(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM payments WHERE status='review' ORDER BY id").fetchall()

    def create_request(self, uid: int, chat_id: int, now: float, wait_s: float, plan: str) -> int:
        cur = self.c.execute(
            "INSERT INTO payments(user_id,chat_id,status,created,expires,plan) VALUES(?,?,?,?,?,?)",
            (uid, chat_id, "awaiting", now, now + wait_s, plan))
        self.c.commit()
        return int(cur.lastrowid)

    def set_plan(self, pid: int, plan: str) -> None:
        self.c.execute("UPDATE payments SET plan=? WHERE id=?", (plan, pid))
        self.c.commit()

    def set_prompt(self, pid: int, msg_id: int) -> None:
        self.c.execute("UPDATE payments SET prompt_msg=? WHERE id=?", (msg_id, pid))
        self.c.commit()

    def set_receipt(self, pid: int, file_id: str, file_type: str) -> None:
        self.c.execute("UPDATE payments SET status='review', file_id=?, file_type=? WHERE id=?", (file_id, file_type, pid))
        self.c.commit()

    def add_extra(self, pid: int) -> int:
        self.c.execute("UPDATE payments SET extras=extras+1 WHERE id=?", (pid,))
        self.c.commit()
        return int(self.payment(pid)["extras"])

    def set_status(self, pid: int, status: str, by: Optional[int] = None, now: Optional[float] = None) -> None:
        self.c.execute("UPDATE payments SET status=?, decided_by=?, decided=? WHERE id=?", (status, by, now, pid))
        self.c.commit()

    def expire_due(self, now: float) -> list[sqlite3.Row]:
        rows = self.c.execute("SELECT * FROM payments WHERE status='awaiting' AND expires<=?", (now,)).fetchall()
        for r in rows:
            self.c.execute("UPDATE payments SET status='expired' WHERE id=?", (r["id"],))
        self.c.commit()
        return rows

    def next_expiry(self) -> Optional[float]:
        r = self.c.execute("SELECT MIN(expires) m FROM payments WHERE status='awaiting'").fetchone()
        return r["m"] if r and r["m"] is not None else None

    def add_admin_msg(self, pid: int, admin_id: int, message_id: int) -> None:
        self.c.execute("INSERT INTO admin_msgs VALUES(?,?,?)", (pid, admin_id, message_id))
        self.c.commit()

    def admin_msgs(self, pid: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM admin_msgs WHERE payment_id=?", (pid,)).fetchall()

    # premium
    def premium(self, uid: int) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM premium WHERE user_id=?", (uid,)).fetchone()

    def set_premium(self, uid: int, until_ms: int, since_ms: int = 0, plan: str = "") -> None:
        self.c.execute(
            "INSERT INTO premium(user_id,until,warned,ended,since,plan) VALUES(?,?,0,0,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET until=?, warned=0, ended=0, since=?, plan=?",
            (uid, until_ms, since_ms, plan, until_ms, since_ms, plan))
        self.c.commit()

    def del_premium(self, uid: int) -> None:
        self.c.execute("DELETE FROM premium WHERE user_id=?", (uid,))
        self.c.commit()

    def expiring(self, now_ms: int) -> list[sqlite3.Row]:
        """Предупреждаем за 3 дня до конца подписки и за 1 день до конца пробного периода."""
        return self.c.execute(
            "SELECT * FROM premium WHERE warned=0 AND until>? AND "
            "((plan!='trial' AND until<=?) OR (plan='trial' AND until<=?))",
            (now_ms, now_ms + 3 * DAY_MS, now_ms + DAY_MS)).fetchall()

    def ended(self, now_ms: int) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM premium WHERE ended=0 AND until<=?", (now_ms,)).fetchall()

    def mark(self, uid: int, col: str) -> None:
        assert col in ("warned", "ended")
        self.c.execute(f"UPDATE premium SET {col}=1 WHERE user_id=?", (uid,))
        self.c.commit()


# ───────────────────────────── логика бота ─────────────────────────────
class PremiumBot:
    def __init__(self, cfg: Cfg, tg: Telegram, store: Store, fb, clock: Callable[[], float] = time.time):
        self.cfg, self.tg, self.store, self.fb, self.now = cfg, tg, store, fb, clock
        self._last_notice = 0.0
        self._last_alerts = 0.0
        self._rate = 0.0
        self._rate_ts = 0.0

    # --- отправка ---
    def send(self, chat_id: int, text: str, markup: Optional[dict] = None) -> Optional[dict]:
        p: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.tg.call("sendMessage", **p)

    def answer(self, cq_id: str, text: str = "", alert: bool = False) -> None:
        self.tg.call("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)

    def open_app_kb(self) -> Optional[dict]:
        return kb([[("📱 Открыть приложение", "url:" + self.cfg.app_url)]]) if self.cfg.app_url.startswith("http") else None

    # --- тексты ---
    def rate(self) -> float:
        """Курс сомонӣ за $1: из USD_TJS или автоматически (обновляется раз в 6 часов)."""
        if self.cfg.usd_tjs > 0:
            return self.cfg.usd_tjs
        if self._rate and time.time() - self._rate_ts < 6 * 3600:
            return self._rate
        for url in ("https://open.er-api.com/v6/latest/USD", "https://api.exchangerate-api.com/v4/latest/USD"):
            try:
                v = float(requests.get(url, timeout=8).json()["rates"]["TJS"])
                if v > 0:
                    self._rate, self._rate_ts = v, time.time()
                    return v
            except Exception:
                continue
        return self._rate  # старое значение или 0 — тогда сомонӣ не показываем

    def rate_line(self) -> str:
        r = self.rate()
        return f"\nКурби имрӯза: 1 $ = {r:.2f} сомонӣ" if r else ""

    def money(self, pid: str) -> str:
        """Сумма тарифа: «$26.64» и, если задан USD_TJS, «≈ 280 сомони»."""
        p = PLANS[pid]
        s = usd(p["usd"])
        r = self.rate()
        if r > 0 and p["usd"] > 0:
            s += f" (≈ {round(p['usd'] * r)} сомонӣ)"
        return s

    def requisites_text(self, pay, premium_until_ms: Optional[int]) -> str:
        c = self.cfg
        pid = pay["plan"] if pay["plan"] in PAID else "m1"
        p = PLANS[pid]
        left = max(1, math.ceil((pay["expires"] - self.now()) / 60))
        extra = ""
        if premium_until_ms and premium_until_ms > self.now() * 1000:
            extra = (f"У вас уже есть Премиум до <b>{fmt_dm(premium_until_ms)}</b>. "
                     f"Оплата продлит подписку ещё на {p['name']}.\n\n")
        per = f"\nЦена за месяц: <b>{usd(p['per'])}</b>" if p["per"] else ""
        note = c.note.replace("{plan}", p["name"])
        return (
            f"💎 <b>Вы выбрали тариф: {p['name']}</b>\n"
            f"К оплате: <b>{self.money(pid)}</b>{per}{self.rate_line()}\n\n{extra}"
            f"Оплатите на карту:\n<code>{html.escape(c.card)}</code>\n"
            f"Получатель: <b>{html.escape(c.holder)}</b>\n"
            f"Сумма: <b>{self.money(pid)}</b>\n"
            f"Комментарий к переводу: <code>{html.escape(note)}</code>\n\n"
            f"После оплаты отправьте сюда чек — фото или скриншот.\n"
            f"⏳ Время ожидания — <b>{left} мин.</b>")

    def admin_caption(self, pay, suffix: str = "") -> str:
        u = self.store.user(pay["user_id"])
        name = html.escape((u["name"] if u and u["name"] else "") or (u["tg_name"] if u else "") or "—")
        uname = f"@{html.escape(u['username'])}" if u and u["username"] else "без username"
        pid = pay["plan"] if pay["plan"] in PAID else "m1"
        return (
            f"🧾 <b>Чек на подписку Премиум</b> · заявка #{pay['id']}\n"
            f"👤 Имя: <b>{name}</b>\n"
            f"📞 Номер: <b>{fmt_phone(u['phone'] if u else '')}</b>\n"
            f"✈️ Telegram: {uname} · ID <code>{pay['user_id']}</code> · "
            f"<a href=\"tg://user?id={pay['user_id']}\">открыть чат</a>\n"
            f"🏷 Тариф: <b>{PLANS[pid]['name']}</b>\n"
            f"💰 Сумма: <b>{self.money(pid)}</b>\n"
            f"🕒 {fmt_dt(pay['created'])}{suffix}")

    def retry_kb(self) -> dict:
        return kb([[("🔄 Повторить", "retry")]])

    # --- входящие ---
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        m = u.get("message")
        if m and m.get("chat", {}).get("type") == "private" and m.get("from"):
            self.on_message(m)

    def on_message(self, m: dict) -> None:
        frm, chat_id = m["from"], m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            if cmd == "/start":
                return self.cmd_start(m, arg)
            if cmd == "/status":
                return self.cmd_status(frm["id"], chat_id)
            if cmd == "/tariffs":
                return self.plans_menu(frm["id"], chat_id)
            if cmd == "/help":
                return self.send(chat_id, "Нажмите /start, чтобы выбрать тариф, или /status, чтобы узнать срок подписки.")
            if frm["id"] in self.cfg.admins:
                if cmd == "/grant":
                    return self.cmd_grant(chat_id, arg)
                if cmd == "/revoke":
                    return self.cmd_revoke(chat_id, arg)
                if cmd == "/pending":
                    return self.cmd_pending(chat_id)
                if cmd == "/unbind":
                    return self.cmd_unbind(chat_id, arg)
            return
        if m.get("photo") or (m.get("document") or {}).get("mime_type", "").startswith(("image/", "application/pdf")):
            return self.on_receipt(m)
        if self.store.active_request(frm["id"], self.now()):
            self.send(chat_id, "Отправьте чек об оплате <b>фото или скриншотом</b>.")
        else:
            self.send(chat_id, "Чтобы выбрать тариф, нажмите /start.")

    def remember_user(self, frm: dict, phone: str = "", name: str = "") -> None:
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.store.upsert_user(frm["id"], name, phone, frm.get("username") or "", full)

    def owner_card(self, owner: int) -> str:
        """Кто владелец номера: имя, @username, ID и ссылка на чат."""
        u = self.store.user(owner)
        name, uname = (u["tg_name"] or u["name"] or "") if u else "", (u["username"] or "") if u else ""
        if not (name or uname):  # в базе нет — спросим у Telegram
            ch = self.tg.call("getChat", chat_id=owner) or {}
            name = " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x)
            uname = ch.get("username") or ""
        return (f"👤 Имя: <b>{html.escape(name) or '—'}</b>\n"
                f"✈️ Username: {'@' + html.escape(uname) if uname else 'нет'}\n"
                f"🆔 Telegram ID: <code>{owner}</code>\n"
                f"💬 <a href=\"tg://user?id={owner}\">Открыть чат</a>")

    def phone_conflict(self, phone: str, uid: int) -> Optional[int]:
        """Если номер уже закреплён за другим Telegram — вернёт его ID."""
        owner = self.store.phone_owner(phone)
        if owner is None:
            try:
                owner = self.fb.phone_owner(phone)
            except Exception:
                log.exception("Firebase phone_owner")
        return owner if owner is not None and owner != uid else None

    def bound_phone(self, uid: int) -> Optional[str]:
        """Номер, к которому этот Telegram уже привязан (SQLite → Firebase)."""
        p = self.store.phone_of(uid)
        if not p:
            try:
                p = self.fb.uid_phone(uid)
            except Exception:
                log.exception("Firebase uid_phone")
        return p

    def login_code(self, m: dict, arg: str) -> None:
        """Вход в приложение: бот шлёт 6-значный код, а в Firebase кладёт только его хэш.
        arg = '<токен>' или '<токен>_<телефон 9 цифр>' (из приложения)."""
        frm, chat_id = m["from"], m["chat"]["id"]
        parts = arg.split("_")  # <токен>[_<телефон>[_r]]  (r = сброс пароля)
        token, ph = parts[0], (parts[1] if len(parts) > 1 else "")
        reset = "r" in parts[2:]
        phone = ph if re.fullmatch(r"\d{9}", ph) else ""
        if not re.fullmatch(r"[0-9a-f]{16,64}", token):
            return self.send(chat_id, "Ссылка входа недействительна. Откройте приложение и повторите.")
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        exp = int((self.now() + 600) * 1000)
        if reset and phone:  # сброс пароля — только для уже зарегистрированного номера
            known = self.store.phone_owner(phone)
            if known is None:
                try:
                    known = self.fb.phone_owner(phone)
                except Exception:
                    log.exception("Firebase phone_owner")
            if known is None:
                try:
                    self.fb.set_verify(token, {"nf": True, "exp": exp})
                except Exception:
                    log.exception("Firebase verify nf")
                return self.send(chat_id, f"⚠️ Аккаунт с номером {fmt_phone(phone)} не найден. Сначала зарегистрируйтесь в приложении.")
        if phone:
            # 1) этот Telegram уже привязан к ДРУГОМУ номеру → ошибка с маской номера
            bound = self.bound_phone(frm["id"])
            if bound and bound != phone:
                try:  # приложение увидит «Telegram уже привязан к номеру …»
                    self.fb.set_verify(token, {"tgb": True, "m": mask_phone(bound), "exp": exp})
                except Exception:
                    log.exception("Firebase verify tgb")
                return self.send(
                    chat_id,
                    f"⚠️ <b>Этот Telegram уже привязан к номеру {mask_phone(bound)}.</b>\n"
                    f"Войдите в приложение с этим номером. Если номер нужно сменить — обратитесь к администратору.")
            # 2) этот номер уже закреплён за другим Telegram
            owner = self.phone_conflict(phone, frm["id"])
            if owner is not None:
                try:  # приложение увидит «номер занят» вместо ожидания кода
                    self.fb.set_verify(token, {"dup": True, "exp": exp})
                except Exception:
                    log.exception("Firebase verify dup")
                return self.send(
                    chat_id,
                    f"⚠️ <b>Аккаунт с номером {fmt_phone(phone)} уже существует.</b>\n"
                    f"Номер зарегистрирован на другой Telegram-аккаунт:\n\n{self.owner_card(owner)}\n\n"
                    f"Войдите с того аккаунта или укажите в приложении другой номер.")
        code = str(secrets.randbelow(900000) + 100000)
        try:
            self.fb.set_verify(token, {"uid": frm["id"], "name": name, "h": hashlib.sha256((token + code).encode()).hexdigest(),
                                       "exp": exp})
        except Exception:
            log.exception("Firebase verify")
            return self.send(chat_id, "⚠️ Сервер временно недоступен. Попробуйте ещё раз.")
        self.remember_user(frm, phone)
        if phone:  # chat id и номер закрепляются друг за другом
            self.store.claim_phone(phone, frm["id"])
            try:
                self.fb.set_phone_owner(phone, frm["id"])
                self.fb.set_uid_phone(frm["id"], phone)
            except Exception:
                log.exception("Firebase set_phone_owner")
        title = "🔑 Код для сброса пароля OK-Mobile" if reset else "🔐 Код входа в OK-Mobile"
        self.send(chat_id, f"{title}: <code>{code}</code>\nДействует 10 минут. Никому его не сообщайте.")

    def cmd_start(self, m: dict, payload: str) -> None:
        if payload.startswith("v"):
            return self.login_code(m, payload[1:])
        phone, name, plan = parse_payload(payload)
        self.remember_user(m["from"], phone, name)
        uid, chat_id = m["from"]["id"], m["chat"]["id"]
        if plan == "tr":
            return self.start_trial(m["from"], chat_id)
        if plan in PAID:
            return self.offer(uid, chat_id, plan)
        self.plans_menu(uid, chat_id)

    def cmd_status(self, uid: int, chat_id: int) -> None:
        p = self.store.premium(uid)
        now_ms = int(self.now() * 1000)
        if p and p["until"] > now_ms:
            name = PLANS.get({"trial": "tr"}.get(p["plan"], p["plan"]), {}).get("name", "Премиум")
            self.send(chat_id,
                      f"✅ Премиум активен ({name}) до <b>{fmt_dm(p['until'])}</b>.\n"
                      f"Осталось: <b>{fmt_left(p['until'] - now_ms)}</b>.",
                      kb([[("➕ Продлить", "menu")]]))
        else:
            self.send(chat_id, "Премиум не активен.", kb([[("💎 Выбрать тариф", "menu")]]))

    # --- выбор тарифа ---
    def trial_available(self, uid: int) -> bool:
        return not self.store.trial_used(uid) and not self.store.premium(uid)

    def plans_menu(self, uid: int, chat_id: int) -> None:
        rows: list[list[tuple[str, str]]] = []
        lines = ["💎 <b>Выберите тариф Премиум</b>\n"]
        if self.trial_available(uid):
            lines.append("🎁 3 дня бесплатно — один раз, без оплаты")
            rows.append([("🎁 3 дня бесплатно", "pl:tr")])
        for pid in PAID:
            p = PLANS[pid]
            lines.append(f"• {plan_line(pid)}")
            rows.append([(f"{p['name']} — {usd(p['usd'])}", f"pl:{pid}")])
        self.send(chat_id, "\n".join(lines), kb(rows))

    def start_trial(self, frm: dict, chat_id: int) -> None:
        uid, now = frm["id"], self.now()
        now_ms = int(now * 1000)
        cur = self.store.premium(uid)
        if cur and cur["until"] > now_ms:
            return self.send(chat_id, f"У вас уже активен Премиум до <b>{fmt_dm(cur['until'])}</b>.", self.open_app_kb())
        try:
            used = self.store.trial_used(uid) or self.fb.has_trial(uid) or bool(cur)
        except Exception:
            log.exception("Firebase trial check")
            return self.send(chat_id, "⚠️ Сервер временно недоступен. Попробуйте ещё раз.")
        if used:
            self.send(chat_id, "Пробный период уже использован. Выберите тариф:")
            return self.plans_menu(uid, chat_id)
        until = now_ms + PLANS["tr"]["days"] * DAY_MS
        try:
            self.fb.set_trial(uid)
            self.fb.set_premium(uid, until, FB_PLAN["tr"], now_ms)
        except Exception:
            log.exception("Firebase: не удалось включить пробный период %s", uid)
            return self.send(chat_id, "⚠️ Не удалось включить пробный период. Попробуйте ещё раз чуть позже.")
        self.store.mark_trial(uid)
        self.store.set_premium(uid, until, now_ms, "trial")
        self.send(chat_id,
                  f"🎁 <b>Вы выбрали: 3 дня бесплатно</b>\nПробный период включён до <b>{fmt_dm(until)}</b>.\n"
                  f"Откройте приложение — все функции доступны. Когда срок закончится, выберите тариф командой /tariffs.",
                  self.open_app_kb())
        u = self.store.user(uid)
        who = html.escape((u["name"] if u and u["name"] else "") or (u["tg_name"] if u else "") or str(uid))
        for admin in self.cfg.admins:
            self.send(admin, f"🎁 Пробный период (3 дня): <b>{who}</b> · {fmt_phone(u['phone'] if u else '')} · "
                             f"ID <code>{uid}</code>")

    def offer(self, uid: int, chat_id: int, plan: str = "") -> None:
        """Показать реквизиты выбранного тарифа (создать заявку на оплату, если активной ещё нет)."""
        plan = plan if plan in PAID else (self.store.last_plan(uid) or "m1")
        if self.store.review_request(uid):
            return self._processing(chat_id)
        now = self.now()
        pay = self.store.active_request(uid, now)
        if not pay:
            pid = self.store.create_request(uid, chat_id, now, self.cfg.wait_min * 60, plan)
            pay = self.store.payment(pid)
        elif pay["plan"] != plan:  # передумал — меняем тариф в активной заявке
            self.store.set_plan(pay["id"], plan)
            pay = self.store.payment(pay["id"])
        prem = self.store.premium(uid)
        sent = self.send(chat_id, self.requisites_text(pay, prem["until"] if prem else None))
        if sent:
            self.store.set_prompt(pay["id"], sent["message_id"])

    def _processing(self, chat_id: int) -> None:
        self.send(chat_id, "⏳ Ваш платёж обрабатывается, пожалуйста, подождите.")

    # --- чек ---
    @staticmethod
    def _file_of(m: dict) -> tuple[str, str]:
        if m.get("photo"):
            return m["photo"][-1]["file_id"], "photo"
        return m["document"]["file_id"], "document"

    def _to_admins(self, pay, file_id: str, ftype: str, caption: str, markup: Optional[dict]) -> list[tuple[int, int]]:
        out = []
        for admin in self.cfg.admins:
            method, key = ("sendPhoto", "photo") if ftype == "photo" else ("sendDocument", "document")
            params: dict[str, Any] = {"chat_id": admin, key: file_id, "caption": caption, "parse_mode": "HTML"}
            if markup:
                params["reply_markup"] = markup
            r = self.tg.call(method, **params)
            if r:
                out.append((admin, r["message_id"]))
            else:
                log.error("Не удалось отправить чек админу %s (он должен нажать /start у этого бота)", admin)
        return out

    def _review_kb(self, pid: int) -> dict:
        return kb([[("✅ Подтвердить", f"ok:{pid}"), ("❌ Отказать", f"no:{pid}")]])

    def on_receipt(self, m: dict) -> None:
        uid, chat_id = m["from"]["id"], m["chat"]["id"]
        self.remember_user(m["from"])
        file_id, ftype = self._file_of(m)
        pay = self.store.active_request(uid, self.now())
        if pay:
            sent = self._to_admins(pay, file_id, ftype, self.admin_caption(pay), self._review_kb(pay["id"]))
            if not sent:
                self.send(chat_id, "⚠️ Не удалось передать чек администратору. Отправьте его ещё раз чуть позже.")
                return
            self.store.set_receipt(pay["id"], file_id, ftype)
            for admin, mid in sent:
                self.store.add_admin_msg(pay["id"], admin, mid)
            return self._processing(chat_id)
        rev = self.store.review_request(uid)
        if rev:  # дополнительный чек к уже поданной заявке
            if self.store.add_extra(rev["id"]) <= 3:
                self._to_admins(rev, file_id, ftype, f"📎 Дополнительный чек к заявке #{rev['id']}", None)
            return self._processing(chat_id)
        self.send(chat_id, "Запрос на подписку не найден или уже отменён. Если вы уже оплатили — нажмите «Повторить» "
                           "и отправьте чек ещё раз.", self.retry_kb())

    # --- кнопки ---
    def on_callback(self, cq: dict) -> None:
        data, frm = cq.get("data") or "", cq["from"]
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id", frm["id"])
        if data == "retry":  # повторить прошлый тариф (или показать список)
            self.answer(cq["id"])
            self.remember_user(frm)
            plan = self.store.last_plan(frm["id"])
            return self.offer(frm["id"], chat_id, plan) if plan else self.plans_menu(frm["id"], chat_id)
        if data == "menu":
            self.answer(cq["id"])
            self.remember_user(frm)
            return self.plans_menu(frm["id"], chat_id)
        pm = re.fullmatch(r"pl:(tr|m1|m3|m6)", data)
        if pm:
            self.answer(cq["id"])
            self.remember_user(frm)
            if pm.group(1) == "tr":
                return self.start_trial(frm, chat_id)
            return self.offer(frm["id"], chat_id, pm.group(1))
        m = re.fullmatch(r"(ok|no):(\d+)", data)
        if not m:
            return self.answer(cq["id"])
        if frm["id"] not in self.cfg.admins:
            return self.answer(cq["id"], "Нет доступа", True)
        self.decide(int(m.group(2)), m.group(1) == "ok", frm, cq["id"])

    def decide(self, pid: int, approve: bool, admin: dict, cq_id: str) -> None:
        pay = self.store.payment(pid)
        if not pay:
            return self.answer(cq_id, "Заявка не найдена", True)
        if pay["status"] != "review":
            labels = {"approved": "уже подтверждена", "rejected": "уже отклонена", "awaiting": "чек ещё не получен", "expired": "запрос отменён"}
            return self.answer(cq_id, f"Заявка #{pid}: {labels.get(pay['status'], pay['status'])}", True)
        uid, now = pay["user_id"], self.now()
        plan = pay["plan"] if pay["plan"] in PAID else "m1"
        p = PLANS[plan]
        who = html.escape(admin.get("first_name") or admin.get("username") or str(admin["id"]))
        if approve:
            now_ms = int(now * 1000)
            cur = self.store.premium(uid)
            active = bool(cur and cur["until"] > now_ms)
            since = (cur["since"] or now_ms) if active else now_ms  # начало отсчёта: первая покупка в текущем периоде
            until = max(now_ms, cur["until"] if cur else 0) + p["days"] * DAY_MS
            try:
                self.fb.set_premium(uid, until, FB_PLAN[plan], since)
            except Exception:
                log.exception("Firebase: не удалось записать подписку %s", uid)
                return self.answer(cq_id, "❌ Не удалось записать подписку в Firebase. Проверьте настройки и нажмите ещё раз.", True)
            self.store.set_premium(uid, until, since, plan)
            self.store.set_status(pid, "approved", admin["id"], now)
            self.answer(cq_id, "Подтверждено")
            self.send(pay["chat_id"],
                      f"🎉 <b>Подписка Премиум оформлена!</b>\n"
                      f"Тариф: <b>{p['name']}</b>\n"
                      f"Действует до <b>{fmt_dm(until)}</b> · осталось <b>{fmt_left(until - now_ms)}</b>.\n"
                      f"Ваш профиль в приложении разблокирован — откройте OK-Mobile.", self.open_app_kb())
            self._mark_admin_cards(pid, f"\n\n✅ <b>Подтверждено</b> · {who} · {fmt_dt(now)}")
        else:
            self.store.set_status(pid, "rejected", admin["id"], now)
            self.answer(cq_id, "Отклонено")
            self.send(pay["chat_id"],
                      "❌ <b>Платёж не подтверждён.</b>\nПроверьте сумму и карту, затем нажмите «Повторить» и отправьте чек ещё раз.",
                      self.retry_kb())
            self._mark_admin_cards(pid, f"\n\n❌ <b>Отказано</b> · {who} · {fmt_dt(now)}")

    def _mark_admin_cards(self, pid: int, suffix: str) -> None:
        pay = self.store.payment(pid)
        caption = self.admin_caption(pay, suffix)
        for r in self.store.admin_msgs(pid):
            self.tg.call("editMessageCaption", chat_id=r["admin_id"], message_id=r["message_id"], caption=caption,
                         parse_mode="HTML", reply_markup={"inline_keyboard": []})

    # --- команды админа ---
    def cmd_grant(self, chat_id: int, arg: str) -> None:
        parts = arg.split()
        if not parts or not parts[0].isdigit():
            return self.send(chat_id, "Формат: /grant <telegram_id> [дней]  (по умолчанию 30)")
        uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
        now_ms = int(self.now() * 1000)
        cur = self.store.premium(uid)
        active = bool(cur and cur["until"] > now_ms)
        since = (cur["since"] or now_ms) if active else now_ms
        until = max(now_ms, cur["until"] if cur else 0) + days * DAY_MS
        try:
            self.fb.set_premium(uid, until, "gift", since)
        except Exception:
            log.exception("Firebase grant")
            return self.send(chat_id, "❌ Не удалось записать в Firebase, см. журнал бота.")
        self.store.set_premium(uid, until, since, "gift")
        self.send(uid, f"🎁 Вам активирован Премиум до <b>{fmt_dm(until)}</b>. Откройте приложение OK-Mobile.")
        self.send(chat_id, f"✅ Премиум для <code>{uid}</code> до {fmt_dm(until)}.")

    def cmd_revoke(self, chat_id: int, arg: str) -> None:
        if not arg.strip().isdigit():
            return self.send(chat_id, "Формат: /revoke <telegram_id>")
        uid = int(arg.strip())
        try:
            self.fb.clear_premium(uid)
        except Exception:
            log.exception("Firebase revoke")
            return self.send(chat_id, "❌ Не удалось удалить запись в Firebase, см. журнал бота.")
        self.store.del_premium(uid)
        self.send(chat_id, f"Премиум для <code>{uid}</code> отключён.")

    def cmd_unbind(self, chat_id: int, arg: str) -> None:
        """Отвязать номер от Telegram (например, человек ошибся номером при регистрации)."""
        if not arg.strip().isdigit():
            return self.send(chat_id, "Формат: /unbind <telegram_id>")
        uid = int(arg.strip())
        old = self.bound_phone(uid)
        try:
            self.fb.unbind(uid)
        except Exception:
            log.exception("Firebase unbind")
            return self.send(chat_id, "❌ Не удалось отвязать в Firebase, см. журнал бота.")
        self.store.unbind(uid)
        self.send(chat_id, f"Номер {fmt_phone(old) if old else ''} отвязан от <code>{uid}</code>. "
                           f"Теперь с этого Telegram можно зарегистрировать другой номер.")

    def cmd_pending(self, chat_id: int) -> None:
        rows = self.store.all_review()
        if not rows:
            return self.send(chat_id, "Заявок на проверке нет.")
        for pay in rows:
            r = self._to_admins_one(chat_id, pay)
            if not r:
                self.send(chat_id, f"Заявка #{pay['id']} (чек не удалось показать)")

    def _to_admins_one(self, admin_id: int, pay) -> bool:
        method, key = ("sendPhoto", "photo") if pay["file_type"] == "photo" else ("sendDocument", "document")
        r = self.tg.call(method, chat_id=admin_id, **{key: pay["file_id"]}, caption=self.admin_caption(pay),
                         parse_mode="HTML", reply_markup=self._review_kb(pay["id"]))
        if r:
            self.store.add_admin_msg(pay["id"], admin_id, r["message_id"])
        return bool(r)

    # --- таймеры ---
    def tick(self) -> None:
        now = self.now()
        for pay in self.store.expire_due(now):
            if pay["prompt_msg"]:
                self.tg.call("editMessageText", chat_id=pay["chat_id"], message_id=pay["prompt_msg"],
                             text="⌛ Запрос на оплату отменён.")
            self.send(pay["chat_id"],
                      "⌛ Время ожидания истекло. Ваш запрос на подписку премиума отменён.\n"
                      "Если вы уже оплатили — нажмите «Повторить» и отправьте чек.", self.retry_kb())
        if now - self._last_notice >= 60:
            self._last_notice = now
            self.subscription_notices()
        self.process_alerts()

    def subscription_notices(self) -> None:
        now_ms = int(self.now() * 1000)
        for r in self.store.expiring(now_ms):
            trial = r["plan"] == "trial"
            what = "Пробный период" if trial else "Ваш Премиум"
            self.send(r["user_id"], f"⏰ {what} заканчивается <b>{fmt_dm(r['until'])}</b> "
                                    f"(осталось {fmt_left(r['until'] - now_ms)}). "
                                    f"Выберите тариф заранее — новый срок добавится к текущему.",
                      kb([[("➕ Выбрать тариф", "menu")]]))
            self.store.mark(r["user_id"], "warned")
        for r in self.store.ended(now_ms):
            trial = r["plan"] == "trial"
            self.send(r["user_id"],
                      ("Пробный период закончился." if trial else "Срок вашего Премиума закончился.")
                      + " Функции приложения закрыты замком — выберите тариф, чтобы открыть их снова.",
                      kb([[("💎 Выбрать тариф", "menu")]]))
            # Запись в Firebase остаётся: приложение само закрывается по времени (until) и показывает дату окончания.
            self.store.mark(r["user_id"], "ended")

    # --- уведомления об остатках ---
    def process_alerts(self) -> None:
        now = self.now()
        if now - self._last_alerts < 8:
            return
        self._last_alerts = now
        try:
            queue = self.fb.get_alerts()
        except Exception:
            log.exception("Firebase alerts")
            return
        for uid_s, entries in list(queue.items())[:50]:
            if not str(uid_s).isdigit() or not isinstance(entries, dict):
                continue
            uid = int(uid_s)
            known = bool(self.store.user(uid))
            if not known:
                try:
                    known = self.fb.has_premium(uid)  # не шлём незнакомым ID
                except Exception:
                    known = False
            items: list[dict] = []
            if known:
                for key in sorted(entries):
                    e = entries[key]
                    for it in (e.get("items") if isinstance(e, dict) else None) or []:
                        if isinstance(it, dict) and it.get("s") in ("l", "z"):
                            items.append(it)
            ok = True
            for chunk in range(0, len(items), 30):
                ok = bool(self.send(uid, self.stock_text(items[chunk:chunk + 30]))) and ok
            if ok or not known:  # при ошибке отправки оставим в очереди до следующего цикла
                try:
                    self.fb.del_alerts(uid_s)
                except Exception:
                    log.exception("Firebase del_alerts")

    @staticmethod
    def stock_text(items: list[dict]) -> str:
        def line(it: dict) -> str:
            q = it.get("q")
            q = int(q) if isinstance(q, (int, float)) and float(q).is_integer() else q
            return f"• {html.escape(str(it.get('n', '—'))[:80])} — {html.escape(str(q))} шт."
        zero = [it for it in items if it["s"] == "z"]
        low = [it for it in items if it["s"] == "l"]
        out = ["📦 <b>Остатки на складе</b>"]
        if zero:
            out += ["", "🔴 <b>Закончилось:</b>"] + [line(i) for i in zero]
        if low:
            out += ["", "🟠 <b>Мало осталось:</b>"] + [line(i) for i in low]
        out += ["", "Пополните склад в приложении OK-SKLAD."]
        return "\n".join(out)

    def poll_timeout(self) -> int:
        nxt = self.store.next_expiry()
        return 8 if nxt is None else max(1, min(8, math.ceil(nxt - self.now())))

    # --- главный цикл ---
    def run(self) -> None:
        me = self.tg.call("getMe")
        log.info("Бот запущен: @%s · админы: %s", (me or {}).get("username", "?"), self.cfg.admins)
        offset: Optional[int] = None
        while True:
            try:
                self.tick()
                for u in self.tg.get_updates(offset, self.poll_timeout()):
                    offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:
                        log.exception("Ошибка при обработке обновления %s", u.get("update_id"))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("Цикл опроса: %s", e)
                time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    cfg = Cfg()
    cfg.check()
    if not cfg.cred_json and not os.path.exists(cfg.cred_path):
        raise SystemExit(f"Нет ключа Firebase: задайте FIREBASE_CREDENTIALS_JSON или положите файл {cfg.cred_path} (см. README).")
    bot = PremiumBot(cfg, Telegram(cfg.token), Store(cfg.sqlite), Firebase(cfg.db_url, cfg.cred_path, cfg.cred_json, cfg.ns))
    try:
        bot.run()
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)


if __name__ == "__main__":
    main()
