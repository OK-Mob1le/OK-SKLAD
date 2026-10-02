#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OK-Mobile · бот входа и подписки

Как это работает
  1. В приложении пользователь жмёт «Оформить подписку» и попадает в этого бота.
  2. Бот присылает реквизиты (карта, сумма, комментарий) и ждёт чек 10 минут.
  3. Нет чека за 10 минут → «запрос отменён» + кнопка «Повторить».
  4. Пришёл чек → пользователю «платёж обрабатывается», админу — чек, имя, номер
     и кнопки «Подтвердить» / «Отказать».
  5. Админ подтвердил → бот пишет в Firebase  premium/<telegram-id> = {until: мс},
     пользователю приходит сообщение об успешной подписке, а приложение
     разблокируется само (оно читает эту запись).

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
        self.price = int(env.get("PRICE_TJS", "20"))
        self.card = env.get("CARD_NUMBER", "4444 8888 1227 1025").strip()
        self.holder = env.get("CARD_HOLDER", "EHSON IDIEV").strip()
        self.note = env.get("PAY_NOTE", "Подписка премиума на 1 месяц").strip()
        self.wait_min = float(env.get("WAIT_MINUTES", "10"))
        self.days = int(env.get("PREMIUM_DAYS", "30"))
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


def fmt_phone(p: str | None) -> str:
    d = re.sub(r"\D", "", p or "")[-9:]
    if len(d) == 9:
        return f"+992 {d[:2]} {d[2:5]} {d[5:7]} {d[7:9]}"
    return f"+992 {d}" if d else "не указан"


