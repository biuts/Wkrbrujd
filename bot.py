import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from database import (
    init_db, upsert_user, get_user, get_all_users, set_user_role,
    set_user_blocked, create_complaint, get_all_complaints,
    get_user_complaints, respond_complaint, get_complaint,
    ROLES, ROLE_NAMES, ADMIN_MIN_LEVEL
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com")  # URL где хостится index.html
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def verify_telegram_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram WebApp initData"""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_val):
            return None
        user_data = json.loads(parsed.get("user", "{}"))
        return user_data
    except Exception as e:
        logger.error(f"verify error: {e}")
        return None


def json_response(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        status=status
    )


async def auth_middleware(request: web.Request):
    """Извлекает и проверяет пользователя из заголовка"""
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data:
        return None
    tg_user = verify_telegram_data(init_data)
    if not tg_user:
        return None
    tg_id = tg_user.get("id")
    username = tg_user.get("username", "")
    full_name = f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
    upsert_user(tg_id, username, full_name)
    user = get_user(tg_id)
    return user


# ─── API routes ────────────────────────────────────────────────────────────────

async def api_me(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if user["is_blocked"]:
        return json_response({"error": "Заблокирован"}, 403)
    user["role_level"] = ROLES.get(user["role"], 1)
    user["role_name"] = ROLE_NAMES.get(user["role"], "Пользователь")
    return json_response(user)


async def api_users(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        return json_response({"error": "Нет доступа"}, 403)
    users = get_all_users()
    for u in users:
        u["role_level"] = ROLES.get(u["role"], 1)
        u["role_name"] = ROLE_NAMES.get(u["role"], "Пользователь")
    return json_response(users)


async def api_set_role(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    body = await request.json()
    target_id = body.get("target_id")
    new_role = body.get("role")
    if not target_id or not new_role:
        return json_response({"error": "Неверные параметры"}, 400)
    result = set_user_role(user["telegram_id"], target_id, new_role)
    return json_response(result)


async def api_set_blocked(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    body = await request.json()
    target_id = body.get("target_id")
    blocked = body.get("blocked", True)
    if not target_id:
        return json_response({"error": "Неверные параметры"}, 400)
    result = set_user_blocked(user["telegram_id"], target_id, blocked)
    return json_response(result)


async def api_create_complaint(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if user["is_blocked"]:
        return json_response({"error": "Заблокирован"}, 403)
    body = await request.json()
    ctype = body.get("type")
    violator_username = body.get("violator_username", "")
    violator_id = body.get("violator_id")
    description = body.get("description", "")
    evidence = body.get("evidence", "")
    if not ctype or not violator_username or not description:
        return json_response({"error": "Заполните обязательные поля"}, 400)
    cid = create_complaint(user["telegram_id"], ctype, violator_username, violator_id, description, evidence)
    return json_response({"ok": True, "id": cid})


async def api_get_complaints(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        return json_response({"error": "Нет доступа"}, 403)
    status_filter = request.rel_url.query.get("status")
    type_filter = request.rel_url.query.get("type")
    complaints = get_all_complaints(status_filter, type_filter)
    for c in complaints:
        if c.get("responded_by"):
            responder = get_user(c["responded_by"])
            if responder:
                c["responder_name"] = responder["full_name"]
                c["responder_username"] = responder["username"]
                c["responder_role_name"] = ROLE_NAMES.get(responder["role"], "")
    return json_response(complaints)


async def api_my_complaints(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    complaints = get_user_complaints(user["telegram_id"])
    for c in complaints:
        if c.get("responded_by"):
            responder = get_user(c["responded_by"])
            if responder:
                c["responder_name"] = responder["full_name"]
                c["responder_role_name"] = ROLE_NAMES.get(responder["role"], "")
    return json_response(complaints)


async def api_respond_complaint(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        return json_response({"error": "Нет доступа"}, 403)
    body = await request.json()
    complaint_id = body.get("complaint_id")
    response_text = body.get("response", "")
    close = body.get("close", True)
    if not complaint_id or not response_text:
        return json_response({"error": "Неверные параметры"}, 400)
    result = respond_complaint(user["telegram_id"], complaint_id, response_text, close, status)
    return json_response(result)


async def api_get_complaint(request: web.Request):
    user = await auth_middleware(request)
    if not user:
        return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        return json_response({"error": "Нет доступа"}, 403)
    complaint_id = int(request.match_info["id"])
    complaint = get_complaint(complaint_id)
    if not complaint:
        return json_response({"error": "Жалоба не найдена"}, 404)
    if complaint.get("responded_by"):
        responder = get_user(complaint["responded_by"])
        if responder:
            complaint["responder_name"] = responder["full_name"]
            complaint["responder_username"] = responder["username"]
            complaint["responder_role_name"] = ROLE_NAMES.get(responder["role"], "")
    return json_response(complaint)


async def api_roles(request: web.Request):
    """Список ролей для фронтенда"""
    return json_response(ROLE_NAMES)


# ─── Bot handlers ──────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    upsert_user(tg_id, username, full_name)
    user = get_user(tg_id)

    if user and user["is_blocked"]:
        await message.answer("🚫 Вы заблокированы в этом боте.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛡 Открыть систему обращений",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]])
    role_name = ROLE_NAMES.get(user["role"] if user else "user", "Пользователь")
    await message.answer(
        f"👋 Добро пожаловать в <b>FROSTBANE UNION</b>!\n\n"
        f"Система обращений позволяет:\n"
        f"• Подавать жалобы на игроков и администраторов\n"
        f"• Предлагать улучшения\n"
        f"• Отслеживать статус своих обращений\n\n"
        f"Ваша роль: <b>{role_name}</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ─── App setup ─────────────────────────────────────────────────────────────────
async def serve_index(request: web.Request):
    return web.FileResponse('index.html')
def create_app():
    app = web.Application()
    app.router.add_get('/', serve_index)
    app.router.add_get('/index.html', serve_index)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/users", api_users)
    app.router.add_post("/api/set-role", api_set_role)
    app.router.add_post("/api/set-blocked", api_set_blocked)
    app.router.add_post("/api/complaint", api_create_complaint)
    app.router.add_get("/api/complaints", api_get_complaints)
    app.router.add_get("/api/my-complaints", api_my_complaints)
    app.router.add_post("/api/complaint/respond", api_respond_complaint)
    app.router.add_get("/api/complaint/{id}", api_get_complaint)
    app.router.add_get("/api/roles", api_roles)
    return app


async def main():
    init_db()
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"API сервер запущен на порту {PORT}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
