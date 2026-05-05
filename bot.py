import asyncio
import os
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =================================================================
# [ КОНФИГУРАЦИЯ ]
# =================================================================
TOKEN = "8674226370:AAEvQovu_94ZJW8kSQIm07rFuHH16k2ex80"
ADMIN_ID = 6677277254
CHANNEL_ID = -1003837981813
CHANNEL_URL = "https://t.me/sdfsdfdsfdsfsdfsfc"
PRICE_PER_NUMBER = 6
PRIORITY_PRICE_PER_NUMBER = 5
HOLD_MINUTES = 20
QUEUE_TTL_HOURS = 8
PRODUCERS_PAGE_SIZE = 10
REPORTS_PAGE_SIZE = 12
DB_NAME = "titan_v40_final.db"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / DB_NAME
MENU_PHOTO = "https://imgur.gg/f/adSrGuP"
KZ_TZ = ZoneInfo("Asia/Almaty")
NIGHTLY_CLEANUP_HOUR = 0
NIGHTLY_CLEANUP_MINUTE = 0
REPORT_TIME_SHIFT_HOURS = 5

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =================================================================
# [ БАЗА ДАННЫХ ]
# =================================================================
class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.create_tables()
        self.ensure_columns()

    def create_tables(self):
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, banned INTEGER DEFAULT 0)"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, number TEXT, status TEXT DEFAULT 'waiting')"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, user_id INTEGER, start_time TIMESTAMP, status TEXT, paid INTEGER DEFAULT 0)"
            )              
            self.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, number TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS referrals ("
                "inviter_id INTEGER, "
                "invited_id INTEGER UNIQUE, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            )

    def query(self, sql, params=(), fetch="all"):
        with self.conn:
            cur = self.conn.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            return cur.fetchall()

    def ensure_columns(self):
        existing = {row[1] for row in self.query("PRAGMA table_info(sessions)")}
        if "group_id" not in existing:
            self.query("ALTER TABLE sessions ADD COLUMN group_id INTEGER")
        if "end_time" not in existing:
            self.query("ALTER TABLE sessions ADD COLUMN end_time TIMESTAMP")
        if "price" not in existing:
            self.query("ALTER TABLE sessions ADD COLUMN price REAL")
            self.query("UPDATE sessions SET price=? WHERE price IS NULL", (PRICE_PER_NUMBER,))

        self.query(
            "CREATE TABLE IF NOT EXISTS breaks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id INTEGER, "
            "start_time TIMESTAMP, "
            "end_time TIMESTAMP"
            ")"
        )
        user_columns = {row[1] for row in self.query("PRAGMA table_info(users)")}
        if "priority" not in user_columns:
            self.query("ALTER TABLE users ADD COLUMN priority INTEGER DEFAULT 0")
        if "is_admin" not in user_columns:
            self.query("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        queue_columns = {row[1] for row in self.query("PRAGMA table_info(queue)")}
        if "created_at" not in queue_columns:
            self.query("ALTER TABLE queue ADD COLUMN created_at TIMESTAMP")
            self.query("UPDATE queue SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        if "proc_by" not in queue_columns:
            self.query("ALTER TABLE queue ADD COLUMN proc_by INTEGER")
        if "code_sender_id" not in queue_columns:
            self.query("ALTER TABLE queue ADD COLUMN code_sender_id INTEGER")
        if "repeat_requested" not in queue_columns:
            self.query("ALTER TABLE queue ADD COLUMN repeat_requested INTEGER DEFAULT 0")
        if "qr_requested" not in queue_columns:
            self.query("ALTER TABLE queue ADD COLUMN qr_requested INTEGER DEFAULT 0")

        if "proc_by" not in existing:
            self.query("ALTER TABLE sessions ADD COLUMN proc_by INTEGER")
        if "code_sender_id" not in existing:
            self.query("ALTER TABLE sessions ADD COLUMN code_sender_id INTEGER")


def get_users_count(db_path: Path) -> int:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if not table:
                return 0
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def resolve_db_path() -> Path:
    env_path = os.getenv("TITAN_DB_PATH")
    if env_path:
        forced = Path(env_path).expanduser().resolve()
        if forced.exists():
            return forced
        logging.warning("TITAN_DB_PATH points to missing file: %s", forced)

    candidates: list[Path] = []

    for p in (DB_PATH, Path.cwd() / DB_NAME):
        if p not in candidates:
            candidates.append(p)

    scan_roots = [BASE_DIR.parent, Path.cwd(), Path.home(), Path("/workspace"), Path("/root")]
    for root in scan_roots:
        if not root.exists() or not root.is_dir():
            continue

        try:
            for found in root.rglob(DB_NAME):
                rp = found.resolve()
                if rp not in candidates:
                    candidates.append(rp)
        except Exception:
            pass

        try:
            for found in root.rglob("*.db"):
                rp = found.resolve()
                if rp not in candidates:
                    candidates.append(rp)
        except Exception:
            pass

    existing: list[tuple[int, Path]] = []
    for path in candidates:
        try:
            if path.exists():
                count = get_users_count(path)
                if count > 0:
                    existing.append((count, path))
        except Exception:
            continue

    if existing:
        existing.sort(key=lambda item: item[0], reverse=True)
        top_users, top_path = existing[0]
        logging.info("DB candidates found (users>0): %s", ", ".join(f"{p}:{c}" for c, p in existing[:10]))
        logging.info("Selected DB with max users (%s): %s", top_users, top_path)
        return top_path

    logging.warning("No DB with users found; using default path: %s", DB_PATH)
    return DB_PATH


RESOLVED_DB_PATH = resolve_db_path()
logging.info("Using database file: %s", RESOLVED_DB_PATH)

db = Database(str(RESOLVED_DB_PATH))


class Form(StatesGroup):
    num = State()


class AdminForm(StatesGroup):
    priority_add = State()
    priority_remove = State()
    message_user = State()
    breaks_group = State()
    broadcast = State()
    admin_add = State()
    admin_remove = State()


# =================================================================
# [ ФУНКЦИИ И МЕНЮ ]
# =================================================================
async def check_sub(user_id: int):
    if is_user_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


def parse_numbers(text: str):
    numbers = re.findall(r"\d{11,12}", text or "")
    return [f"+{n}" for n in numbers]


def unique_numbers(numbers: list[str]) -> list[str]:
    seen = set()
    unique = []
    for num in numbers:
        if num not in seen:
            seen.add(num)
            unique.append(num)
    return unique


def parse_dt(value):
    if isinstance(value, datetime):
        return value
    if value is None:
        return now_kz_naive()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return now_kz_naive()


def get_break_minutes(group_id: int | None, start_time: datetime, end_time: datetime) -> int:
    if not group_id:
        return 0
    rows = db.query(
        "SELECT start_time, end_time FROM breaks "
        "WHERE group_id=? AND end_time > ? AND start_time < ?",
        (group_id, start_time.strftime("%Y-%m-%d %H:%M:%S"), end_time.strftime("%Y-%m-%d %H:%M:%S")),
    )

    unique_rows = []
    seen = set()
    for b_start, b_end in rows:
        key = (parse_dt(b_start), parse_dt(b_end))
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append((b_start, b_end))

    minutes = 0
    for b_start, b_end in unique_rows:
        b_start_dt = parse_dt(b_start)
        b_end_dt = parse_dt(b_end)
        overlap_start = max(start_time, b_start_dt)
        overlap_end = min(end_time, b_end_dt)
        if overlap_end > overlap_start:
            minutes += int((overlap_end - overlap_start).total_seconds() // 60)
    return minutes


def effective_minutes(group_id: int | None, start_time: datetime, end_time: datetime) -> int:
    total = int((end_time - start_time).total_seconds() // 60)
    return max(0, total - get_break_minutes(group_id, start_time, end_time))


def mark_overdue_paid():
    return []


def get_user_balance(user_id: int):
    rows = db.query(
        "SELECT status, paid, price, start_time, end_time, group_id "
        "FROM sessions WHERE user_id=? AND status='slet'",
        (user_id,),
    )
    total = 0.0
    for status, paid, price, start_time, end_time, group_id in rows:
        if status != "slet" or not start_time or not end_time:
            continue
        start_dt = parse_dt(start_time)
        end_dt = parse_dt(end_time)
        worked = effective_minutes(group_id, start_dt, end_dt)
        if worked >= HOLD_MINUTES:
            total += price or PRICE_PER_NUMBER
    return total


def format_status(status: str | None) -> str:
    status_map = {
        "waiting": "в очереди",
        "proc": "в работе",
        "vstal": "стоит",
        "paid": "отстоял",
        "slet": "слетел",
        "otvyaz": "отвяз",
        "error": "ошибка",
    }
    return status_map.get(status or "", status or "в очереди")


def now_kz_naive() -> datetime:
    return datetime.now(KZ_TZ).replace(tzinfo=None)


def format_work_status(status: str, worked: int, paid: int) -> str:
    if status == "otvyaz":
        return "отвяз"
    if status == "slet" and (paid or worked >= HOLD_MINUTES):
        return "отстоял / слетел"
    return format_status(status)


def to_report_display_time(dt: datetime) -> datetime:
    return dt + timedelta(hours=REPORT_TIME_SHIFT_HOURS)


def get_vstal_contest_start(now: datetime) -> datetime:
    """Weekly TOP-vstal counter start: Monday 09:00 (KZ)."""
    week_start = now - timedelta(days=now.weekday())
    return week_start.replace(hour=9, minute=0, second=0, microsecond=0)


def get_next_monday_9(now: datetime) -> datetime:
    """Next contest launch point: upcoming Monday 09:00 (KZ)."""
    this_monday_9 = get_vstal_contest_start(now)
    if now < this_monday_9:
        return this_monday_9
    return this_monday_9 + timedelta(days=7)


def get_group_binding_key(chat_id: int, thread_id: int | None) -> str:
    return f"gid:{chat_id}:{thread_id or 0}"


def get_thread_id(message_obj) -> int:
    return int(getattr(message_obj, "message_thread_id", 0) or 0)


def get_linked_groups():
    rows = db.query("SELECT key, value FROM settings WHERE key LIKE 'gid:%'")
    groups = []
    for key, value in rows:
        parts = key.split(":")
        if len(parts) < 3:
            continue
        try:
            gid = int(parts[1])
            thread_id = int(parts[2])
        except ValueError:
            continue
        groups.append((gid, thread_id, value or ""))
    groups.sort(key=lambda item: (item[0], item[1]))
    return groups


def get_office_label_for_group(group_id: int | None) -> str:
    gids = sorted({gid for gid, _thread, _title in get_linked_groups()})
    for idx, gid in enumerate(gids, 1):
        if gid == group_id:
            return f"Офис {idx}"
    return "Офис 1"


def can_manage_number(user_id: int, owner_id: int, number: str) -> bool:
    if is_user_admin(user_id):
        return True

    q_row = db.query(
        "SELECT proc_by FROM queue "
        "WHERE number=? AND user_id=? AND status='proc' "
        "ORDER BY id DESC LIMIT 1",
        (number, owner_id),
        fetch="one",
    )
    if q_row:
        return q_row[0] == user_id

    s_row = db.query(
        "SELECT proc_by FROM sessions "
        "WHERE number=? AND user_id=? "
        "ORDER BY id DESC LIMIT 1",
        (number, owner_id),
        fetch="one",
    )
    if s_row:
        return s_row[0] == user_id

    return False


def is_group_linked(chat_id: int, thread_id: int | None) -> bool:
    tid = int(thread_id or 0)
    if tid <= 0:
        return False
    key = get_group_binding_key(chat_id, tid)
    return bool(db.query("SELECT 1 FROM settings WHERE key=?", (key,), fetch="one"))


def is_user_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    row = db.query("SELECT is_admin FROM users WHERE user_id=?", (user_id,), fetch="one")
    return bool(row and row[0])


def is_user_banned(user_id: int) -> bool:
    row = db.query("SELECT banned FROM users WHERE user_id=?", (user_id,), fetch="one")
    return bool(row and row[0])


def is_user_priority(user_id: int) -> bool:
    row = db.query("SELECT priority FROM users WHERE user_id=?", (user_id,), fetch="one")
    return bool(row and row[0])


def get_user_price(user_id: int) -> float:
    return PRIORITY_PRICE_PER_NUMBER if is_user_priority(user_id) else PRICE_PER_NUMBER


def get_menu_caption(is_subscribed: bool) -> str:
    if not is_subscribed:
        return "⚠️ Подпишись на канал!"
    return "<b>⌚️ Репутация:</b> @GGGGG67123\n\n<b>🏠 Главное меню:</b>"


def cleanup_queue_expired():
    cutoff = (now_kz_naive() - timedelta(hours=QUEUE_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    db.query("DELETE FROM queue WHERE created_at IS NOT NULL AND created_at < ?", (cutoff,))


def cleanup_paid_reports():
    db.query("DELETE FROM sessions WHERE paid=1")


def cleanup_archives():
    db.query("DELETE FROM submissions")


def cleanup_processed_queue():
    db.query("DELETE FROM queue WHERE status!='waiting'")


def build_producers_page(page: int):
    total = db.query("SELECT COUNT(*) FROM users", fetch="one")[0]
    max_page = max(1, (total + PRODUCERS_PAGE_SIZE - 1) // PRODUCERS_PAGE_SIZE)
    page = max(1, min(page, max_page))
    offset = (page - 1) * PRODUCERS_PAGE_SIZE
    rows = db.query(
        "SELECT user_id, username, banned FROM users ORDER BY user_id DESC LIMIT ? OFFSET ?",
        (PRODUCERS_PAGE_SIZE, offset),
    )
    return rows, page, max_page


def parse_break_lines(text: str):
    entries = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "-" not in line:
            continue
        start_str, end_str = [part.strip() for part in line.split("-", 1)]
        try:
            start_time = datetime.strptime(start_str, "%H:%M")
            end_time = datetime.strptime(end_str, "%H:%M")
        except ValueError:
            continue
        entries.append((start_time, end_time))
    return entries


def unique_break_rows(rows):
    seen = set()
    unique = []
    for start_time, end_time in rows:
        start_label = parse_dt(start_time).strftime("%H:%M")
        end_label = parse_dt(end_time).strftime("%H:%M")
        key = (start_label, end_label)
        if key in seen:
            continue
        seen.add(key)
        unique.append((start_label, end_label))
    return unique

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

async def get_main_menu_kb(user_id: int):
    kb = InlineKeyboardBuilder()

    if not await check_sub(user_id):
        kb.row(
            InlineKeyboardButton(text="📢 Подписаться на канал", callback_data="subscribe")
        )
        kb.row(
            InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")
        )
    else:
        kb.row(
            InlineKeyboardButton(text="☎️ Сдать заявку", callback_data="submit_request")
        )
        kb.row(
            InlineKeyboardButton(text="💾 Архив", callback_data="archive"),
            InlineKeyboardButton(text="🎯 Переводы", callback_data="transfer")
        )
        kb.row(
            InlineKeyboardButton(text="💻 Техподдержка", callback_data="support")
        )

        if is_user_admin(user_id):
            kb.row(
                InlineKeyboardButton(text="⚙️ Админ", callback_data="admin_menu")
            )

    return kb.as_markup()

def build_user_queue_view(uid: int):
    rows = db.query(
        "SELECT id, number FROM queue WHERE user_id=? AND status='waiting' ORDER BY id ASC",
        (uid,),
    )
    total_waiting = db.query("SELECT COUNT(*) FROM queue WHERE status='waiting'", fetch="one")[0]
    user_priority = 1 if is_user_priority(uid) else 0
    kb = InlineKeyboardBuilder()

    lines = ["<b>📋 Ваши номера в очереди</b>", f"Всего номеров в общей очереди: <b>{total_waiting}</b>", ""]
    if not rows:
        lines.append("Номеров в очереди нет.")
    else:
        for q_id, number in rows:
            q_pos = db.query(
                "SELECT COUNT(*) FROM queue q "
                "JOIN users u ON q.user_id=u.user_id "
                "WHERE q.status='waiting' "
                "AND (u.priority > ? OR (u.priority = ? AND q.id <= ?))",
                (user_priority, user_priority, q_id),
                fetch="one",
            )[0]
            lines.append(f"• <code>{number}</code> — очередь: {q_pos}")
            kb.row(types.InlineKeyboardButton(text=f"{number} • очередь {q_pos}", callback_data=f"u_q_del_{q_id}"))

    kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="u_back"))
    return "\n".join(lines), kb.as_markup()


def build_admin_reports_view(page: int):
    today_str = now_kz_naive().strftime("%Y-%m-%d")
    source_rows = db.query(
        "SELECT u.username, s.user_id, s.number, s.start_time, s.end_time, s.group_id, s.price, s.status "
        "FROM sessions s LEFT JOIN users u ON s.user_id=u.user_id "
        "WHERE date(COALESCE(s.end_time, s.start_time))=? AND s.status='slet' "
        "ORDER BY s.id DESC",
        (today_str,),
    )
    total_paid = db.query("SELECT COALESCE(SUM(price),0) FROM sessions WHERE status='slet' AND paid=1", fetch="one")[0]

    rows = []
    now = now_kz_naive()
    for row in source_rows:
        _name, _u_id, _number, start_time, end_time, group_id, _price, _rep_status = row
        start_dt = parse_dt(start_time)
        end_dt = parse_dt(end_time) if end_time else now
        worked = effective_minutes(group_id, start_dt, end_dt)
        if worked >= HOLD_MINUTES:
            rows.append(row)

    total_today = sum((row[6] or PRICE_PER_NUMBER) for row in rows)

    total_rows = len(rows)
    max_page = max(1, (total_rows + REPORTS_PAGE_SIZE - 1) // REPORTS_PAGE_SIZE)
    page = max(1, min(page, max_page))
    start = (page - 1) * REPORTS_PAGE_SIZE
    end = start + REPORTS_PAGE_SIZE
    page_rows = rows[start:end]

    header_lines = [
        f"<b>🧾 Автоотчёт за сегодня (KZ) • страница {page}/{max_page}</b>",
        f"<b>💰 Баланс (всё время):</b> {total_paid}$",
        f"<b>💵 За сегодня:</b> {total_today}$",
        "",
        "Номинатор | Номер | Минуты | Сумма",
        "",
    ]
    lines = list(header_lines)
    total = 0.0
    if not page_rows:
        lines.append("Пока пусто")
    else:
        for name, u_id, number, start_time, end_time, group_id, price, rep_status in page_rows:
            label = f"@{name}" if name else f"ID:{u_id}"
            start_dt = parse_dt(start_time)
            end_dt = parse_dt(end_time) if end_time else now_kz_naive()
            worked = effective_minutes(group_id, start_dt, end_dt)
            amount = price or PRICE_PER_NUMBER
            total += amount
            row_line = f"• {label} | <code>{number}</code> | {worked} мин | {amount}$"
            lines.append(row_line)
        lines.append("")
        lines.append(f"<b>ИТОГО ПО СТРАНИЦЕ: {total}$</b>")

    caption_text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 1:
        nav.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"adm_reports_page_{page - 1}"))
    if page < max_page:
        nav.append(types.InlineKeyboardButton(text="➡️", callback_data=f"adm_reports_page_{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
    return caption_text, kb.as_markup()


async def render_report_message(call: CallbackQuery, caption_text: str, markup):
    try:
        await call.message.edit_caption(
            caption=caption_text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        return
    except Exception:
        pass

    try:
        await call.message.edit_text(
            text=caption_text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        return
    except Exception:
        pass

    # Не создаем лишнее второе сообщение, если редактирование не удалось.
    try:
        await call.answer("⚠️ Не удалось обновить сообщение", show_alert=False)
    except Exception:
        pass


async def render_action_message(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        return
    except Exception:
        pass
    try:
        await call.message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
        return
    except Exception:
        pass
    # Не создаем второе сообщение при клике по кнопкам.
    try:
        await call.answer("⚠️ Не удалось обновить сообщение", show_alert=False)
    except Exception:
        pass


def back_kb(target: str = "u_back"):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data=target)
    return kb.as_markup()


@dp.callback_query()
async def cb_handler(call: CallbackQuery, state: FSMContext):
    uid, data = call.from_user.id, call.data

    if data in {"refresh_menu", "u_back", "u_menu"}:
        await state.clear()
        cap = get_menu_caption(await check_sub(uid))
        try:
            await call.message.edit_caption(
                caption=cap,
                reply_markup=await get_main_menu_kb(uid),
                parse_mode="HTML",
            )
        except Exception:
            await call.message.answer_photo(
                photo=MENU_PHOTO,
                caption=cap,
                reply_markup=await get_main_menu_kb(uid),
                parse_mode="HTML",
            )

    elif data == "u_yield":
        user_price = get_user_price(uid)
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text=f"{user_price}$ 15м + 5м грев", callback_data="u_yield_confirm"))
        await call.message.answer(
            "☎️ Выберите тариф:\n\n"
            "<b>📌 Введите номер в формате:</b>\n"
            "<code>+77777777777</code>\n"
            "<code>77777777777</code>\n"
            "<code>877777777777</code>\n\n"
            "<b>⚠️ ОТВЯЗ -ВЫПЛАТА.</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    elif data == "u_yield_confirm":
        await state.set_state(Form.num)
        await call.message.answer("📞 Введите номера (каждый с новой строки или через пробел):")

    elif data == "u_q":
        caption, markup = build_user_queue_view(uid)
        await call.message.edit_caption(caption=caption, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("u_q_del_"):
        q_id = int(data.replace("u_q_del_", ""))
        db.query("DELETE FROM queue WHERE id=? AND user_id=? AND status='waiting'", (q_id, uid))
        caption, markup = build_user_queue_view(uid)
        await call.message.edit_caption(caption=caption, reply_markup=markup, parse_mode="HTML")
        await call.answer("✅ Номер удалён из очереди", show_alert=False)
        return

    elif data == "u_my_numbers":
            balance = get_user_balance(uid)
            rows = db.query(
                "SELECT number, status, paid, start_time, end_time, group_id "
                "FROM sessions "
                "WHERE user_id=? AND status='slet' "
                "ORDER BY id DESC LIMIT 300",
                (uid,),
            )

            lines = [
                f"<b>💰 Баланс:</b> <b>{balance}$</b>",
                "<b>📱 Мои номера:</b>",
                "",
            ]

            if not rows:
                lines.append("Номеров пока нет")
            else:
                now = now_kz_naive()
                for number, status, paid, start_time, end_time, group_id in rows:
                    start_dt = parse_dt(start_time)
                    end_dt = parse_dt(end_time) if end_time else now
                    worked = effective_minutes(group_id, start_dt, end_dt)

                    status_label = "отстоял / слетел" if (paid or worked >= HOLD_MINUTES) else "слетел"
                    lines.append(f"• <code>{number}</code> — {status_label} • {worked} мин")

            await call.message.edit_caption(
                caption="\n".join(lines),
                reply_markup=back_kb(),
                parse_mode="HTML",
            )
    

    elif data == "u_breaks":
        today_label = now_kz_naive().strftime("%Y-%m-%d")
        linked_groups = sorted({gid for gid, _thread, _title in get_linked_groups()})
        lines = ["<b>🪩 Перерывы</b>", ""]
        if not linked_groups:
            lines.append("Активных перерывов нет.")
        else:
            placeholders = ",".join("?" for _ in linked_groups)
            rows = db.query(
                f"SELECT group_id, start_time, end_time FROM breaks "
                f"WHERE group_id IN ({placeholders}) AND date(start_time)=? "
                f"ORDER BY group_id ASC, start_time DESC LIMIT 200",
                tuple(linked_groups) + (today_label,),
            )
            if not rows:
                lines.append("Активных перерывов нет.")
            else:
                office_by_gid = {gid: f"Офис {idx}" for idx, gid in enumerate(linked_groups, 1)}
                grouped = {}
                for group_id, start_time, end_time in rows:
                    label = office_by_gid.get(group_id, "Офис 1")
                    grouped.setdefault(label, []).append((start_time, end_time))

                for label, entries in grouped.items():
                    lines.append(f"<b>{label}</b>")
                    for start_label, end_label in unique_break_rows(entries):
                        lines.append(f"• {start_label}–{end_label}")
                    lines.append("")
        await call.message.edit_caption(caption="\n".join(lines), reply_markup=back_kb(), parse_mode="HTML")


    elif data == "adm_main":
        if not is_user_admin(uid):
            return
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="✅ Топ встал", callback_data="adm_top_vstal"))
        kb.row(
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
            types.InlineKeyboardButton(text="🧾 Автоотчёт", callback_data="adm_reports"),
        )
        kb.row(types.InlineKeyboardButton(text="📣 Рассылка", callback_data="adm_broadcast"))
        kb.row(types.InlineKeyboardButton(text="🧹 Очистка очереди", callback_data="adm_clear"))
        kb.row(
            types.InlineKeyboardButton(text="🏢 Группы", callback_data="adm_groups"),
            types.InlineKeyboardButton(text="👤 Производители", callback_data="adm_users"),
        )
        kb.row(types.InlineKeyboardButton(text="⭐️ Приоритеты", callback_data="adm_priorities"))
        kb.row(types.InlineKeyboardButton(text="👮 Админы", callback_data="adm_admins"))
        kb.row(types.InlineKeyboardButton(text="🍽 Перерывы", callback_data="adm_breaks"))
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="u_back"))
        await call.message.edit_caption(
            caption="🛡 <b>Админ-панель</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data == "adm_clear":
        if not is_user_admin(uid):
            return
        db.query("DELETE FROM queue")
        await call.answer("✅ Очередь очищена", show_alert=True)

    elif data == "adm_stats":
        if not is_user_admin(uid):
            return
        users = db.query("SELECT COUNT(*) FROM users", fetch="one")[0]
        paid = db.query("SELECT COUNT(*) FROM sessions WHERE status='slet' AND paid=1", fetch="one")[0]
        paid_amount = db.query("SELECT COALESCE(SUM(price),0) FROM sessions WHERE status='slet' AND paid=1", fetch="one")[0]
        active = db.query("SELECT COUNT(*) FROM sessions WHERE status='vstal' AND paid=0", fetch="one")[0]
        submitted = db.query("SELECT COUNT(*) FROM submissions", fetch="one")[0]
        txt = (
            "<b>📊 Статистика</b>\n\n"
            f"Пользователей: <b>{users}</b>\n"
        )
        await call.message.edit_caption(caption=txt, reply_markup=back_kb("adm_main"), parse_mode="HTML")

    elif data == "adm_top_vstal":
        if not is_user_admin(uid):
            return

        tops = db.query(
            "SELECT u.username, u.user_id, COUNT(s.id) "
            "FROM sessions s JOIN users u ON s.user_id=u.user_id "
            "WHERE s.status IN ('vstal','paid','slet') "
            "GROUP BY u.user_id ORDER BY COUNT(s.id) DESC LIMIT 30",
        )

        lines = ["<b>✅ Топ встал номеров (ТОП-30) (27.02.26)</b>", ""]
        if not tops:
            lines.append("Пока пусто")
        else:
            for i, (name, u_id, cnt) in enumerate(tops, 1):
                label = f"@{name}" if name else f"ID:{u_id}"
                lines.append(f"{i}. {label} — {cnt} шт.")
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=back_kb("adm_main"),
            parse_mode="HTML",
        )

    elif data == "adm_reports" or data.startswith("adm_reports_page_"):
        if not is_user_admin(uid):
            return
        page = 1
        if data.startswith("adm_reports_page_"):
            try:
                page = int(data.rsplit("_", 1)[1])
            except ValueError:
                page = 1
        caption_text, markup = build_admin_reports_view(page)
        await render_report_message(call, caption_text, markup)

    elif data == "adm_broadcast":
        if not is_user_admin(uid):
            return
        await state.set_state(AdminForm.broadcast)
        await call.message.answer("Введите текст рассылки для всех пользователей:")

    elif data == "adm_groups":
        if not is_user_admin(uid):
            return
        groups = get_linked_groups()
        kb = InlineKeyboardBuilder()
        lines = ["<b>🏢 Привязанные топики</b>", ""]
        if not groups:
            lines.append("Топики не привязаны.")
        else:
            for gid, thread_id, title in groups:
                label = title or f"Группа {gid}"
                kb.row(
                    types.InlineKeyboardButton(
                        text=f"❌ {label} • топик {thread_id}",
                        callback_data=f"adm_group_unlink_{gid}_{thread_id}",
                    )
                )
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data == "adm_breaks":
        if not is_user_admin(uid):
            return
        groups = get_linked_groups()
        kb = InlineKeyboardBuilder()
        lines = ["<b>🍽 Перерывы по группам</b>", ""]
        if not groups:
            lines.append("Топики не привязаны.")
        else:
            for gid, thread_id, title in groups:
                label = title or f"Группа {gid}"
                kb.row(
                    types.InlineKeyboardButton(
                        text=label,
                        callback_data=f"adm_breaks_group_{gid}",
                    )
                )
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data.startswith("adm_breaks_group_"):
        if not is_user_admin(uid):
            return
        payload = data.replace("adm_breaks_group_", "")
        gid = int(payload)
        await state.set_state(AdminForm.breaks_group)
        await state.update_data(break_group_id=gid)
        today_label = now_kz_naive().strftime("%Y-%m-%d")
        rows = db.query(
            "SELECT start_time, end_time FROM breaks WHERE group_id=? AND date(start_time)=? ORDER BY start_time ASC",
            (gid, today_label),
        )
        group_title = db.query("SELECT value FROM settings WHERE key LIKE ? LIMIT 1", (f"gid:{gid}:%",), fetch="one")
        group_name = group_title[0] if group_title and group_title[0] else f"Группа {gid}"
        lines = [
            f"<b>🍽 Перерывы {group_name}</b>",
            "Вводите время строками, например:",
            "11:00 - 12:00",
            "14:00 - 14:30",
            "15:00 - 15:15",
            "",
        ]
        if not rows:
            lines.append("Перерывов нет.")
        else:
            for start_time, end_time in rows:
                start_dt = parse_dt(start_time)
                end_dt = parse_dt(end_time)
                lines.append(f"• {start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}")
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_breaks"))
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data.startswith("adm_group_unlink_"):
        if not is_user_admin(uid):
            return
        payload = data.replace("adm_group_unlink_", "")
        gid_part, thread_part = payload.split("_", 1)
        gid = int(gid_part)
        thread_id = int(thread_part)
        db.query("DELETE FROM settings WHERE key=?", (get_group_binding_key(gid, thread_id),))
        await call.answer("✅ Топик отвязан", show_alert=True)

    elif data == "adm_users" or data.startswith("adm_users_page_"):
        if not is_user_admin(uid):
            return
        page = 1
        if data.startswith("adm_users_page_"):
            page = int(data.replace("adm_users_page_", ""))
        rows, page, max_page = build_producers_page(page)
        kb = InlineKeyboardBuilder()
        lines = [
            f"<b>👤 Пользователи (страница {page}/{max_page})</b>",
            "Нажмите на пользователя, чтобы открыть действия.",
            "",
        ]
        if not rows:
            lines.append("Пока пусто.")
        else:
            for user_id, username, banned in rows:
                label = f"@{username}" if username else f"ID:{user_id}"
                status = "🚫" if banned else "✅"
                lines.append(f"{status} {label}")
                kb.row(
                    types.InlineKeyboardButton(
                        text=label,
                        callback_data=f"adm_user_menu_{user_id}_{page}",
                    )
                )
        nav = []
        if page > 1:
            nav.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_users_page_{page - 1}"))
        if page < max_page:
            nav.append(types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"adm_users_page_{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data.startswith("adm_user_menu_"):
        if not is_user_admin(uid):
            return
        parts = data.replace("adm_user_menu_", "").split("_")
        target_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 1
        row = db.query("SELECT username, banned FROM users WHERE user_id=?", (target_id,), fetch="one")
        if row is None:
            return
        username, banned = row
        label = f"@{username}" if username else f"ID:{target_id}"
        kb = InlineKeyboardBuilder()
        kb.row(
            types.InlineKeyboardButton(text="🚫 Бан", callback_data=f"adm_user_ban_{target_id}"),
            types.InlineKeyboardButton(text="✅ Разбан", callback_data=f"adm_user_unban_{target_id}"),
        )
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_users_page_{page}"))
        status = "🚫" if banned else "✅"
        await call.message.edit_caption(
            caption=f"<b>Пользователь:</b> {label}\nСтатус: {status}",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data == "adm_priorities":
        if not is_user_admin(uid):
            return
        rows = db.query("SELECT user_id, username, priority FROM users WHERE priority=1 ORDER BY user_id DESC LIMIT 20")
        kb = InlineKeyboardBuilder()
        lines = ["<b>⭐️ Приоритеты</b>", "Список приоритетных пользователей:", ""]
        if not rows:
            lines.append("Пока пусто.")
        else:
            for user_id, username, priority in rows:
                label = f"@{username}" if username else f"ID:{user_id}"
                status = "⭐️" if priority else "•"
                lines.append(f"{status} {label}")
        kb.row(
            types.InlineKeyboardButton(text="Выдать приоритет", callback_data="adm_priority_add"),
            types.InlineKeyboardButton(text="Снять приоритет", callback_data="adm_priority_remove"),
        )
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
        await call.message.edit_caption(
            caption="\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    elif data.startswith("adm_user_toggle_"):
        if not is_user_admin(uid):
            return
        target_id = int(data.replace("adm_user_toggle_", ""))
        row = db.query("SELECT banned FROM users WHERE user_id=?", (target_id,), fetch="one")
        if row is None:
            return
        new_value = 0 if row[0] else 1
        db.query("UPDATE users SET banned=? WHERE user_id=?", (new_value, target_id))
        await call.answer("✅ Обновлено", show_alert=True)
    elif data.startswith("adm_user_ban_"):
        if not is_user_admin(uid):
            return
        target_id = int(data.replace("adm_user_ban_", ""))
        db.query("UPDATE users SET banned=1 WHERE user_id=?", (target_id,))
        await call.answer("✅ Забанен", show_alert=True)
    elif data.startswith("adm_user_unban_"):
        if not is_user_admin(uid):
            return
        target_id = int(data.replace("adm_user_unban_", ""))
        db.query("UPDATE users SET banned=0 WHERE user_id=?", (target_id,))
        await call.answer("✅ Разбанен", show_alert=True)
    elif data == "adm_priority_add":
        if not is_user_admin(uid):
            return
        await state.set_state(AdminForm.priority_add)
        await call.message.answer("Введите @username или ID для выдачи приоритета:")

    elif data == "adm_priority_remove":
        if not is_user_admin(uid):
            return
        await state.set_state(AdminForm.priority_remove)
        await call.message.answer("Введите @username или ID для снятия приоритета:")

    elif data == "adm_admins":
        if not is_user_admin(uid):
            return
        rows = db.query("SELECT user_id, username FROM users WHERE is_admin=1 ORDER BY user_id DESC LIMIT 30")
        lines = ["<b>👮 Админы бота</b>", ""]
        if not rows:
            lines.append("Список пуст")
        else:
            for user_id, username in rows:
                label = f"@{username}" if username else f"ID:{user_id}"
                lines.append(f"• {label}")
        kb = InlineKeyboardBuilder()
        kb.row(
            types.InlineKeyboardButton(text="➕ Добавить", callback_data="adm_admin_add"),
            types.InlineKeyboardButton(text="➖ Удалить", callback_data="adm_admin_remove"),
        )
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="adm_main"))
        await call.message.edit_caption(caption="\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")

    elif data == "adm_admin_add":
        if not is_user_admin(uid):
            return
        await state.set_state(AdminForm.admin_add)
        await call.message.answer("Введите @username или ID для добавления администратора:")

    elif data == "adm_admin_remove":
        if not is_user_admin(uid):
            return
        await state.set_state(AdminForm.admin_remove)
        await call.message.answer("Введите @username или ID для удаления администратора:")

    elif data == "u_next_num":
        thread_id = get_thread_id(call.message)
        if not is_group_linked(call.message.chat.id, thread_id):
            await call.answer()
            return
        result = await issue_next_number(call.message.chat.id, thread_id, uid)
        if not result:
            await call.answer("Очередь пуста", show_alert=False)
            return
        user_id, number = result
        next_kb = InlineKeyboardBuilder()
        next_kb.row(types.InlineKeyboardButton(text="⏭ Скип", callback_data=f"k_{user_id}_{number}"))
        await call.message.answer(
            f"Номер: <code>{number}</code>\n"
            "Отправьте фото в ответ на это сообщение.",
            parse_mode="HTML",
            reply_markup=next_kb.as_markup(),
        )
        try:
            office_label = get_office_label_for_group(call.message.chat.id)
            await bot.send_message(
                user_id,
                f"📨 Ваш номер {number} взяли. {office_label}. Ожидайте кода.",
            )
        except Exception:
            pass


    elif data.startswith(("rr_", "qr_")):
        parts = data.split("_", 4)
        if len(parts) < 5:
            try:
                await call.answer()
            except Exception:
                pass
            return
        action, chat_id, thread_id, worker_uid, number = parts
        chat_id = int(chat_id)
        thread_id = int(thread_id)
        worker_uid = int(worker_uid)
        flag_col = "repeat_requested" if action == "rr" else "qr_requested"
        row = db.query(f"SELECT {flag_col} FROM queue WHERE number=? AND user_id=? AND status='proc' ORDER BY id DESC LIMIT 1", (number, worker_uid), fetch="one")
        if not row:
            await call.answer("⚠️ Номер уже закрыт", show_alert=False)
            return
        if row[0]:
            await call.answer("⚠️ Уже запрашивали", show_alert=False)
            return
        db.query(f"UPDATE queue SET {flag_col}=1 WHERE number=? AND user_id=? AND status='proc'", (number, worker_uid))
        text = (
            f"🔁 По номеру {number} запросили повторный код.\nОтправьте фото в ответ на это сообщение."
            if action == "rr"
            else f"🧾 По номеру {number} запросили повторный QR-код.\nОтправьте фото в ответ на это сообщение."
        )
        try:
            if thread_id:
                await bot.send_message(chat_id, text, message_thread_id=thread_id)
            else:
                await bot.send_message(chat_id, text)
            await call.answer("✅ Запрос отправлен")
        except Exception:
            await call.answer("⚠️ Не удалось отправить запрос", show_alert=True)

    elif data.startswith(("v_", "s_", "e_", "m_", "k_", "d_")):
        act, v_uid, v_num = data.split("_", 2)
        v_uid = int(v_uid)
        if not can_manage_number(uid, v_uid, v_num):
            await call.answer("⛔ Нет доступа", show_alert=False)
            return

        if act == "v":
            group_id = call.message.chat.id if call.message and call.message.chat else None
            price = get_user_price(v_uid)
            queue_row = db.query(
                "SELECT proc_by, code_sender_id FROM queue WHERE number=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (v_num, v_uid),
                fetch="one",
            )
            proc_by = queue_row[0] if queue_row else None
            code_sender_id = queue_row[1] if queue_row else uid
            db.query(
                "INSERT INTO sessions (number, user_id, start_time, status, paid, group_id, price, proc_by, code_sender_id) "
                "VALUES (?, ?, ?, 'vstal', 0, ?, ?, ?, ?)",
                (v_num, v_uid, now_kz_naive().strftime("%Y-%m-%d %H:%M:%S"), group_id, price, proc_by, code_sender_id),
            )
            db.query("DELETE FROM queue WHERE number=? AND user_id=?", (v_num, v_uid))
            kb2 = InlineKeyboardBuilder()
            kb2.row(
                types.InlineKeyboardButton(text="🔴 Слет", callback_data=f"s_{v_uid}_{v_num}"),
                types.InlineKeyboardButton(text="🚫 Был отвяз", callback_data=f"d_{v_uid}_{v_num}"),
            )
            kb2.row(types.InlineKeyboardButton(text="💬 Сообщение", callback_data=f"m_{v_uid}_{v_num}"))
            kb2.row(types.InlineKeyboardButton(text="⏭ Вперёд", callback_data="u_next_num"))
            await render_action_message(call, f"✅ {v_num} — <b>Принят</b>", kb2.as_markup())
            try:
                await bot.send_message(v_uid, f"✅ Ваш номер {v_num} встал.")
            except Exception:
                pass

        elif act == "s":
            now = now_kz_naive()
            active = db.query(
                "SELECT id, start_time, group_id FROM sessions "
                "WHERE number=? AND user_id=? AND status='vstal' ORDER BY id DESC LIMIT 1",
                (v_num, v_uid),
                fetch="one",
            )
            paid = 0
            if active:
                sid, start_time, group_id = active
                worked = effective_minutes(group_id, parse_dt(start_time), now)
                paid = 1 if worked >= HOLD_MINUTES else 0
                db.query(
                    "UPDATE sessions SET status='slet', paid=?, end_time=? WHERE id=?",
                    (paid, now.strftime("%Y-%m-%d %H:%M:%S"), sid),
                )
            else:
                await call.answer("⚠️ Активная сессия уже закрыта", show_alert=False)
                return
            await render_action_message(call, f"🔴 {v_num} — <b>Слетел</b>")
            try:
                if paid:
                    await bot.send_message(v_uid, f"✅ Номер {v_num} отстоял 20+ мин и отмечен как слетел.")
                else:
                    await bot.send_message(v_uid, f"🔴 Номер {v_num} слетел.")
            except Exception:
                pass

        elif act == "m":
            await state.set_state(AdminForm.message_user)
            await state.update_data(target_user_id=v_uid, target_number=v_num)
            await call.message.answer("Введите сообщение пользователю по номеру:")

        elif act == "k":
            active_q = db.query(
                "SELECT id FROM queue WHERE number=? AND user_id=? AND status='proc' ORDER BY id DESC LIMIT 1",
                (v_num, v_uid),
                fetch="one",
            )
            if not active_q:
                await call.answer("⚠️ Номер уже не в обработке", show_alert=False)
                return
            db.query(
                "UPDATE queue SET status='waiting', proc_by=NULL, code_sender_id=NULL, repeat_requested=0, qr_requested=0 WHERE id=?",
                (active_q[0],),
            )
            await render_action_message(call, f"⏭ {v_num} — <b>был возвращен в очередь</b>")
            sent = False
            try:
                await bot.send_message(v_uid, f"ℹ️ Номер {v_num} пропущен и возвращен в очередь.")
                sent = True
            except Exception:
                pass
            if not sent:
                await call.message.answer(f"⚠️ Не удалось отправить ЛС пользователю по номеру {v_num}.")

        elif act == "d":
            now = now_kz_naive().strftime("%Y-%m-%d %H:%M:%S")
            sid = db.query(
                "SELECT id FROM sessions WHERE number=? AND user_id=? AND status='vstal' ORDER BY id DESC LIMIT 1",
                (v_num, v_uid),
                fetch="one",
            )
            if not sid:
                await call.answer("⚠️ Активная сессия уже закрыта", show_alert=False)
                return
            db.query("UPDATE sessions SET status='otvyaz', paid=0, end_time=? WHERE id=?", (now, sid[0]))
            await render_action_message(call, f"🚫 {v_num} — <b>Был отвяз</b>")
            sent = False
            try:
                await bot.send_message(v_uid, f"🚫 Номер {v_num} отмечен как отвяз.")
                sent = True
            except Exception:
                pass
            if not sent:
                await call.answer("⚠️ Не удалось отправить уведомление пользователю", show_alert=False)
                await call.message.answer(f"⚠️ Не удалось отправить ЛС пользователю по номеру {v_num}.")

        elif act == "e":
            db.query("UPDATE queue SET status='error' WHERE number=?", (v_num,))
            await render_action_message(call, f"⚠️ {v_num} — <b>Ошибка</b>")
            try:
                await bot.send_message(v_uid, f"⚠️ По вашему номеру {v_num} отмечена ошибка.")
            except Exception:
                pass

    await call.answer()


@dp.message(CommandStart())
async def start(message: types.Message):
    db.query(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (message.from_user.id, message.from_user.username),
    )

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        ref_id_raw = parts[1].replace("ref_", "", 1)
        if ref_id_raw.isdigit():
            inviter_id = int(ref_id_raw)
            if inviter_id != message.from_user.id:
                db.query(
                    "INSERT OR IGNORE INTO referrals (inviter_id, invited_id) VALUES (?, ?)",
                    (inviter_id, message.from_user.id),
                )

    await message.answer_photo(
        photo=MENU_PHOTO,
        caption=get_menu_caption(await check_sub(message.from_user.id)),
        reply_markup=await get_main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(Command("set"))
async def set_group(message: types.Message):
    if not is_user_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа. Команда доступна только админам.")
    if message.chat.type == "private":
        return await message.answer("❌ Введите это в группе!")

    thread_id = get_thread_id(message)
    if thread_id <= 0:
        return await message.answer("⚠️ Используйте /set внутри НУЖНОГО топика (темы).")

    key = get_group_binding_key(message.chat.id, thread_id)
    gid = db.query("SELECT value FROM settings WHERE key=?", (key,), fetch="one")
    if gid:
        db.query("DELETE FROM settings WHERE key=?", (key,))
        await message.answer("🔓 Топик отвязан (только эта тема).")
    else:
        group_title = message.chat.title or "Группа"
        db.query("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, group_title))
        await message.answer("🔒 Топик привязан (работает только в этой теме).")


@dp.message(Command("dbinfo"))
async def db_info(message: types.Message):
    if not is_user_admin(message.from_user.id):
        return
    users_count = db.query("SELECT COUNT(*) FROM users", fetch="one")[0]
    await message.answer(
        "🗄 <b>DB INFO</b>\n"
        f"Путь: <code>{RESOLVED_DB_PATH}</code>\n"
        f"Пользователей: <b>{users_count}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("break"))
async def set_break(message: types.Message):
    if not is_user_admin(message.from_user.id):
        return
    if message.chat.type == "private":
        return await message.answer("❌ Введите это в группе!")
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.answer("⚠️ Используйте: /break 30, /break 0 или /break 12:00 12:30")
    start = None
    end = None
    if len(parts) == 2 and re.fullmatch(r"-?\d+", parts[1]):
        minutes = int(parts[1])
        if minutes <= 0:
            db.query("DELETE FROM breaks WHERE group_id=?", (message.chat.id,))
            return await message.answer("🧹 Перерывы для группы очищены.")
        start = now_kz_naive()
        end = start + timedelta(minutes=minutes)
    elif len(parts) == 3:
        try:
            now = now_kz_naive()
            start = datetime.strptime(parts[1], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
            end = datetime.strptime(parts[2], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
        except ValueError:
            return await message.answer("⚠️ Формат времени: /break 12:00 12:30")
        if end <= start:
            return await message.answer("⚠️ Конец перерыва должен быть позже начала.")
    else:
        return await message.answer("⚠️ Используйте: /break 30, /break 0 или /break 12:00 12:30")
    start_db = start.strftime("%Y-%m-%d %H:%M:%S")
    end_db = end.strftime("%Y-%m-%d %H:%M:%S")
    existing = db.query(
        "SELECT 1 FROM breaks WHERE group_id=? AND start_time=? AND end_time=?",
        (message.chat.id, start_db, end_db),
        fetch="one",
    )
    if existing:
        return await message.answer(f"ℹ️ Такой перерыв уже есть ({start.strftime('%H:%M')}–{end.strftime('%H:%M')}).")
    db.query(
        "INSERT INTO breaks (group_id, start_time, end_time) VALUES (?, ?, ?)",
        (message.chat.id, start_db, end_db),
    )
    await message.answer(f"🍽 Перерыв добавлен. ({start.strftime('%H:%M')}–{end.strftime('%H:%M')})")


def resolve_user_id(text: str) -> int | None:
    if not text:
        return None
    value = text.strip()
    if value.startswith("@"):
        username = value[1:]
        row = db.query("SELECT user_id FROM users WHERE username=?", (username,), fetch="one")
        return row[0] if row else None
    if value.isdigit():
        return int(value)
    return None


@dp.message(AdminForm.priority_add)
async def admin_priority_add(message: types.Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    target_id = resolve_user_id(message.text)
    if not target_id:
        return await message.answer("❌ Пользователь не найден. Введите @username или ID.")
    db.query("UPDATE users SET priority=1 WHERE user_id=?", (target_id,))
    await state.clear()
    await message.answer("✅ Приоритет выдан.")


@dp.message(AdminForm.priority_remove)
async def admin_priority_remove(message: types.Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    target_id = resolve_user_id(message.text)
    if not target_id:
        return await message.answer("❌ Пользователь не найден. Введите @username или ID.")
    db.query("UPDATE users SET priority=0 WHERE user_id=?", (target_id,))
    await state.clear()
    await message.answer("✅ Приоритет снят.")


@dp.message(AdminForm.admin_add)
async def admin_add_handler(message: types.Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    target_id = resolve_user_id(message.text)
    if not target_id:
        return await message.answer("❌ Пользователь не найден. Введите @username или ID.")
    db.query("UPDATE users SET is_admin=1 WHERE user_id=?", (target_id,))
    await state.clear()
    await message.answer("✅ Администратор добавлен.")


@dp.message(AdminForm.admin_remove)
async def admin_remove_handler(message: types.Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    target_id = resolve_user_id(message.text)
    if not target_id:
        return await message.answer("❌ Пользователь не найден. Введите @username или ID.")
    db.query("UPDATE users SET is_admin=0 WHERE user_id=?", (target_id,))
    await state.clear()
    await message.answer("✅ Администратор удален.")


@dp.message(AdminForm.message_user)
async def admin_message_user(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_user_id")
    number = data.get("target_number")
    if not target_id:
        await state.clear()
        return
    try:
        await bot.send_message(target_id, f"💬 Сообщение по номеру {number}: {message.text}")
        await message.answer("✅ Сообщение отправлено.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить сообщение.")
    await state.clear()


@dp.message(AdminForm.broadcast)
async def admin_broadcast(message: types.Message, state: FSMContext):
    if not is_user_admin(message.from_user.id):
        return
    users = db.query("SELECT user_id FROM users")
    sent = 0
    for (target_uid,) in users:
        try:
            await bot.send_message(target_uid, message.text)
            sent += 1
        except Exception:
            pass
    await state.clear()
    await message.answer(f"✅ Рассылка завершена. Отправлено: {sent}")


@dp.message(AdminForm.breaks_group)
async def admin_breaks_group(message: types.Message, state: FSMContext):
    data = await state.get_data()
    gid = data.get("break_group_id")
    if not gid:
        await state.clear()
        return

    raw = (message.text or "").strip()
    if re.fullmatch(r"-?\d+", raw) and int(raw) <= 0:
        db.query("DELETE FROM breaks WHERE group_id=?", (gid,))
        await state.clear()
        return await message.answer("🧹 Перерывы для группы очищены.")

    entries = parse_break_lines(message.text)
    if not entries:
        return await message.answer("❌ Формат: 11:00 - 12:00 (каждая строка) или 0/минус для очистки.")
    now = now_kz_naive()
    added = 0
    skipped = 0
    for start_time, end_time in entries:
        start_dt = start_time.replace(year=now.year, month=now.month, day=now.day)
        end_dt = end_time.replace(year=now.year, month=now.month, day=now.day)
        if end_dt <= start_dt:
            skipped += 1
            continue
        start_db = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_db = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        exists = db.query(
            "SELECT 1 FROM breaks WHERE group_id=? AND start_time=? AND end_time=?",
            (gid, start_db, end_db),
            fetch="one",
        )
        if exists:
            skipped += 1
            continue
        db.query(
            "INSERT INTO breaks (group_id, start_time, end_time) VALUES (?, ?, ?)",
            (gid, start_db, end_db),
        )
        added += 1
    await state.clear()
    await message.answer(f"✅ Перерывы обработаны. Добавлено: {added}, пропущено: {skipped}.")


@dp.message(Command("breaks"))
async def list_breaks(message: types.Message):
    if not is_user_admin(message.from_user.id):
        return
    if message.chat.type == "private":
        return await message.answer("❌ Введите это в группе!")
    today_label = now_kz_naive().strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT start_time, end_time FROM breaks WHERE group_id=? AND date(start_time)=? ORDER BY start_time ASC",
        (message.chat.id, today_label),
    )
    if not rows:
        return await message.answer("✅ Активных перерывов нет.")
    lines = ["🍽 Активные перерывы:"]
    for start_label, end_label in unique_break_rows(rows):
        lines.append(f"• {start_label}–{end_label}")
    await message.answer("\n".join(lines))


async def issue_next_number(chat_id: int, thread_id: int | None, operator_id: int):
    if not is_group_linked(chat_id, thread_id):
        return None
    cleanup_queue_expired()
    res = db.query(
        "SELECT q.id, q.user_id, q.number, u.username "
        "FROM queue q LEFT JOIN users u ON q.user_id=u.user_id "
        "WHERE q.status='waiting' ORDER BY u.priority DESC, q.id ASC LIMIT 1",
        fetch="one",
    )
    if not res:
        return None
    q_id, user_id, number, _username = res
    db.query(
        "UPDATE queue SET status='proc', proc_by=?, repeat_requested=0, qr_requested=0 WHERE id=?",
        (operator_id, q_id),
    )
    return user_id, number


@dp.message(F.text.regexp(r"(?i)^\s*номер\s*([.!])?\s*$"))
async def get_num(message: types.Message):
    thread_id = get_thread_id(message)
    if not is_group_linked(message.chat.id, thread_id):
        return
    result = await issue_next_number(message.chat.id, thread_id, message.from_user.id)
    if not result:
        return await message.answer("Очередь пуста.")

    user_id, number = result
    next_kb = InlineKeyboardBuilder()
    next_kb.row(types.InlineKeyboardButton(text="⏭ Скип", callback_data=f"k_{user_id}_{number}"))
    await message.answer(
        f"Номер: <code>{number}</code>\n"
        "Отправьте фото в ответ на это сообщение.",
        parse_mode="HTML",
        reply_markup=next_kb.as_markup(),
    )
    try:
        office_label = get_office_label_for_group(message.chat.id)
        await bot.send_message(
            user_id,
            f"📨 Ваш номер {number} взяли. {office_label}. Ожидайте кода.",
        )
    except Exception:
        pass


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await _process_code_media(message)


@dp.message(F.document)
async def handle_document_image(message: types.Message):
    # Иногда код отправляют как документ-картинку (jpg/png), а не как photo.
    mime = (message.document.mime_type or "") if message.document else ""
    if not mime.startswith("image/"):
        return
    await _process_code_media(message)


async def _process_code_media(message: types.Message):
    thread_id = get_thread_id(message)
    if not is_group_linked(message.chat.id, thread_id):
        return

    worker_thread_id = get_thread_id(message)
    req_kb = InlineKeyboardBuilder()

    num = None
    worker_id = None
    source_text = ""
    if message.reply_to_message:
        source_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        match = re.search(r"\+\d{10,15}", source_text)
        if match:
            num = match.group()
            worker = db.query(
                "SELECT user_id FROM queue WHERE number=? AND status='proc' AND proc_by=? ORDER BY id DESC LIMIT 1",
                (num, message.from_user.id),
                fetch="one",
            )
            if not worker:
                worker = db.query(
                    "SELECT user_id FROM queue WHERE number=? AND status='proc' ORDER BY id DESC LIMIT 1",
                    (num,),
                    fetch="one",
                )
            if worker:
                worker_id = worker[0]

    if not num or not worker_id:
        fallback = db.query(
            "SELECT number, user_id FROM queue WHERE status='proc' AND proc_by=? ORDER BY id DESC LIMIT 1",
            (message.from_user.id,),
            fetch="one",
        )
        if not fallback:
            fallback = db.query(
                "SELECT number, user_id FROM queue WHERE status='proc' ORDER BY id DESC LIMIT 1",
                fetch="one",
            )
        if not fallback:
            return await message.answer("⚠️ Номер не найден в очереди.")
        num, worker_id = fallback

    db.query(
        "UPDATE queue SET code_sender_id=? WHERE number=? AND user_id=? AND status='proc'",
        (message.from_user.id, num, worker_id),
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        types.InlineKeyboardButton(text="✅ Встал", callback_data=f"v_{worker_id}_{num}"),
        types.InlineKeyboardButton(text="⏭ Скип", callback_data=f"k_{worker_id}_{num}"),
    )
    kb.row(
        types.InlineKeyboardButton(text="⚠️ Ошибка", callback_data=f"e_{worker_id}_{num}"),
        types.InlineKeyboardButton(text="💬 Сообщение", callback_data=f"m_{worker_id}_{num}"),
    )

    media_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)
    if not media_id:
        return await message.answer("⚠️ Не удалось обработать файл.")

    await message.answer_photo(media_id, caption=f"⚙️ Работа: {num}", reply_markup=kb.as_markup())

    try:
        worker_thread_id = message.message_thread_id if message.is_topic_message else 0
        req_kb = InlineKeyboardBuilder()
        req_kb.row(
            types.InlineKeyboardButton(
                text="🔁 Запросить повторно",
                callback_data=f"rr_{message.chat.id}_{worker_thread_id}_{worker_id}_{num}",
            ),
            types.InlineKeyboardButton(
                text="🧾 Запросить QR",
                callback_data=f"qr_{message.chat.id}_{worker_thread_id}_{worker_id}_{num}",
            ),
        )
        await bot.send_photo(
            worker_id,
            media_id,
            caption=f"📩 По вашему номеру {num} пришел код.",
            reply_markup=req_kb.as_markup(),
        )
    except Exception:
        await message.answer(f"⚠️ Не удалось отправить код владельцу номера {num} в ЛС.")



@dp.message(Form.num)
async def num_input(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id):
        return await message.answer("🚫 Вы забанены и не можете сдавать номера.")
    numbers = unique_numbers(parse_numbers(message.text))
    if numbers:
        user_priority = 1 if is_user_priority(message.from_user.id) else 0
        for num in numbers:
            db.query(
                "INSERT INTO queue (user_id, number, status, created_at) VALUES (?, ?, 'waiting', ?)",
                (message.from_user.id, num, now_kz_naive().strftime("%Y-%m-%d %H:%M:%S")),
            )
            db.query(
                "INSERT INTO submissions (user_id, number, created_at) VALUES (?, ?, ?)",
                (message.from_user.id, num, now_kz_naive().strftime("%Y-%m-%d %H:%M:%S")),
            )
        last_id = db.query(
            "SELECT MAX(id) FROM queue WHERE user_id=?",
            (message.from_user.id,),
            fetch="one",
        )[0]
        q_pos = db.query(
            "SELECT COUNT(*) FROM queue q "
            "JOIN users u ON q.user_id=u.user_id "
            "WHERE q.status='waiting' AND (u.priority > ? OR (u.priority = ? AND q.id <= ?))",
            (user_priority, user_priority, last_id),
            fetch="one",
        )[0]
        total_waiting = db.query("SELECT COUNT(*) FROM queue WHERE status='waiting'", fetch="one")[0]
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="u_menu"))
        await message.answer(
            f"✅ Номер(а) добавлены: <b>{len(numbers)}</b>\n"
            f"📋 Ваша позиция в очереди: <b>{q_pos}</b>\n"
            f"📋 Всего в очереди: <b>{total_waiting}</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer("❌ Ошибка! Введите 11 цифр в каждой строке или через пробел.")


@dp.message(Command(commands=["submit", "sumbit"]))
async def submit_cmd(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id):
        return await message.answer("🚫 Вы забанены и не можете сдавать номера.")
    await state.set_state(Form.num)
    await message.answer("📞 Введите номера (каждый с новой строки или через пробел):")


@dp.message(Command("queue"))
async def queue_cmd(message: types.Message):
    caption, _ = build_user_queue_view(message.from_user.id)
    await message.answer(caption, parse_mode="HTML")


@dp.message(Command("archive"))
async def archive_cmd(message: types.Message):
    uid = message.from_user.id
    balance = get_user_balance(uid)
    rows = db.query(
        "SELECT number, status, paid, start_time, end_time, group_id "
        "FROM sessions "
        "WHERE user_id=? AND status='slet' "
        "ORDER BY id DESC LIMIT 300",
        (uid,),
    )

    lines = [
        f"<b>💰 Баланс:</b> <b>{balance}$</b>",
        "<b>📱 Мои номера:</b>",
        "",
    ]

    if not rows:
        lines.append("Номеров пока нет")
    else:
        now = now_kz_naive()
        for number, status, paid, start_time, end_time, group_id in rows:
            start_dt = parse_dt(start_time)
            end_dt = parse_dt(end_time) if end_time else now
            worked = effective_minutes(group_id, start_dt, end_dt)

            status_label = "отстоял / слетел" if (paid or worked >= HOLD_MINUTES) else "слетел"
            lines.append(f"• <code>{number}</code> — {status_label} • {worked} мин")

    await message.answer("\n".join(lines), parse_mode="HTML")


async def hold_checker():
    while True:
        await asyncio.sleep(60)


async def nightly_cleanup():
    while True:
        now = datetime.now(KZ_TZ)
        next_cleanup = now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if next_cleanup <= now:
            next_cleanup += timedelta(days=1)

        await asyncio.sleep((next_cleanup - now).total_seconds())

        try:
            cleanup_paid_reports()
            cleanup_archives()
        except Exception:
            pass


    
async def main():
    asyncio.create_task(hold_checker())
    asyncio.create_task(nightly_cleanup())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
