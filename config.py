from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    bot_token: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    free_analyses_limit: int
    sub_price_1m_stars: int
    sub_price_3m_stars: int
    kaspi_enabled: bool
    kaspi_pay_url: str
    kaspi_phone: str
    kaspi_recipient: str
    kaspi_price_1m_kzt: int
    kaspi_price_3m_kzt: int
    support_contact: str
    db_path: str
    database_url: str
    admin_ids: tuple[int, ...]
    port: int
    webhook_url: str


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set in .env")
    if not gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids = tuple(
        int(item.strip())
        for item in raw_admins.split(",")
        if item.strip().isdigit()
    )

    raw_db = os.getenv("DB_PATH", "calorie_bot.db").strip()
    db_path = str(Path(raw_db) if Path(raw_db).is_absolute() else (BASE_DIR / raw_db))
    database_url = os.getenv("DATABASE_URL", "").strip()

    raw_port = os.getenv("PORT", "").strip()
    port = int(raw_port) if raw_port.isdigit() else 0
    webhook_url = (
        os.getenv("WEBHOOK_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    ).rstrip("/")

    kaspi_pay_url = os.getenv("KASPI_PAY_URL", "").strip()
    kaspi_phone = os.getenv("KASPI_PHONE", "").strip()
    kaspi_enabled = os.getenv("KASPI_ENABLED", "true").strip().lower() in ("1", "true", "yes")

    return Config(
        bot_token=bot_token,
        gemini_api_key=gemini_api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash").strip(),
        free_analyses_limit=int(os.getenv("FREE_ANALYSES_LIMIT", "5")),
        sub_price_1m_stars=int(os.getenv("SUB_PRICE_1M_STARS", "250")),
        sub_price_3m_stars=int(os.getenv("SUB_PRICE_3M_STARS", "600")),
        kaspi_enabled=kaspi_enabled,
        kaspi_pay_url=kaspi_pay_url,
        kaspi_phone=kaspi_phone,
        kaspi_recipient=os.getenv("KASPI_RECIPIENT", "").strip(),
        kaspi_price_1m_kzt=int(os.getenv("KASPI_PRICE_1M_KZT", "2490")),
        kaspi_price_3m_kzt=int(os.getenv("KASPI_PRICE_3M_KZT", "5990")),
        support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
        db_path=db_path,
        database_url=database_url,
        admin_ids=admin_ids,
        port=port,
        webhook_url=webhook_url,
    )
