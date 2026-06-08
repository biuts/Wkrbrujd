import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = 5497334125

ROLES = {
    "owner": 6,
    "main_admin": 5,
    "senior_admin": 4,
    "admin": 3,
    "junior_admin": 2,
    "user": 1,
}

ROLE_NAMES = {
    "owner": "Владелец",
    "main_admin": "Главный администратор",
    "senior_admin": "Старший администратор",
    "admin": "Администратор",
    "junior_admin": "Младший администратор",
    "user": "Пользователь",
}

ADMIN_MIN_LEVEL = 2
ROLE_MANAGE_MIN_LEVEL = 4

# Connection pool — переиспользуем соединения вместо создания новых каждый раз
_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor
        )
    return _pool

def get_conn():
    return get_pool().getconn()

def release_conn(conn):
    get_pool().putconn(conn)


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'user',
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            reporter_id BIGINT NOT NULL,
            violator_username TEXT,
            violator_id BIGINT,
            description TEXT NOT NULL,
            evidence TEXT,
            status TEXT DEFAULT 'open',
            admin_response TEXT,
            responded_by BIGINT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            sender_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            reply_to INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
            is_deleted BOOLEAN DEFAULT FALSE,
            edited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add edited column if not exists
    c.execute("""
        ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS edited BOOLEAN DEFAULT FALSE
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_sender ON chat_messages(sender_id)")

    # Индексы для быстрых запросов
    c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_reporter ON complaints(reporter_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_type ON complaints(type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")

    c.execute("""
        INSERT INTO users (telegram_id, username, full_name, role)
        VALUES (%s, 'owner', 'Owner', 'owner')
        ON CONFLICT (telegram_id) DO NOTHING
    """, (OWNER_ID,))

    conn.commit()
    release_conn(conn)


def upsert_user(telegram_id: int, username: str, full_name: str):
    conn = get_conn()
    c = conn.cursor()
    role = "owner" if telegram_id == OWNER_ID else None

    c.execute("SELECT role FROM users WHERE telegram_id = %s", (telegram_id,))
    existing = c.fetchone()
    if existing:
        c.execute("UPDATE users SET username = %s, full_name = %s WHERE telegram_id = %s",
                  (username, full_name, telegram_id))
    else:
        c.execute("""
            INSERT INTO users (telegram_id, username, full_name, role)
            VALUES (%s, %s, %s, %s)
        """, (telegram_id, username, full_name, role or "user"))
    conn.commit()
    release_conn(conn)


def get_user(telegram_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    row = c.fetchone()
    release_conn(conn)
    return dict(row) if row else None


def get_all_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY role DESC, full_name")
    rows = c.fetchall()
    release_conn(conn)
    return [dict(r) for r in rows]


def set_user_role(admin_id: int, target_id: int, new_role: str) -> dict:
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (target_id,))
    target = c.fetchone()

    if not admin or not target:
        release_conn(conn)
        return {"ok": False, "error": "Пользователь не найден"}

    admin_level = ROLES.get(admin["role"], 0)
    target_level = ROLES.get(target["role"], 0)
    new_role_level = ROLES.get(new_role, 0)

    if target["telegram_id"] == OWNER_ID:
        release_conn(conn)
        return {"ok": False, "error": "Нельзя изменить роль владельца"}

    if admin_level < ROLE_MANAGE_MIN_LEVEL:
        release_conn(conn)
        return {"ok": False, "error": "Недостаточно прав для изменения ролей"}

    if new_role_level >= admin_level:
        release_conn(conn)
        return {"ok": False, "error": "Нельзя выдать роль выше или равную своей"}

    if target_level >= admin_level:
        release_conn(conn)
        return {"ok": False, "error": "Нельзя изменить роль пользователя с равным или более высоким уровнем"}

    c.execute("UPDATE users SET role = %s WHERE telegram_id = %s", (new_role, target_id))
    conn.commit()
    release_conn(conn)
    return {"ok": True}


def set_user_blocked(admin_id: int, target_id: int, blocked: bool) -> dict:
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (target_id,))
    target = c.fetchone()

    if not admin or not target:
        release_conn(conn)
        return {"ok": False, "error": "Пользователь не найден"}

    admin_level = ROLES.get(admin["role"], 0)
    target_level = ROLES.get(target["role"], 0)

    if target["telegram_id"] == OWNER_ID:
        release_conn(conn)
        return {"ok": False, "error": "Нельзя заблокировать владельца"}

    if admin_level < ROLE_MANAGE_MIN_LEVEL:
        release_conn(conn)
        return {"ok": False, "error": "Недостаточно прав"}

    if target_level >= admin_level:
        release_conn(conn)
        return {"ok": False, "error": "Нельзя заблокировать пользователя с равным или более высоким уровнем"}

    c.execute("UPDATE users SET is_blocked = %s WHERE telegram_id = %s", (1 if blocked else 0, target_id))
    conn.commit()
    release_conn(conn)
    return {"ok": True}


def create_complaint(reporter_id: int, ctype: str, violator_username: str,
                     violator_id, description: str, evidence: str = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO complaints (type, reporter_id, violator_username, violator_id, description, evidence)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """, (ctype, reporter_id, violator_username, violator_id, description, evidence))
    complaint_id = c.fetchone()["id"]
    conn.commit()
    release_conn(conn)
    return complaint_id


