import os
import psycopg2
from psycopg2.extras import RealDictCursor
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


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


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
        INSERT INTO users (telegram_id, username, full_name, role)
        VALUES (%s, 'owner', 'Owner', 'owner')
        ON CONFLICT (telegram_id) DO NOTHING
    """, (OWNER_ID,))

    conn.commit()
    conn.close()


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
    conn.close()


def get_user(telegram_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY role DESC, full_name")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_user_role(admin_id: int, target_id: int, new_role: str) -> dict:
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (target_id,))
    target = c.fetchone()

    if not admin or not target:
        conn.close()
        return {"ok": False, "error": "Пользователь не найден"}

    admin_level = ROLES.get(admin["role"], 0)
    target_level = ROLES.get(target["role"], 0)
    new_role_level = ROLES.get(new_role, 0)

    if target["telegram_id"] == OWNER_ID:
        conn.close()
        return {"ok": False, "error": "Нельзя изменить роль владельца"}

    if admin_level < ROLE_MANAGE_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Недостаточно прав для изменения ролей"}

    if new_role_level >= admin_level:
        conn.close()
        return {"ok": False, "error": "Нельзя выдать роль выше или равную своей"}

    if target_level >= admin_level:
        conn.close()
        return {"ok": False, "error": "Нельзя изменить роль пользователя с равным или более высоким уровнем"}

    c.execute("UPDATE users SET role = %s WHERE telegram_id = %s", (new_role, target_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def set_user_blocked(admin_id: int, target_id: int, blocked: bool) -> dict:
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (target_id,))
    target = c.fetchone()

    if not admin or not target:
        conn.close()
        return {"ok": False, "error": "Пользователь не найден"}

    admin_level = ROLES.get(admin["role"], 0)
    target_level = ROLES.get(target["role"], 0)

    if target["telegram_id"] == OWNER_ID:
        conn.close()
        return {"ok": False, "error": "Нельзя заблокировать владельца"}

    if admin_level < ROLE_MANAGE_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Недостаточно прав"}

    if target_level >= admin_level:
        conn.close()
        return {"ok": False, "error": "Нельзя заблокировать пользователя с равным или более высоким уровнем"}

    c.execute("UPDATE users SET is_blocked = %s WHERE telegram_id = %s", (1 if blocked else 0, target_id))
    conn.commit()
    conn.close()
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
    conn.close()
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
    conn.close()
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
    conn.close()
    return [dict(r) for r in rows]


def respond_complaint(admin_id: int, complaint_id: int, response: str, close: bool = True, status: str = "closed") -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    if not admin:
        conn.close()
        return {"ok": False, "error": "Администратор не найден"}
    admin_level = ROLES.get(admin["role"], 0)
    if admin_level < ADMIN_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Недостаточно прав"}

    status = status if status else ("closed" if close else "open")
    c.execute("""
        UPDATE complaints
        SET admin_response = %s, responded_by = %s, status = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (response, admin_id, status, complaint_id))
    conn.commit()
    conn.close()
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
    conn.close()
    return dict(row) if row else None
    

def set_complaint_status(admin_id: int, complaint_id: int, new_status: str) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = %s", (admin_id,))
    admin = c.fetchone()
    if not admin or ROLES.get(admin["role"], 0) < ADMIN_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Нет прав"}
    c.execute("UPDATE complaints SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (new_status, complaint_id))
    conn.commit()
    conn.close()
    return {"ok": True}
