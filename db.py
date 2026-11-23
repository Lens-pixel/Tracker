from datetime import datetime, timedelta, timezone
import json
import logging
import ssl as ssl_module
from typing import Any, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

logger = logging.getLogger("calorie_bot.db")


def normalize_postgres_dsn(database_url: str) -> tuple[str, Any]:
    """Normalize Neon / Render PostgreSQL URLs for asyncpg."""
    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = None
    kept_params: list[tuple[str, str]] = []
    for k, v in query_items:
        kl = k.lower()
        if kl == "sslmode":
            sslmode = v.lower()
        elif kl in ("channel_binding", "options"):
            # asyncpg does not accept libpq channel_binding param in DSN
            continue
        else:
            kept_params.append((k, v))

    clean_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept_params), parts.fragment)
    )

    ssl_arg: Any = None
    host = (parts.hostname or "").lower()
    if sslmode in ("require", "verify-ca", "verify-full") or "neon.tech" in host or "render.com" in host:
        ctx = ssl_module.create_default_context()
        if sslmode == "require" or sslmode is None:
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
        ssl_arg = ctx
    elif sslmode == "disable":
        ssl_arg = False

    return clean_url, ssl_arg


def to_pg_placeholders(sql: str) -> str:
    """Convert SQLite '?' placeholders to PostgreSQL '$1, $2, ...'."""
    out: list[str] = []
    idx = 0
    for ch in sql:
        if ch == "?":
            idx += 1
            out.append(f"${idx}")
        else:
            out.append(ch)
    return "".join(out)


