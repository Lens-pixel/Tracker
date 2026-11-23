import asyncio
import os
import tempfile

from config import load_config
from db import Database
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


async def test_db_flow() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        await db.init()
        user = await db.ensure_user(12345, "tester", "Test")
        assert user["user_id"] == 12345
        assert user["terms_accepted_at"] is None
        assert not db.is_subscribed(user)

        await db.accept_terms(12345)
        user = await db.get_user(12345)
        assert user["terms_accepted_at"] is not None

        pid = await db.create_pending_meal(
            user_id=12345,
            meal_name="Омлет",
            items=[{"name": "Яйца", "grams": 150, "kcal": 220, "protein": 18, "fat": 15, "carbs": 1}],
            kcal=220,
            protein=18.0,
            fat=15.0,
            carbs=1.0,
            short_tip="Хороший белковый завтрак.",
            source_type="text",
        )
        saved = await db.commit_pending_meal(pid, 12345)
        assert saved is not None
        today = await db.get_today_meals(12345)
        assert len(today) == 1
        assert today[0]["kcal"] == 220

        # Test Kaspi order flow & double-approve protection
        oid = await db.create_kaspi_order(12345, "1m", 30, 2490, "file_abc", "photo")
        resolved = await db.resolve_kaspi_order(oid, "approved")
        assert resolved is not None
        assert await db.resolve_kaspi_order(oid, "approved") is None  # atomic guard

        await db.grant_subscription(12345, days=30, provider="kaspi", amount_kzt=2490, charge_id=f"kaspi_{oid}")
        user_after = await db.get_user(12345)
        assert db.is_subscribed(user_after)

        stats = await db.get_admin_stats()
        assert stats["active_subs"] == 1
        assert stats["total_kzt"] == 2490
        assert stats["pending_kaspi"] == 0
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    test_calculate_targets()
    test_terms_length()
    asyncio.run(test_db_flow())
    print("ALL_TESTS_PASSED")
