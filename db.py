from datetime import datetime, timedelta, timezone
import json
from typing import Any, Optional
import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    @staticmethod
    async def _ensure_column(
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

            # Safe migrations for existing databases
            await self._ensure_column(db, "users", "terms_accepted_at", "TEXT")
            await self._ensure_column(db, "payments", "provider", "TEXT NOT NULL DEFAULT 'stars'")
            await self._ensure_column(db, "payments", "amount_kzt", "INTEGER NOT NULL DEFAULT 0")
            await db.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def ensure_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO users (user_id, username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name, self._now_iso()),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row)

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        clean = username.lstrip("@").strip().lower()
        if not clean:
            return None
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id FROM users WHERE LOWER(username) = ? LIMIT 1",
                (clean,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def accept_terms(self, user_id: int) -> str:
        ts = self._now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET terms_accepted_at = ? WHERE user_id = ?",
                (ts, user_id),
            )
            await db.commit()
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
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
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
            await db.commit()

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
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET free_used = free_used + 1 WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

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
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET sub_until = ? WHERE user_id = ?",
                (new_until.isoformat(), user_id),
            )
            await db.execute(
                """
                INSERT INTO payments (
                    user_id, provider, telegram_payment_charge_id, amount_stars, amount_kzt, days, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, provider, charge_id, amount_stars, amount_kzt, days, now.isoformat()),
            )
            await db.commit()
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
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                INSERT INTO kaspi_orders (
                    user_id, plan, days, amount_kzt, status, receipt_file_id, receipt_type, created_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (user_id, plan, days, amount_kzt, receipt_file_id, receipt_type, self._now_iso()),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_kaspi_order(self, order_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM kaspi_orders WHERE id = ?", (order_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def resolve_kaspi_order(self, order_id: int, new_status: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                UPDATE kaspi_orders
                SET status = ?, resolved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (new_status, self._now_iso(), order_id),
            )
            await db.commit()
            if cur.rowcount == 0:
                return None
            cur2 = await db.execute("SELECT * FROM kaspi_orders WHERE id = ?", (order_id,))
            row = await cur2.fetchone()
            return dict(row) if row else None

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
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
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
            await db.commit()
            return int(cur.lastrowid)

    async def get_pending_meal(self, pending_id: int, user_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM pending_meals WHERE id = ? AND user_id = ?",
                (pending_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            data = dict(row)
            data["items"] = json.loads(data["items_json"])
            return data

    async def delete_pending_meal(self, pending_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM pending_meals WHERE id = ? AND user_id = ?",
                (pending_id, user_id),
            )
            await db.commit()

    async def commit_pending_meal(self, pending_id: int, user_id: int) -> Optional[dict[str, Any]]:
        pending = await self.get_pending_meal(pending_id, user_id)
        if not pending:
            return None
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
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
            await db.execute(
                "DELETE FROM pending_meals WHERE id = ? AND user_id = ?",
                (pending_id, user_id),
            )
            await db.commit()
        return pending

    async def get_today_meals(self, user_id: int) -> list[dict[str, Any]]:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM meals
                WHERE user_id = ? AND created_at >= ?
                ORDER BY id ASC
                """,
                (user_id, since),
            )
            rows = await cur.fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
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
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM meals WHERE id = ? AND user_id = ?",
                (last_meal["id"], user_id),
            )
            await db.commit()
        return last_meal

    async def get_week_summary(self, user_id: int) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT kcal, protein, fat, carbs, created_at
                FROM meals
                WHERE user_id = ? AND created_at >= ?
                ORDER BY created_at ASC
                """,
                (user_id, since),
            )
            rows = await cur.fetchall()

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
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL AND sub_until > ?",
                (now_iso,),
            )
            active_subs = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM meals")
            total_meals = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COALESCE(SUM(amount_stars), 0) FROM payments")
            total_stars = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COALESCE(SUM(amount_kzt), 0) FROM payments")
            total_kzt = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM kaspi_orders WHERE status = 'pending'")
            pending_kaspi = (await cur.fetchone())[0]
        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "total_meals": total_meals,
            "total_stars": total_stars,
            "total_kzt": total_kzt,
            "pending_kaspi": pending_kaspi,
        }
