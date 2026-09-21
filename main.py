import os
import re
import asyncio
import random
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    ChannelPrivateError,
    AuthKeyUnregisteredError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import zoneinfo

# =========================================================
#                     НАСТРОЙКИ
# =========================================================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TARGET_BOT = os.environ.get("TARGET_BOT", "@zvezdolovbot")
START_COMMAND = os.environ.get("START_COMMAND", "/start")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", 61))
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"
BONUS_TZ = zoneinfo.ZoneInfo("Europe/Samara")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("zvezdolov")

# =========================================================
#                 СЛУЖЕБНЫЕ ССЫЛКИ TELEGRAM
# =========================================================
SKIP_USERNAMES = {
    "share", "addstickers", "addemoji", "proxy", "socks",
    "setlanguage", "joinchat", "iv", "c", "s", "addtheme",
    "login", "confirmphone", "socks", "bg", "addlist",
}


# =========================================================
#                    ЗАГРУЗКА СЕССИЙ
# =========================================================
SESSIONS = []
for i in range(1, 21):
    val = os.environ.get(f"SESSION_STR_{i}")
    if val and val.strip():
        SESSIONS.append((i, val.strip()))

if not SESSIONS and os.environ.get("SESSION_STR"):
    SESSIONS.append((1, os.environ["SESSION_STR"].strip()))

if not SESSIONS:
    log.error("❌ Нет сессий. Добавь SESSION_STR_1, SESSION_STR_2...")
    raise SystemExit(1)

log.info(f"🔑 Сессий загружено: {len(SESSIONS)}")

clients = [
    (idx, TelegramClient(StringSession(sess), API_ID, API_HASH))
    for idx, sess in SESSIONS
]


# =========================================================
#                     ХЕЛПЕРЫ
# =========================================================
async def human_pause(min_s=1.0, max_s=3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


def extract_invite_or_username(url: str):
    """
    Возвращает:
      ('invite', hash)     — для t.me/+hash и t.me/joinchat/hash
      ('username', name)   — для t.me/name
      None                 — если служебная ссылка
    """
    if not url:
        return None

    # Игнорируем служебные ссылки
    for skip in ("/share/", "/addstickers/", "/addemoji/", "/proxy",
                 "/socks", "/setlanguage/", "/addtheme/", "/login",
                 "/confirmphone", "/bg/", "/addlist"):
        if skip in url:
            return None

    # Invite-ссылка: t.me/+hash  или  t.me/joinchat/hash
    m = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", url)
    if m:
        return ("invite", m.group(1))

    # Публичный канал: t.me/username
    m = re.search(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,31})", url)
    if m:
        username = m.group(1)
        if username.lower() in SKIP_USERNAMES:
            return None
        return ("username", username)

    return None


async def subscribe(c: TelegramClient, kind: str, value: str) -> bool:
    """Подписка: по invite-хэшу или по username. Все ошибки обрабатываются."""
    try:
        if kind == "invite":
            await c(ImportChatInviteRequest(value))
            log.info(f"   ✅ Подписался (invite): +{value}")
        else:
            await c(JoinChannelRequest(value))
            log.info(f"   ✅ Подписался: @{value}")
        return True

    except UserAlreadyParticipantError:
        log.info(f"   ℹ️ Уже подписан: {value}")
        return True

    except InviteHashExpiredError:
        log.warning(f"   ⏰ Invite просрочена: +{value}")
        return False

    except InviteHashInvalidError:
        log.warning(f"   ❌ Неверный invite: +{value}")
        return False

    except ChannelPrivateError:
        log.warning(f"   🚫 Закрытый канал: {value}")
        return False

    except (UsernameInvalidError, UsernameNotOccupiedError):
        log.info(f"   ⏭ Пропуск (недействительный username): {value}")
        return False

    except FloodWaitError as e:
        log.warning(f"   ⏳ FloodWait {e.seconds}с — ждём")
        await asyncio.sleep(e.seconds)
        return False

    except TypeError as e:
        # "Cannot cast InputPeerUser to InputChannel" — ссылка на юзера/бота
        if "InputPeerUser" in str(e) or "InputChannel" in str(e):
            log.info(f"   ⏭ Пропуск (это юзер/бот, а не канал): {value}")
            return False
        log.warning(f"   ❌ TypeError: {e}")
        return False

    except Exception as e:
        err = str(e)
        if "Nobody is using this username" in err or "username is unacceptable" in err:
            log.info(f"   ⏭ Пропуск (невалидный username): {value}")
            return False
        log.warning(f"   ❌ Ошибка подписки {value}: {e}")
        return False


async def press_button(c: TelegramClient, msg, keyword: str) -> bool:
    """Нажимает inline callback-кнопку по ключевому слову."""
    if not msg.buttons:
        return False
    for row in msg.buttons:
        for btn in row:
            if keyword.lower() in btn.text.lower():
                if btn.url:
                    continue  # URL-кнопки не жмём через callback
                try:
                    await btn.click()
                    log.info(f"   👆 Нажал: '{btn.text}'")
                    await human_pause(1.5, 3.0)
                    return True
                except Exception as e:
                    log.warning(f"   ❌ Не смог нажать '{btn.text}': {e}")
    return False


