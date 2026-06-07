import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from database import (
    init_db, upsert_user, get_user, get_all_users, set_user_role,
    set_user_blocked, create_complaint, get_all_complaints,
    get_user_complaints, respond_complaint, get_complaint,
    set_complaint_status, get_admins,
    ROLES, ROLE_NAMES, ADMIN_MIN_LEVEL
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com")
PORT = int(os.getenv("PORT", 8080))
OWNER_ID = int(os.getenv("OWNER_ID", "5497334125"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── Шаблоны ───────────────────────────────────────────────────────────────────

TEMPLATES = {
    "closed_1": {
        "label": "❌ Закрыто — нет нарушений",
        "status": "closed",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «Закрыто».\nИгрок не совершал запрещённых действий, нарушений правил не выявлено."
    },
    "closed_2": {
        "label": "❌ Закрыто — нет доказательств",
        "status": "closed",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «Закрыто».\nНедостаточно доказательств."
    },
    "approved_1": {
        "label": "✅ Одобрено — беседа",
        "status": "approved",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «Одобрено».\nС игроком будет проведена воспитательная беседа."
    },
    "approved_2": {
        "label": "✅ Одобрено — исключение",
        "status": "approved",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «Одобрено».\nИгрок будет исключен из семьи."
    },
    "approved_3": {
        "label": "✅ Одобрено — наказание",
        "status": "approved",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «Одобрено».\nИгрок получит соответствующее наказание."
    },
    "reviewing": {
        "label": "🔍 На рассмотрении",
        "status": "reviewing",
        "text": "Здравствуйте уважаемый пользователь.\nВаша жалоба получает статус: «На рассмотрении».\nМы сделаем проверку данного игрока."
    },
}

TYPE_NAMES = {
    "player": "На игрока",
    "admin": "На зама",
    "suggestion": "Предложение",
}

# ─── Клавиатуры ────────────────────────────────────────────────────────────────

def complaint_keyboard(complaint_id: int, status: str) -> InlineKeyboardMarkup:
    buttons = []
    if status == "open":
        buttons.append([
            InlineKeyboardButton(text="🔍 На рассмотрении", callback_data=f"tpl:reviewing:{complaint_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📋 Шаблоны ответа", callback_data=f"templates:{complaint_id}"),
        ])
    elif status == "reviewing":
        buttons.append([
            InlineKeyboardButton(text="📋 Шаблоны ответа", callback_data=f"templates:{complaint_id}"),
        ])
    buttons.append([
        InlineKeyboardButton(text="🔗 Открыть в приложении", url=WEBAPP_URL)
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def templates_keyboard(complaint_id: int) -> InlineKeyboardMarkup:
    buttons = []
    for key, tpl in TEMPLATES.items():
        if key == "reviewing":
            continue
        buttons.append([InlineKeyboardButton(
            text=tpl["label"],
            callback_data=f"tpl:{key}:{complaint_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back:{complaint_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── Уведомления ───────────────────────────────────────────────────────────────

async def notify_admins_new_complaint(complaint: dict):
    """Уведомляет всех админов о новой жалобе"""
    admins = get_admins()
    ctype = TYPE_NAMES.get(complaint.get("type", ""), complaint.get("type", ""))
    reporter = complaint.get("reporter_username") or complaint.get("reporter_name") or "Неизвестно"
    violator = complaint.get("violator_username") or "—"
    desc = (complaint.get("description") or "")[:200]
    cid = complaint["id"]

    text = (
        f"🆕 <b>Новая жалоба #{cid}</b>\n"
        f"{'─'*28}\n"
        f"📂 <b>Тип:</b> {ctype}\n"
        f"👤 <b>От:</b> @{reporter}\n"
        f"🎯 <b>На кого:</b> {violator}\n"
        f"📝 <b>Описание:</b>\n{desc}"
    )

    kb = complaint_keyboard(cid, "open")
    for admin in admins:
        try:
            await bot.send_message(admin["telegram_id"], text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.error(f"Не удалось уведомить {admin['telegram_id']}: {e}")

async def notify_user_response(complaint: dict, template_key: str):
    """Уведомляет пользователя об ответе на жалобу"""
    tpl = TEMPLATES[template_key]
    reporter_id = complaint["reporter_id"]
    cid = complaint["id"]
    try:
        await bot.send_message(
            reporter_id,
            f"📋 <b>Ответ по жалобе #{cid}</b>\n\n{tpl['text']}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {reporter_id}: {e}")

# ─── Callback handlers ─────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("templates:"))
async def cb_templates(call: types.CallbackQuery):
    complaint_id = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=templates_keyboard(complaint_id))
    await call.answer()

@dp.callback_query(F.data.startswith("back:"))
async def cb_back(call: types.CallbackQuery):
    complaint_id = int(call.data.split(":")[1])
    complaint = get_complaint(complaint_id)
    if complaint:
        await call.message.edit_reply_markup(
            reply_markup=complaint_keyboard(complaint_id, complaint.get("status", "open"))
        )
    await call.answer()

@dp.callback_query(F.data.startswith("tpl:"))
async def cb_template(call: types.CallbackQuery):
    parts = call.data.split(":")
    tpl_key = parts[1]
    complaint_id = int(parts[2])

    tpl = TEMPLATES.get(tpl_key)
    if not tpl:
        await call.answer("❌ Шаблон не найден", show_alert=True)
        return

    user = get_user(call.from_user.id)
    if not user or ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return

    complaint = get_complaint(complaint_id)
    if not complaint:
        await call.answer("❌ Жалоба не найдена", show_alert=True)
        return

    new_status = tpl["status"]
    close = new_status in ("closed", "approved")

    result = respond_complaint(call.from_user.id, complaint_id, tpl["text"], close, new_status)
    if not result.get("ok"):
        await call.answer(f"❌ {result.get('error', 'Ошибка')}", show_alert=True)
        return

    # Уведомляем пользователя
    await notify_user_response(complaint, tpl_key)

    status_icons = {
        "closed": "❌ Закрыто",
        "approved": "✅ Одобрено",
        "reviewing": "🔍 На рассмотрении",
    }
    status_str = status_icons.get(new_status, new_status)

    admin_name = user.get("full_name") or f"@{user.get('username')}"
    await call.message.edit_text(
        call.message.text + f"\n\n{'─'*28}\n{status_str} · <i>{admin_name}</i>\n<i>{tpl['text'][:100]}...</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Открыть в приложении", url=WEBAPP_URL)
        ]])
    )
    await call.answer(f"✅ {status_str}")

# ─── Bot commands ──────────────────────────────────────────────────────────────

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

# ─── Auth & API ────────────────────────────────────────────────────────────────

def verify_telegram_data(init_data: str) -> dict | None:
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
    return get_user(tg_id)

async def api_me(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if user["is_blocked"]: return json_response({"error": "Заблокирован"}, 403)
    user["role_level"] = ROLES.get(user["role"], 1)
    user["role_name"] = ROLE_NAMES.get(user["role"], "Пользователь")
    return json_response(user)

async def api_users(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL: return json_response({"error": "Нет доступа"}, 403)
    users = get_all_users()
    for u in users:
        u["role_level"] = ROLES.get(u["role"], 1)
        u["role_name"] = ROLE_NAMES.get(u["role"], "Пользователь")
    return json_response(users)

async def api_set_role(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    body = await request.json()
    return json_response(set_user_role(user["telegram_id"], body.get("target_id"), body.get("role")))

async def api_set_blocked(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    body = await request.json()
    return json_response(set_user_blocked(user["telegram_id"], body.get("target_id"), body.get("blocked", True)))

async def api_create_complaint(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if user["is_blocked"]: return json_response({"error": "Заблокирован"}, 403)
    body = await request.json()
    ctype = body.get("type")
    violator_username = body.get("violator_username", "")
    violator_id = body.get("violator_id")
    description = body.get("description", "")
    evidence = body.get("evidence", "")
    if not ctype or not violator_username or not description:
        return json_response({"error": "Заполните обязательные поля"}, 400)
    cid = create_complaint(user["telegram_id"], ctype, violator_username, violator_id, description, evidence)
    # Уведомляем админов
    complaint = get_complaint(cid)
    if complaint:
        asyncio.create_task(notify_admins_new_complaint(complaint))
    return json_response({"ok": True, "id": cid})

async def api_get_complaints(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL: return json_response({"error": "Нет доступа"}, 403)
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

async def api_my_complaints(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    complaints = get_user_complaints(user["telegram_id"])
    for c in complaints:
        if c.get("responded_by"):
            responder = get_user(c["responded_by"])
            if responder:
                c["responder_name"] = responder["full_name"]
                c["responder_role_name"] = ROLE_NAMES.get(responder["role"], "")
    return json_response(complaints)

async def api_respond_complaint(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL: return json_response({"error": "Нет доступа"}, 403)
    body = await request.json()
    complaint_id = body.get("complaint_id")
    response_text = body.get("response", "")
    close = body.get("close", True)
    new_status = body.get("status", "closed" if close else "open")
    if not complaint_id or not response_text:
        return json_response({"error": "Неверные параметры"}, 400)
    return json_response(respond_complaint(user["telegram_id"], complaint_id, response_text, close, new_status))

async def api_get_complaint(request):
    user = await auth_middleware(request)
    if not user: return json_response({"error": "Unauthorized"}, 401)
    if ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL: return json_response({"error": "Нет доступа"}, 403)
    complaint_id = int(request.match_info["id"])
    complaint = get_complaint(complaint_id)
    if not complaint: return json_response({"error": "Жалоба не найдена"}, 404)
    if complaint.get("responded_by"):
        responder = get_user(complaint["responded_by"])
        if responder:
            complaint["responder_name"] = responder["full_name"]
            complaint["responder_username"] = responder["username"]
            complaint["responder_role_name"] = ROLE_NAMES.get(responder["role"], "")
    return json_response(complaint)

async def api_roles(request):
    return json_response(ROLE_NAMES)

async def api_index(request):
    return web.FileResponse("index.html")

def create_app():
    app = web.Application()
    app.router.add_get("/", api_index)
    app.router.add_get("/favicon.ico", lambda r: web.Response(status=204))
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
        