def get_all_complaints(status_filter=None, type_filter=None):
    conn = get_conn()
    c = conn.cursor()
    query = """
        SELECT c.*, u.username as reporter_username, u.full_name as reporter_name
        FROM complaints c
        LEFT JOIN users u ON c.reporter_id = u.telegram_id
    """
    params = []
    conditions = []
    if status_filter:
        conditions.append("c.status = %s")
        params.append(status_filter)
    if type_filter:
        conditions.append("c.type = %s")
        params.append(type_filter)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY c.created_at DESC"
    c.execute(query, params)
    rows = c.fetchall()
    release_conn(conn)
    return [dict(r) for r in rows]


def get_user_complaints(reporter_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT c.*, u.full_name as responder_name, u.username as responder_username, u2.role as responder_role
        FROM complaints c
        LEFT JOIN users u ON c.responded_by = u.telegram_id
        LEFT JOIN users u2 ON c.responded_by = u2.telegram_id
        WHERE c.reporter_id = %s
        ORDER BY c.created_at DESC
    """, (reporter_id,))
    rows = c.fetchall()
    release_conn(conn)
    return [dict(r) for r in rows]


def respond_complaint(admin_id: int, complaint_id: int, response: str, close: bool = True, status: str = "closed") -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    if not admin:
        release_conn(conn)
        return {"ok": False, "error": "Администратор не найден"}
    admin_level = ROLES.get(admin["role"], 0)
    if admin_level < ADMIN_MIN_LEVEL:
        release_conn(conn)
        return {"ok": False, "error": "Недостаточно прав"}

    status = status if status else ("closed" if close else "open")
    c.execute("""
        UPDATE complaints
        SET admin_response = %s, responded_by = %s, status = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (response, admin_id, status, complaint_id))
    conn.commit()
    release_conn(conn)
    return {"ok": True}


