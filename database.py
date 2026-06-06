import sqlite3
import os
from datetime import datetime

DB_PATH = "frostbane.db"

OWNER_ID = 5497334125

# Иерархия ролей (чем выше число — тем выше роль)
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

# Минимальный уровень роли для доступа в админ-панель
ADMIN_MIN_LEVEL = 2  # junior_admin и выше

# Минимальный уровень для управления ролями
ROLE_MANAGE_MIN_LEVEL = 4  # senior_admin и выше


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'user',
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            reporter_id INTEGER NOT NULL,
            violator_username TEXT,
            violator_id INTEGER,
            description TEXT NOT NULL,
            evidence TEXT,
            status TEXT DEFAULT 'open',
            admin_response TEXT,
            responded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Создаём владельца если не существует
    c.execute("""
        INSERT OR IGNORE INTO users (telegram_id, username, full_name, role)
        VALUES (?, 'owner', 'Owner', 'owner')
    """, (OWNER_ID,))

    conn.commit()
    conn.close()


def upsert_user(telegram_id: int, username: str, full_name: str):
    conn = get_conn()
    c = conn.cursor()
    # Если владелец — сразу owner
    role = "owner" if telegram_id == OWNER_ID else None

    existing = c.execute("SELECT role, is_blocked FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if existing:
        c.execute("UPDATE users SET username = ?, full_name = ? WHERE telegram_id = ?",
                  (username, full_name, telegram_id))
    else:
        c.execute("""
            INSERT INTO users (telegram_id, username, full_name, role)
            VALUES (?, ?, ?, ?)
        """, (telegram_id, username, full_name, role or "user"))
    conn.commit()
    conn.close()


def get_user(telegram_id: int):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM users ORDER BY role DESC, full_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_user_role(admin_id: int, target_id: int, new_role: str) -> dict:
    """Изменить роль пользователя. Возвращает {'ok': bool, 'error': str}"""
    conn = get_conn()
    c = conn.cursor()

    admin = c.execute("SELECT * FROM users WHERE telegram_id = ?", (admin_id,)).fetchone()
    target = c.execute("SELECT * FROM users WHERE telegram_id = ?", (target_id,)).fetchone()

    if not admin or not target:
        conn.close()
        return {"ok": False, "error": "Пользователь не найден"}

    admin_level = ROLES.get(admin["role"], 0)
    target_level = ROLES.get(target["role"], 0)
    new_role_level = ROLES.get(new_role, 0)

    # Нельзя трогать владельца
    if target["telegram_id"] == OWNER_ID:
        conn.close()
        return {"ok": False, "error": "Нельзя изменить роль владельца"}

    # Только senior_admin+ могут менять роли
    if admin_level < ROLE_MANAGE_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Недостаточно прав для изменения ролей"}

    # Нельзя выдать роль выше своей
    if new_role_level >= admin_level:
        conn.close()
        return {"ok": False, "error": "Нельзя выдать роль выше или равную своей"}

    # Нельзя менять роль тем, кто выше или равен тебе
    if target_level >= admin_level:
        conn.close()
        return {"ok": False, "error": "Нельзя изменить роль пользователя с равным или более высоким уровнем"}

    c.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (new_role, target_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def set_user_blocked(admin_id: int, target_id: int, blocked: bool) -> dict:
    conn = get_conn()
    c = conn.cursor()

    admin = c.execute("SELECT * FROM users WHERE telegram_id = ?", (admin_id,)).fetchone()
    target = c.execute("SELECT * FROM users WHERE telegram_id = ?", (target_id,)).fetchone()

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

    c.execute("UPDATE users SET is_blocked = ? WHERE telegram_id = ?", (1 if blocked else 0, target_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def create_complaint(reporter_id: int, ctype: str, violator_username: str,
                     violator_id, description: str, evidence: str = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO complaints (type, reporter_id, violator_username, violator_id, description, evidence)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (ctype, reporter_id, violator_username, violator_id, description, evidence))
    complaint_id = c.lastrowid
    conn.commit()
    conn.close()
    return complaint_id


def get_all_complaints(status_filter=None, type_filter=None):
    conn = get_conn()
    c = conn.cursor()
    query = "SELECT c.*, u.username as reporter_username, u.full_name as reporter_name FROM complaints c LEFT JOIN users u ON c.reporter_id = u.telegram_id"
    params = []
    conditions = []
    if status_filter:
        conditions.append("c.status = ?")
        params.append(status_filter)
    if type_filter:
        conditions.append("c.type = ?")
        params.append(type_filter)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY c.created_at DESC"
    rows = c.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_complaints(reporter_id: int):
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute("""
        SELECT c.*, u.full_name as responder_name, u.username as responder_username, u2.role as responder_role
        FROM complaints c
        LEFT JOIN users u ON c.responded_by = u.telegram_id
        LEFT JOIN users u2 ON c.responded_by = u2.telegram_id
        WHERE c.reporter_id = ?
        ORDER BY c.created_at DESC
    """, (reporter_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def respond_complaint(admin_id: int, complaint_id: int, response: str, close: bool = True) -> dict:
    conn = get_conn()
    c = conn.cursor()
    admin = c.execute("SELECT * FROM users WHERE telegram_id = ?", (admin_id,)).fetchone()
    if not admin:
        conn.close()
        return {"ok": False, "error": "Администратор не найден"}
    admin_level = ROLES.get(admin["role"], 0)
    if admin_level < ADMIN_MIN_LEVEL:
        conn.close()
        return {"ok": False, "error": "Недостаточно прав"}

    status = "closed" if close else "open"
    c.execute("""
        UPDATE complaints
        SET admin_response = ?, responded_by = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (response, admin_id, status, complaint_id))
    conn.commit()
    conn.close()
    return {"ok": True}


def get_complaint(complaint_id: int):
    conn = get_conn()
    c = conn.cursor()
    row = c.execute("""
        SELECT c.*,
               u1.username as reporter_username, u1.full_name as reporter_name,
               u2.username as responder_username, u2.full_name as responder_name, u2.role as responder_role
        FROM complaints c
        LEFT JOIN users u1 ON c.reporter_id = u1.telegram_id
        LEFT JOIN users u2 ON c.responded_by = u2.telegram_id
        WHERE c.id = ?
    """, (complaint_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