# =========================================================
#              ОБРАБОТКА ОТВЕТА БОТА
# =========================================================
def make_handler(idx: int, c: TelegramClient):
    async def handler(event):
        msg = event.message
        try:
            if msg.text:
                short = msg.text[:110].replace(chr(10), " | ")
                log.info(f"💬 [акк {idx}] Бот: {short}")

            if msg.buttons:
                log.info(f"   🔘 [акк {idx}] Кнопок: {len(msg.buttons)}")

                # 1. Собираем ссылки из URL-кнопок
                links = []
                for row in msg.buttons:
                    for btn in row:
                        if btn.url:
                            info = extract_invite_or_username(btn.url)
                            if info:
                                links.append(info)

                if links:
                    log.info(f"   📡 [акк {idx}] Каналов для подписки: {len(links)}")
                    for kind, value in links:
                        await subscribe(c, kind, value)
                        await human_pause(2.5, 6.0)

                # 2. Жмём «Я подписался» / «Проверить»
                clicked = await press_button(c, msg, "подписался")
                if not clicked:
                    clicked = await press_button(c, msg, "проверить")

                # 3. Если не нашли — пробуем другие полезные кнопки
                if not clicked:
                    for kw in ["забрать", "получить", "ускорить", "обновить", "бонус"]:
                        if await press_button(c, msg, kw):
                            break

        except Exception as e:
            log.exception(f"❌ [акк {idx}] Ошибка обработки: {e}")

    return handler


# =========================================================
#                ОСНОВНОЕ ДЕЙСТВИЕ
# =========================================================
async def run_interaction(idx: int, c: TelegramClient):
    try:
        log.info(f"▶️ [акк {idx}] Пишу '{START_COMMAND}' в {TARGET_BOT}")
        await human_pause(2.0, 6.0)
        await c.send_message(TARGET_BOT, START_COMMAND)
        log.info(f"   ✅ [акк {idx}] Отправлено")
    except FloodWaitError as e:
        log.warning(f"   ⏳ [акк {idx}] FloodWait {e.seconds}с")
        await asyncio.sleep(e.seconds)
    except AuthKeyUnregisteredError:
        log.error(f"   🚫 [акк {idx}] Сессия отозвана — обнови SESSION_STR_{idx}")
    except Exception as e:
        log.exception(f"   ❌ [акк {idx}] Ошибка: {e}")


# =========================================================
#                  РАСПИСАНИЕ
# =========================================================
scheduler = AsyncIOScheduler()


def schedule_tasks():
    if TEST_MODE:
        log.info("🧪 TEST_MODE: один запуск через 20 сек")
        for i, (idx, c) in enumerate(clients):
            scheduler.add_job(
                run_interaction,
                trigger="date",
                run_date=datetime.now(BONUS_TZ) + timedelta(seconds=20 + i * 3),
                args=[idx, c],
                id=f"test_{idx}",
                replace_existing=True,
            )
    else:
        total = len(clients)
        log.info(f"⏰ Интервал: каждые {INTERVAL_MINUTES} мин • Аккаунтов: {total}")
        step_sec = (INTERVAL_MINUTES * 60) // max(total, 1)
        log.info(f"📊 Сдвиг между аккаунтами: ~{step_sec} сек")

        for i, (idx, c) in enumerate(clients):
            offset_sec = i * step_sec
            scheduler.add_job(
                run_interaction,
                trigger="interval",
                minutes=INTERVAL_MINUTES,
                args=[idx, c],
                id=f"task_{idx}",
                replace_existing=True,
                next_run_time=datetime.now(BONUS_TZ) + timedelta(seconds=offset_sec),
            )


# =========================================================
#                       ЗАПУСК
# =========================================================
async def main():
    log.info("=" * 55)
    log.info(f"🚀 Запуск • Бот: {TARGET_BOT} • Аккаунтов: {len(clients)}")
    log.info("=" * 55)

    for idx, c in clients:
        c.add_event_handler(make_handler(idx, c), events.NewMessage(from_users=TARGET_BOT))

    started = []
    for i, (idx, c) in enumerate(clients):
        try:
            await c.start()
            me = await c.get_me()
            started.append((idx, c))
            log.info(f"🚀 [акк {idx}] Запущен как @{me.username or me.id}")
            if i < len(clients) - 1:
                await human_pause(1.5, 3.5)
        except AuthKeyUnregisteredError:
            log.error(f"🚫 [акк {idx}] Сессия недействительна")
        except Exception as e:
            log.exception(f"❌ [акк {idx}] Ошибка запуска: {e}")

    if not started:
        log.error("❌ Ни один не запустился")
        return

    clients.clear()
    clients.extend(started)

    schedule_tasks()
    scheduler.start()

    log.info("✅ Готово. Работаю.")
    await asyncio.gather(*(c.run_until_disconnected() for _, c in clients))


if __name__ == "__main__":
    with clients[0][1]:
        clients[0][1].loop.run_until_complete(main())