def get_complaint(complaint_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT c.*,
               u1.username as reporter_username, u1.full_name as reporter_name,
               u2.username as responder_username, u2.full_name as responder_name, u2.role as responder_role
        FROM complaints c
        LEFT JOIN users u1 ON c.reporter_id = u1.telegram_id
        LEFT JOIN users u2 ON c.responded_by = u2.telegram_id
        WHERE c.id = %s
    """, (complaint_id,))
    row = c.fetchone()
    release_conn(conn)
    return dict(row) if row else None
    

def set_complaint_status(admin_id: int, complaint_id: int, new_status: str) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    if not admin or ROLES.get(admin["role"], 0) < ADMIN_MIN_LEVEL:
        release_conn(conn)
        return {"ok": False, "error": "Нет прав"}
    c.execute("UPDATE complaints SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (new_status, complaint_id))
    conn.commit()
    release_conn(conn)
    return {"ok": True}

def get_admins():
    conn = get_conn()
    c = conn.cursor()
    admin_roles = [role for role, level in ROLES.items() if level >= ADMIN_MIN_LEVEL]
    placeholders = ",".join(["%s"] * len(admin_roles))
    c.execute(f"SELECT * FROM users WHERE role IN ({placeholders}) AND is_blocked = 0", admin_roles)
    rows = c.fetchall()
    release_conn(conn)
    return [dict(r) for r in rows]



# ── CHAT ──────────────────────────────────────────────────

def send_chat_message(sender_id: int, text: str, reply_to: int = None) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE telegram_id = %s", (sender_id,))
    user = c.fetchone()
    if not user or ROLES.get(user["role"], 0) < ADMIN_MIN_LEVEL:
        release_conn(conn)
        return {"ok": False, "error": "Нет доступа"}
    c.execute("""
        INSERT INTO chat_messages (sender_id, text, reply_to)
        VALUES (%s, %s, %s) RETURNING id, created_at
    """, (sender_id, text.strip(), reply_to))
    row = c.fetchone()
    conn.commit()
    release_conn(conn)
    return {"ok": True, "id": row["id"], "created_at": str(row["created_at"])}


def get_chat_messages(limit: int = 100, before_id: int = None) -> list:
    conn = get_conn()
    c = conn.cursor()
    if before_id:
        c.execute("""
            SELECT m.id, m.sender_id, m.text, m.reply_to, m.is_deleted, m.created_at,
                   u.username, u.full_name, u.role,
                   r.text as reply_text, ru.username as reply_username, ru.role as reply_role
            FROM chat_messages m
            LEFT JOIN users u ON m.sender_id = u.telegram_id
            LEFT JOIN chat_messages r ON m.reply_to = r.id
            LEFT JOIN users ru ON r.sender_id = ru.telegram_id
            WHERE m.id < %s
            ORDER BY m.id DESC LIMIT %s
        """, (before_id, limit))
    else:
        c.execute("""
            SELECT m.id, m.sender_id, m.text, m.reply_to, m.is_deleted, m.created_at,
                   u.username, u.full_name, u.role,
                   r.text as reply_text, ru.username as reply_username, ru.role as reply_role
            FROM chat_messages m
            LEFT JOIN users u ON m.sender_id = u.telegram_id
            LEFT JOIN chat_messages r ON m.reply_to = r.id
            LEFT JOIN users ru ON r.sender_id = ru.telegram_id
            ORDER BY m.id DESC LIMIT %s
        """, (limit,))
    rows = c.fetchall()
    release_conn(conn)
    return list(reversed([dict(r) for r in rows]))


def delete_chat_message(user_id: int, message_id: int) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE telegram_id = %s", (user_id,))
    user = c.fetchone()
    if not user:
        release_conn(conn)
        return {"ok": False, "error": "Пользователь не найден"}
    user_level = ROLES.get(user["role"], 0)
    c.execute("SELECT sender_id FROM chat_messages WHERE id = %s AND is_deleted = FALSE", (message_id,))
    msg = c.fetchone()
    if not msg:
        release_conn(conn)
        return {"ok": False, "error": "Сообщение не найдено"}
    is_own = msg["sender_id"] == user_id
    is_senior = user_level >= ROLES.get("senior_admin", 4)
    if not is_own and not is_senior:
        release_conn(conn)
        return {"ok": False, "error": "Нет прав"}
    c.execute("UPDATE chat_messages SET is_deleted = TRUE WHERE id = %s", (message_id,))
    conn.commit()
    release_conn(conn)
    return {"ok": True}


def get_new_chat_messages(after_id: int) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT m.id, m.sender_id, m.text, m.reply_to, m.is_deleted, m.created_at,
               u.username, u.full_name, u.role,
               r.text as reply_text, ru.username as reply_username, ru.role as reply_role
        FROM chat_messages m
        LEFT JOIN users u ON m.sender_id = u.telegram_id
        LEFT JOIN chat_messages r ON m.reply_to = r.id
        LEFT JOIN users ru ON r.sender_id = ru.telegram_id
        WHERE m.id > %s
        ORDER BY m.id ASC
    """, (after_id,))
    rows = c.fetchall()
    release_conn(conn)
    return [dict(r) for r in rows]


def edit_chat_message(user_id: int, message_id: int, new_text: str) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE telegram_id = %s", (user_id,))
    user = c.fetchone()
    if not user:
        release_conn(conn)
        return {"ok": False, "error": "Пользователь не найден"}
    user_level = ROLES.get(user["role"], 0)
    c.execute("SELECT sender_id, is_deleted FROM chat_messages WHERE id = %s", (message_id,))
    msg = c.fetchone()
    if not msg or msg["is_deleted"]:
        release_conn(conn)
        return {"ok": False, "error": "Сообщение не найдено"}
    is_own = msg["sender_id"] == user_id
    is_owner = user_level >= ROLES.get("owner", 6)
    if not is_own and not is_owner:
        release_conn(conn)
        return {"ok": False, "error": "Нет прав"}
    new_text = new_text.strip()
    if not new_text:
        release_conn(conn)
        return {"ok": False, "error": "Пустое сообщение"}
    c.execute("UPDATE chat_messages SET text = %s, edited = TRUE WHERE id = %s", (new_text, message_id))
    conn.commit()
    release_conn(conn)
    return {"ok": True}