class Database:
    def __init__(self, path: str, database_url: str = "") -> None:
        self.path = path
        self.database_url = database_url.strip()
        self.is_postgres = self.database_url.startswith(("postgres://", "postgresql://"))
        self._pool: Any = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    async def _ensure_sqlite_column(
        db: aiosqlite.Connection,
        table: str,
        column: str,
        col_def: str,
    ) -> None:
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cur.fetchall()}
        if column not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    async def init(self) -> None:
        if self.is_postgres:
            if asyncpg is None:
                raise RuntimeError("asyncpg is required when DATABASE_URL is set")
            dsn, ssl_arg = normalize_postgres_dsn(self.database_url)
            self._pool = await asyncpg.create_pool(
                dsn=dsn,
                ssl=ssl_arg,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        gender TEXT,
                        age INTEGER,
                        height_cm DOUBLE PRECISION,
                        weight_kg DOUBLE PRECISION,
                        activity TEXT,
                        goal TEXT,
                        target_kcal INTEGER DEFAULT 2000,
                        target_protein INTEGER DEFAULT 120,
                        target_fat INTEGER DEFAULT 65,
                        target_carbs INTEGER DEFAULT 220,
                        free_used INTEGER DEFAULT 0,
                        sub_until TEXT,
                        onboarded INTEGER DEFAULT 0,
                        terms_accepted_at TEXT,
                        source TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meals (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES users(user_id),
                        meal_name TEXT NOT NULL,
                        items_json TEXT NOT NULL,
                        kcal INTEGER NOT NULL,
                        protein DOUBLE PRECISION NOT NULL,
                        fat DOUBLE PRECISION NOT NULL,
                        carbs DOUBLE PRECISION NOT NULL,
                        source_type TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_meals (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        meal_name TEXT NOT NULL,
                        items_json TEXT NOT NULL,
                        kcal INTEGER NOT NULL,
                        protein DOUBLE PRECISION NOT NULL,
                        fat DOUBLE PRECISION NOT NULL,
                        carbs DOUBLE PRECISION NOT NULL,
                        short_tip TEXT,
                        source_type TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payments (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        provider TEXT NOT NULL DEFAULT 'stars',
                        telegram_payment_charge_id TEXT,
                        amount_stars INTEGER NOT NULL DEFAULT 0,
                        amount_kzt INTEGER NOT NULL DEFAULT 0,
                        days INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kaspi_orders (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        plan TEXT NOT NULL,
                        days INTEGER NOT NULL,
                        amount_kzt INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        receipt_file_id TEXT,
                        receipt_type TEXT,
                        created_at TEXT NOT NULL,
                        resolved_at TEXT
                    )
                    """
                )
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS source TEXT")
                await conn.execute(
                    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'stars'"
                )
                await conn.execute(
                    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_kzt INTEGER NOT NULL DEFAULT 0"
                )
            logger.info("Connected to PostgreSQL database")
            return

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    gender TEXT,
                    age INTEGER,
                    height_cm REAL,
                    weight_kg REAL,
                    activity TEXT,
                    goal TEXT,
                    target_kcal INTEGER DEFAULT 2000,
                    target_protein INTEGER DEFAULT 120,
                    target_fat INTEGER DEFAULT 65,
                    target_carbs INTEGER DEFAULT 220,
                    free_used INTEGER DEFAULT 0,
                    sub_until TEXT,
                    onboarded INTEGER DEFAULT 0,
                    terms_accepted_at TEXT,
                    source TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS meals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    meal_name TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    kcal INTEGER NOT NULL,
                    protein REAL NOT NULL,
                    fat REAL NOT NULL,
                    carbs REAL NOT NULL,
                    source_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_meals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    meal_name TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    kcal INTEGER NOT NULL,
                    protein REAL NOT NULL,
                    fat REAL NOT NULL,
                    carbs REAL NOT NULL,
                    short_tip TEXT,
                    source_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'stars',
                    telegram_payment_charge_id TEXT,
                    amount_stars INTEGER NOT NULL DEFAULT 0,
                    amount_kzt INTEGER NOT NULL DEFAULT 0,
                    days INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS kaspi_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    plan TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    amount_kzt INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    receipt_file_id TEXT,
                    receipt_type TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            await self._ensure_sqlite_column(db, "users", "terms_accepted_at", "TEXT")
            await self._ensure_sqlite_column(db, "users", "source", "TEXT")
            await self._ensure_sqlite_column(db, "payments", "provider", "TEXT NOT NULL DEFAULT 'stars'")
            await self._ensure_sqlite_column(db, "payments", "amount_kzt", "INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        logger.info("Using SQLite database: %s", self.path)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.is_postgres:
            async with self._pool.acquire() as conn:
                status = await conn.execute(to_pg_placeholders(sql), *params)
            try:
                return int(status.split()[-1])
            except (ValueError, IndexError):
                return 0
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, tuple(params))
            await db.commit()
            return int(cur.rowcount)

    async def _insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.is_postgres:
            pg_sql = to_pg_placeholders(sql).rstrip().rstrip(";") + " RETURNING id"
            async with self._pool.acquire() as conn:
                val = await conn.fetchval(pg_sql, *params)
            return int(val)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, tuple(params))
            await db.commit()
            return int(cur.lastrowid)

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        if self.is_postgres:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(to_pg_placeholders(sql), *params)
            return dict(row) if row is not None else None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, tuple(params))
            row = await cur.fetchone()
            return dict(row) if row is not None else None

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if self.is_postgres:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(to_pg_placeholders(sql), *params)
            return [dict(r) for r in rows]
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, tuple(params))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.is_postgres:
            async with self._pool.acquire() as conn:
                return await conn.fetchval(to_pg_placeholders(sql), *params)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(sql, tuple(params))
            row = await cur.fetchone()
            return row[0] if row else None

    async def ensure_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = await self._fetchone("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        is_new = existing is None
        clean_source = (source or "").strip()[:64] or None
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
            """,
            (user_id, username, first_name, clean_source, self._now_iso()),
        )
        row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        assert row is not None
        row["is_new"] = is_new
        return row

    async def get_recent_users(self, limit: int = 15) -> list[dict[str, Any]]:
        return await self._fetchall(
            """
            SELECT user_id, username, first_name, source, free_used, sub_until, onboarded, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    async def get_total_users_count(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM users") or 0)

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        clean = username.lstrip("@").strip().lower()
        if not clean:
            return None
        val = await self._fetchval(
            "SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1",
            (clean,),
        )
        return int(val) if val is not None else None

    async def accept_terms(self, user_id: int) -> str:
        ts = self._now_iso()
        await self._execute(
            "UPDATE users SET terms_accepted_at = ? WHERE user_id = ?",
            (ts, user_id),
        )
        return ts

    async def save_profile(
        self,
        user_id: int,
        gender: str,
        age: int,
        height_cm: float,
        weight_kg: float,
        activity: str,
        goal: str,
        target_kcal: int,
        target_protein: int,
        target_fat: int,
        target_carbs: int,
    ) -> None:
        await self._execute(
            """
            UPDATE users
            SET gender = ?, age = ?, height_cm = ?, weight_kg = ?,
                activity = ?, goal = ?, target_kcal = ?, target_protein = ?,
                target_fat = ?, target_carbs = ?, onboarded = 1
            WHERE user_id = ?
            """,
            (
                gender,
                age,
                height_cm,
                weight_kg,
                activity,
                goal,
                target_kcal,
                target_protein,
                target_fat,
                target_carbs,
                user_id,
            ),
        )

    def is_subscribed(self, user: dict[str, Any]) -> bool:
        sub_until = user.get("sub_until")
        if not sub_until:
            return False
        try:
            dt = datetime.fromisoformat(sub_until)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt > datetime.now(timezone.utc)
        except ValueError:
            return False

    async def increment_free_used(self, user_id: int) -> None:
        await self._execute(
            "UPDATE users SET free_used = free_used + 1 WHERE user_id = ?",
            (user_id,),
        )

    async def grant_subscription(
        self,
        user_id: int,
        days: int,
        amount_stars: int = 0,
        charge_id: str = "",
        provider: str = "stars",
        amount_kzt: int = 0,
    ) -> datetime:
        user = await self.get_user(user_id)
        now = datetime.now(timezone.utc)
        base = now
        if user and user.get("sub_until"):
            try:
                existing = datetime.fromisoformat(user["sub_until"])
                if existing.tzinfo is None:
                    existing = existing.replace(tzinfo=timezone.utc)
                if existing > now:
                    base = existing
            except ValueError:
                pass

        new_until = base + timedelta(days=days)
        await self._execute(
            "UPDATE users SET sub_until = ? WHERE user_id = ?",
            (new_until.isoformat(), user_id),
        )
        await self._execute(
            """
            INSERT INTO payments (
                user_id, provider, telegram_payment_charge_id, amount_stars, amount_kzt, days, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, provider, charge_id, amount_stars, amount_kzt, days, now.isoformat()),
        )
        return new_until

    async def create_kaspi_order(
        self,
        user_id: int,
        plan: str,
        days: int,
        amount_kzt: int,
        receipt_file_id: str,
        receipt_type: str,
    ) -> int:
        return await self._insert_returning_id(
            """
            INSERT INTO kaspi_orders (
                user_id, plan, days, amount_kzt, status, receipt_file_id, receipt_type, created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (user_id, plan, days, amount_kzt, receipt_file_id, receipt_type, self._now_iso()),
        )

    async def get_kaspi_order(self, order_id: int) -> Optional[dict[str, Any]]:
        return await self._fetchone("SELECT * FROM kaspi_orders WHERE id = ?", (order_id,))

    async def resolve_kaspi_order(self, order_id: int, new_status: str) -> Optional[dict[str, Any]]:
        affected = await self._execute(
            """
            UPDATE kaspi_orders
            SET status = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (new_status, self._now_iso(), order_id),
        )
        if affected == 0:
            return None
        return await self.get_kaspi_order(order_id)

    async def create_pending_meal(
        self,
        user_id: int,
        meal_name: str,
        items: list[dict[str, Any]],
        kcal: int,
        protein: float,
        fat: float,
        carbs: float,
        short_tip: Optional[str],
        source_type: str,
    ) -> int:
        return await self._insert_returning_id(
            """
            INSERT INTO pending_meals (
                user_id, meal_name, items_json, kcal, protein, fat, carbs, short_tip, source_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                meal_name,
                json.dumps(items, ensure_ascii=False),
                kcal,
                protein,
                fat,
                carbs,
                short_tip,
                source_type,
                self._now_iso(),
            ),
        )

    async def get_pending_meal(self, pending_id: int, user_id: int) -> Optional[dict[str, Any]]:
        data = await self._fetchone(
            "SELECT * FROM pending_meals WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        )
        if not data:
            return None
        data["items"] = json.loads(data["items_json"])
        return data

    async def delete_pending_meal(self, pending_id: int, user_id: int) -> None:
        await self._execute(
            "DELETE FROM pending_meals WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        )

    async def commit_pending_meal(self, pending_id: int, user_id: int) -> Optional[dict[str, Any]]:
        pending = await self.get_pending_meal(pending_id, user_id)
        if not pending:
            return None
        await self._execute(
            """
            INSERT INTO meals (
                user_id, meal_name, items_json, kcal, protein, fat, carbs, source_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                pending["meal_name"],
                pending["items_json"],
                pending["kcal"],
                pending["protein"],
                pending["fat"],
                pending["carbs"],
                pending["source_type"],
                self._now_iso(),
            ),
        )
        await self.delete_pending_meal(pending_id, user_id)
        return pending

    async def get_today_meals(self, user_id: int) -> list[dict[str, Any]]:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        rows = await self._fetchall(
            """
            SELECT * FROM meals
            WHERE user_id = ? AND created_at >= ?
            ORDER BY id ASC
            """,
            (user_id, since),
        )

        result: list[dict[str, Any]] = []
        for item in rows:
            dt = datetime.fromisoformat(item["created_at"]).astimezone()
            if dt.strftime("%Y-%m-%d") == today_prefix:
                item["local_time"] = dt.strftime("%H:%M")
                result.append(item)
        return result

    async def delete_last_today_meal(self, user_id: int) -> Optional[dict[str, Any]]:
        meals = await self.get_today_meals(user_id)
        if not meals:
            return None
        last_meal = meals[-1]
        await self._execute(
            "DELETE FROM meals WHERE id = ? AND user_id = ?",
            (last_meal["id"], user_id),
        )
        return last_meal

    async def get_week_summary(self, user_id: int) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        rows = await self._fetchall(
            """
            SELECT kcal, protein, fat, carbs, created_at
            FROM meals
            WHERE user_id = ? AND created_at >= ?
            ORDER BY created_at ASC
            """,
            (user_id, since),
        )

        by_day: dict[str, dict[str, float]] = {}
        today = datetime.now().date()
        for offset in range(6, -1, -1):
            day_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            by_day[day_str] = {"kcal": 0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "count": 0}

        for row in rows:
            dt = datetime.fromisoformat(row["created_at"]).astimezone()
            day_str = dt.strftime("%Y-%m-%d")
            if day_str in by_day:
                by_day[day_str]["kcal"] += row["kcal"]
                by_day[day_str]["protein"] += row["protein"]
                by_day[day_str]["fat"] += row["fat"]
                by_day[day_str]["carbs"] += row["carbs"]
                by_day[day_str]["count"] += 1

        return [{"day": day, **vals} for day, vals in by_day.items()]

    async def get_admin_stats(self) -> dict[str, int]:
        now_iso = self._now_iso()
        total_users = int(await self._fetchval("SELECT COUNT(*) FROM users") or 0)
        active_subs = int(
            await self._fetchval(
                "SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL AND sub_until > ?",
                (now_iso,),
            )
            or 0
        )
        total_meals = int(await self._fetchval("SELECT COUNT(*) FROM meals") or 0)
        total_stars = int(
            await self._fetchval("SELECT COALESCE(SUM(amount_stars), 0) FROM payments") or 0
        )
        total_kzt = int(
            await self._fetchval("SELECT COALESCE(SUM(amount_kzt), 0) FROM payments") or 0
        )
        pending_kaspi = int(
            await self._fetchval("SELECT COUNT(*) FROM kaspi_orders WHERE status = 'pending'") or 0
        )
        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "total_meals": total_meals,
            "total_stars": total_stars,
            "total_kzt": total_kzt,
            "pending_kaspi": pending_kaspi,
        }