def parse_payload(payload: str) -> tuple[str, str]:
    """start-параметр из приложения: 'p' + base64url('<телефон>|<имя>') → (телефон, имя)."""
    if not payload or payload[0] != "p":
        return "", ""
    raw = payload[1:]
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return "", ""
    phone, _, name = text.partition("|")
    return re.sub(r"\D", "", phone)[-9:], name.strip()[:60]


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

    def set_premium(self, uid: int, until_ms: int) -> None:
        self._db.reference(f"{self.ns}/premium/{uid}").set(
            {"until": int(until_ms), "plan": "month", "updatedAt": int(time.time() * 1000)})

    def set_verify(self, token: str, data: dict) -> None:
        self._db.reference(f"{self.ns}/verify/{token}").set(data)

    def clear_premium(self, uid: int) -> None:
        self._db.reference(f"{self.ns}/premium/{uid}").delete()


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
            decided_by INTEGER, decided REAL);
        CREATE TABLE IF NOT EXISTS admin_msgs(
            payment_id INTEGER, admin_id INTEGER, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS premium(
            user_id INTEGER PRIMARY KEY, until INTEGER NOT NULL,      -- миллисекунды
            warned INTEGER DEFAULT 0, ended INTEGER DEFAULT 0);
        """)
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

    def all_review(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM payments WHERE status='review' ORDER BY id").fetchall()

    def create_request(self, uid: int, chat_id: int, now: float, wait_s: float) -> int:
        cur = self.c.execute(
            "INSERT INTO payments(user_id,chat_id,status,created,expires) VALUES(?,?,?,?,?)",
            (uid, chat_id, "awaiting", now, now + wait_s))
        self.c.commit()
        return int(cur.lastrowid)

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

    def set_premium(self, uid: int, until_ms: int) -> None:
        self.c.execute(
            "INSERT INTO premium(user_id,until,warned,ended) VALUES(?,?,0,0) "
            "ON CONFLICT(user_id) DO UPDATE SET until=?, warned=0, ended=0", (uid, until_ms, until_ms))
        self.c.commit()

    def del_premium(self, uid: int) -> None:
        self.c.execute("DELETE FROM premium WHERE user_id=?", (uid,))
        self.c.commit()

    def expiring(self, now_ms: int, within_ms: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM premium WHERE warned=0 AND until>? AND until<=?", (now_ms, now_ms + within_ms)).fetchall()

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

    # --- отправка ---
    def send(self, chat_id: int, text: str, markup: Optional[dict] = None) -> Optional[dict]:
        p: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.tg.call("sendMessage", **p)

    def answer(self, cq_id: str, text: str = "", alert: bool = False) -> None:
        self.tg.call("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)

    # --- тексты ---
    def requisites_text(self, pay, premium_until_ms: Optional[int]) -> str:
        c = self.cfg
        left = max(1, math.ceil((pay["expires"] - self.now()) / 60))
        extra = ""
        if premium_until_ms and premium_until_ms > self.now() * 1000:
            extra = (f"У вас уже есть Премиум до <b>{fmt_d(premium_until_ms)}</b>. "
                     f"Оплата продлит подписку ещё на 1 месяц.\n\n")
        return (
            f"💎 <b>Премиум на 1 месяц — {c.price} сомони</b>\n\n{extra}"
            f"Оплатите на карту:\n<code>{html.escape(c.card)}</code>\n"
            f"Получатель: <b>{html.escape(c.holder)}</b>\n"
            f"Сумма: <b>{c.price} сомони</b>\n"
            f"Комментарий к переводу: <code>{html.escape(c.note)}</code>\n\n"
            f"После оплаты отправьте сюда чек — фото или скриншот.\n"
            f"⏳ Время ожидания — <b>{left} мин.</b>")

    def admin_caption(self, pay, suffix: str = "") -> str:
        u = self.store.user(pay["user_id"])
        name = html.escape((u["name"] if u and u["name"] else "") or (u["tg_name"] if u else "") or "—")
        uname = f"@{html.escape(u['username'])}" if u and u["username"] else "без username"
        return (
            f"🧾 <b>Чек на подписку Премиум</b> · заявка #{pay['id']}\n"
            f"👤 Имя: <b>{name}</b>\n"
            f"📞 Номер: <b>{fmt_phone(u['phone'] if u else '')}</b>\n"
            f"✈️ Telegram: {uname} · ID <code>{pay['user_id']}</code> · "
            f"<a href=\"tg://user?id={pay['user_id']}\">открыть чат</a>\n"
            f"💰 {self.cfg.price} сомони · 1 месяц\n"
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
            if cmd == "/help":
                return self.send(chat_id, "Нажмите /start, чтобы оформить Премиум, или /status, чтобы узнать срок подписки.")
            if frm["id"] in self.cfg.admins:
                if cmd == "/grant":
                    return self.cmd_grant(chat_id, arg)
                if cmd == "/revoke":
                    return self.cmd_revoke(chat_id, arg)
                if cmd == "/pending":
                    return self.cmd_pending(chat_id)
            return
        if m.get("photo") or (m.get("document") or {}).get("mime_type", "").startswith(("image/", "application/pdf")):
            return self.on_receipt(m)
        if self.store.active_request(frm["id"], self.now()):
            self.send(chat_id, "Отправьте чек об оплате <b>фото или скриншотом</b>.")
        else:
            self.send(chat_id, "Чтобы оформить Премиум, нажмите /start.")

    def remember_user(self, frm: dict, phone: str = "", name: str = "") -> None:
        full = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        self.store.upsert_user(frm["id"], name, phone, frm.get("username") or "", full)

    def login_code(self, m: dict, token: str) -> None:
        """Вход в приложение: бот шлёт 6-значный код, а в Firebase кладёт только его хэш."""
        frm, chat_id = m["from"], m["chat"]["id"]
        if not re.fullmatch(r"[0-9a-f]{16,64}", token):
            return self.send(chat_id, "Ссылка входа недействительна. Откройте приложение и повторите.")
        code = str(secrets.randbelow(900000) + 100000)
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        try:
            self.fb.set_verify(token, {"uid": frm["id"], "name": name, "h": hashlib.sha256((token + code).encode()).hexdigest(),
                                       "exp": int((self.now() + 600) * 1000)})
        except Exception:
            log.exception("Firebase verify")
            return self.send(chat_id, "⚠️ Сервер временно недоступен. Попробуйте ещё раз.")
        self.remember_user(frm)
        self.send(chat_id, f"🔐 Код входа в OK-Mobile: <code>{code}</code>\nДействует 10 минут. Никому его не сообщайте.")

    def cmd_start(self, m: dict, payload: str) -> None:
        if payload.startswith("v"):
            return self.login_code(m, payload[1:])
        phone, name = parse_payload(payload)
        self.remember_user(m["from"], phone, name)
        self.offer(m["from"]["id"], m["chat"]["id"])

    def cmd_status(self, uid: int, chat_id: int) -> None:
        p = self.store.premium(uid)
        if p and p["until"] > self.now() * 1000:
            self.send(chat_id, f"✅ Премиум активен до <b>{fmt_d(p['until'])}</b>.",
                      kb([[("➕ Продлить на 1 месяц", "retry")]]))
        else:
            self.send(chat_id, "Премиум не активен.", kb([[("💎 Оформить подписку", "retry")]]))

    def offer(self, uid: int, chat_id: int) -> None:
        """Показать реквизиты (создать заявку на оплату, если активной ещё нет)."""
        if self.store.review_request(uid):
            return self._processing(chat_id)
        now = self.now()
        pay = self.store.active_request(uid, now)
        if not pay:
            pid = self.store.create_request(uid, chat_id, now, self.cfg.wait_min * 60)
            pay = self.store.payment(pid)
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
        if data == "retry":
            self.answer(cq["id"])
            self.remember_user(frm)
            return self.offer(frm["id"], chat_id)
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
        who = html.escape(admin.get("first_name") or admin.get("username") or str(admin["id"]))
        if approve:
            cur = self.store.premium(uid)
            base = max(int(now * 1000), cur["until"] if cur else 0)
            until = base + self.cfg.days * DAY_MS
            try:
                self.fb.set_premium(uid, until)
            except Exception:
                log.exception("Firebase: не удалось записать подписку %s", uid)
                return self.answer(cq_id, "❌ Не удалось записать подписку в Firebase. Проверьте настройки и нажмите ещё раз.", True)
            self.store.set_premium(uid, until)
            self.store.set_status(pid, "approved", admin["id"], now)
            self.answer(cq_id, "Подтверждено")
            markup = kb([[("📱 Открыть приложение", "url:" + self.cfg.app_url)]]) if self.cfg.app_url.startswith("http") else None
            self.send(pay["chat_id"],
                      f"🎉 <b>Подписка Премиум оформлена!</b>\nДействует до <b>{fmt_d(until)}</b>.\n"
                      f"Ваш профиль в приложении разблокирован — откройте OK-Mobile.", markup)
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
            return self.send(chat_id, "Формат: /grant <telegram_id> [дней]")
        uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else self.cfg.days
        cur = self.store.premium(uid)
        until = max(int(self.now() * 1000), cur["until"] if cur else 0) + days * DAY_MS
        try:
            self.fb.set_premium(uid, until)
        except Exception:
            log.exception("Firebase grant")
            return self.send(chat_id, "❌ Не удалось записать в Firebase, см. журнал бота.")
        self.store.set_premium(uid, until)
        self.send(uid, f"🎁 Вам активирован Премиум до <b>{fmt_d(until)}</b>. Откройте приложение OK-Mobile.")
        self.send(chat_id, f"✅ Премиум для <code>{uid}</code> до {fmt_d(until)}.")

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

    def subscription_notices(self) -> None:
        now_ms = int(self.now() * 1000)
        for r in self.store.expiring(now_ms, 3 * DAY_MS):
            self.send(r["user_id"], f"⏰ Ваш Премиум заканчивается <b>{fmt_d(r['until'])}</b>. "
                                    f"Продлите заранее — новый месяц добавится к текущему сроку.",
                      kb([[("➕ Продлить на 1 месяц", "retry")]]))
            self.store.mark(r["user_id"], "warned")
        for r in self.store.ended(now_ms):
            self.send(r["user_id"], "Срок вашего Премиума закончился. Функции приложения снова заблокированы.",
                      kb([[("💎 Оформить подписку", "retry")]]))
            self.store.mark(r["user_id"], "ended")
            try:
                self.fb.clear_premium(r["user_id"])
            except Exception:
                log.exception("Firebase clear")

    def poll_timeout(self) -> int:
        nxt = self.store.next_expiry()
        return 25 if nxt is None else max(1, min(25, math.ceil(nxt - self.now())))

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
