import asyncio
import os
import tempfile

from config import load_config
from db import Database, normalize_postgres_dsn, to_pg_placeholders
from nutrition import calculate_targets, progress_bar
from terms import get_full_terms_text, get_short_terms_notice


def test_calculate_targets() -> None:
    t_lose = calculate_targets("male", 28, 180, 82, "medium", "lose")
    assert 1800 <= t_lose.kcal <= 2600
    assert t_lose.protein > 100
    assert t_lose.fat > 50
    assert t_lose.carbs >= 50

    t_female = calculate_targets("female", 25, 165, 58, "light", "maintain")
    assert 1500 <= t_female.kcal <= 2200
    assert progress_bar(500, 1000, 10) == "█████░░░░░"


def test_terms_length() -> None:
    cfg = load_config()
    short = get_short_terms_notice()
    full = get_full_terms_text(cfg)
    assert 100 < len(short) < 1500
    assert 500 < len(full) < 4096


def test_pg_helpers() -> None:
    assert to_pg_placeholders("SELECT * FROM users WHERE a = ? AND b = ?") == (
        "SELECT * FROM users WHERE a = $1 AND b = $2"
    )
    dsn, ssl_arg = normalize_postgres_dsn(
        "postgres://u:p@ep-cool-123.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    )
    assert dsn.startswith("postgresql://")
    assert "channel_binding" not in dsn
    assert "sslmode" not in dsn
    assert ssl_arg is not None


async def test_db_flow() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        await db.init()
        uid = 6740328688  # 64-bit Telegram user_id
        user = await db.ensure_user(uid, "Lenskkrutoi", "Lensx", source="threads")
        assert user["user_id"] == uid
        assert user["is_new"] is True
        assert user["source"] == "threads"
        user_again = await db.ensure_user(uid, "Lenskkrutoi", "Lensx", source="other")
        assert user_again["is_new"] is False
        assert user_again["source"] == "threads"  # original source preserved
        recent = await db.get_recent_users(5)
        assert len(recent) == 1 and recent[0]["user_id"] == uid
        assert user["terms_accepted_at"] is None
        assert not db.is_subscribed(user)
        assert await db.get_user_id_by_username("@lenskkrutoi") == uid

        await db.accept_terms(uid)
        user = await db.get_user(uid)
        assert user["terms_accepted_at"] is not None

        pid = await db.create_pending_meal(
            user_id=uid,
            meal_name="Омлет",
            items=[{"name": "Яйца", "grams": 150, "kcal": 220, "protein": 18, "fat": 15, "carbs": 1}],
            kcal=220,
            protein=18.0,
            fat=15.0,
            carbs=1.0,
            short_tip="Хороший белковый завтрак.",
            source_type="text",
        )
        saved = await db.commit_pending_meal(pid, uid)
        assert saved is not None
        today = await db.get_today_meals(uid)
        assert len(today) == 1
        assert today[0]["kcal"] == 220

        oid = await db.create_kaspi_order(uid, "1m", 30, 2490, "file_abc", "photo")
        resolved = await db.resolve_kaspi_order(oid, "approved")
        assert resolved is not None
        assert await db.resolve_kaspi_order(oid, "approved") is None

        await db.grant_subscription(uid, days=30, provider="kaspi", amount_kzt=2490, charge_id=f"kaspi_{oid}")
        user_after = await db.get_user(uid)
        assert db.is_subscribed(user_after)

        stats = await db.get_admin_stats()
        assert stats["active_subs"] == 1
        assert stats["total_kzt"] == 2490
        assert stats["pending_kaspi"] == 0
        await db.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    test_calculate_targets()
    test_terms_length()
    test_pg_helpers()
    asyncio.run(test_db_flow())
    print("ALL_TESTS_PASSED")
