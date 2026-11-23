import asyncio
from datetime import datetime
import hashlib
import html
import io
import logging
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from ai_analyzer import MealAnalysis, NutritionAnalyzer
from config import Config, load_config
from db import Database
from nutrition import (
    ACTIVITY_LABELS,
    GOAL_LABELS,
    calculate_targets,
    format_meal_preview,
    progress_bar,
)
from terms import get_full_terms_text, get_short_terms_notice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("calorie_bot")

MENU_TODAY = "📊 Сегодня"
MENU_WEEK = "📈 Неделя"
MENU_SUB = "⭐ Подписка"
MENU_PROFILE = "⚙️ Профиль"


class OnboardingStates(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    activity = State()
    goal = State()


class CorrectionStates(StatesGroup):
    waiting_correction = State()


class KaspiStates(StatesGroup):
    waiting_receipt = State()


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_TODAY), KeyboardButton(text=MENU_WEEK)],
            [KeyboardButton(text=MENU_SUB), KeyboardButton(text=MENU_PROFILE)],
        ],
        resize_keyboard=True,
    )


def terms_accept_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Читать оферту полностью", callback_data="show_full_terms")],
            [InlineKeyboardButton(text="✅ Принимаю условия и начать", callback_data="accept_terms")],
        ]
    )


def pending_meal_kb(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Записать", callback_data=f"meal_save:{pending_id}"),
                InlineKeyboardButton(text="✏️ Уточнить", callback_data=f"meal_edit:{pending_id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Отмена", callback_data=f"meal_cancel:{pending_id}"),
            ],
        ]
    )


def subscription_kb(config: Config) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"⭐ Telegram Stars — 1 мес ({config.sub_price_1m_stars} ⭐)",
                callback_data="buy:1m",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🔥 Telegram Stars — 3 мес ({config.sub_price_3m_stars} ⭐)",
                callback_data="buy:3m",
            )
        ],
    ]
    if config.kaspi_enabled:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text=f"🇰🇿 Kaspi — 1 мес ({config.kaspi_price_1m_kzt:,} ₸)".replace(",", " "),
                        callback_data="kaspi:1m",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"🇰🇿 Kaspi — 3 мес ({config.kaspi_price_3m_kzt:,} ₸)".replace(",", " "),
                        callback_data="kaspi:3m",
                    )
                ],
            ]
        )
    rows.append([InlineKeyboardButton(text="📄 Публичная оферта", callback_data="show_full_terms")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужчина", callback_data="gender:male"),
                InlineKeyboardButton(text="👩 Женщина", callback_data="gender:female"),
            ]
        ]
    )


def activity_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🪑 Сидячий (мало движения)", callback_data="act:low")],
            [InlineKeyboardButton(text="🚶 Лёгкая (1–2 тр/нед)", callback_data="act:light")],
            [InlineKeyboardButton(text="🏃 Средняя (3–4 тр/нед)", callback_data="act:medium")],
            [InlineKeyboardButton(text="🏋️ Высокая (спорт почти каждый день)", callback_data="act:high")],
        ]
    )


def goal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Похудеть", callback_data="goal:lose")],
            [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal:maintain")],
            [InlineKeyboardButton(text="💪 Набрать массу", callback_data="goal:gain")],
        ]
    )


def build_router(config: Config, db: Database, analyzer: NutritionAnalyzer) -> Router:
    router = Router()

    async def start_onboarding(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(OnboardingStates.gender)
        await message.answer(
            "Давай за 30 секунд рассчитаем твою дневную норму КБЖУ.\n"
            "Шаг 1/6 — выбери свой пол:",
            reply_markup=gender_kb(),
        )

    async def ensure_terms_accepted(message: Message, user: dict) -> bool:
        if user.get("terms_accepted_at"):
            return True
        await message.answer(get_short_terms_notice(), reply_markup=terms_accept_kb())
        return False

    @router.message(Command("terms"))
    async def cmd_terms(message: Message) -> None:
        await message.answer(get_full_terms_text(config))

    @router.callback_query(F.data == "show_full_terms")
    async def cb_show_full_terms(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(get_full_terms_text(config))

    @router.callback_query(F.data == "accept_terms")
    async def cb_accept_terms(callback: CallbackQuery, state: FSMContext) -> None:
        user = await db.ensure_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.first_name,
        )
        await db.accept_terms(user["user_id"])
        await callback.answer("Условия приняты")
        try:
            await callback.message.edit_text("✅ Условия публичной оферты приняты.")
        except Exception:
            pass

        if not user["onboarded"]:
            await start_onboarding(callback.message, state)
        else:
            await callback.message.answer(
                "Отправь <b>фото еды</b>, <b>голосовое</b> или <b>текст</b> — я сразу посчитаю КБЖУ.",
                reply_markup=main_menu_kb(),
            )

    async def notify_if_new_user(bot: Bot, user: dict) -> None:
        if not user.get("is_new") or not config.admin_ids:
            return
        if user["user_id"] in config.admin_ids:
            return

        total = await db.get_total_users_count()
        first_name = html.escape(user.get("first_name") or "Без имени")
        username = user.get("username")
        handle = f"@{html.escape(username)}" if username else f'<a href="tg://user?id={user["user_id"]}">профиль</a>'
        source = html.escape(user.get("source") or "прямой вход")

        text = (
            f"🆕 <b>Новый пользователь #{total}</b>\n"
            f"👤 {first_name} ({handle})\n"
            f"🆔 ID: <code>{user['user_id']}</code>\n"
            f"🔗 Источник: <b>{source}</b>"
        )
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception as exc:
                logger.error("Failed to notify admin %s about new user %s: %s", admin_id, user["user_id"], exc)

    @router.message(CommandStart())
    async def cmd_start(
        message: Message,
        command: CommandObject,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        await state.clear()
        source = (command.args or "").strip() if command else ""
        user = await db.ensure_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            source=source or None,
        )
        await notify_if_new_user(bot, user)
        if not user.get("terms_accepted_at"):
            await message.answer(get_short_terms_notice(), reply_markup=terms_accept_kb())
            return

        if not user["onboarded"]:
            await start_onboarding(message, state)
            return

        await message.answer(
            "👋 С возвращением!\n\n"
            "Просто отправь <b>фото еды</b>, <b>голосовое</b> или <b>текст</b> (например: <i>«200 г творога 5% и банан»</i>) — я посчитаю КБЖУ.",
            reply_markup=main_menu_kb(),
        )

    @router.callback_query(F.data == "recalc_profile")
    async def cb_recalc_profile(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await start_onboarding(callback.message, state)

    @router.callback_query(OnboardingStates.gender, F.data.startswith("gender:"))
    async def on_gender(callback: CallbackQuery, state: FSMContext) -> None:
        gender = callback.data.split(":", 1)[1]
        await state.update_data(gender=gender)
        await state.set_state(OnboardingStates.age)
        await callback.answer()
        await callback.message.edit_text("Шаг 2/6 — сколько тебе полных лет? (Напиши число, например: 26)")

    @router.message(OnboardingStates.age)
    async def on_age(message: Message, state: FSMContext) -> None:
        try:
            age = int((message.text or "").strip())
            if not 12 <= age <= 100:
                raise ValueError
        except ValueError:
            await message.answer("Введи возраст числом от 12 до 100 (например: 27).")
            return
        await state.update_data(age=age)
        await state.set_state(OnboardingStates.height)
        await message.answer("Шаг 3/6 — твой рост в сантиметрах? (Например: 175)")

    @router.message(OnboardingStates.height)
    async def on_height(message: Message, state: FSMContext) -> None:
        try:
            height = float((message.text or "").strip().replace(",", "."))
            if not 100 <= height <= 240:
                raise ValueError
        except ValueError:
            await message.answer("Введи рост в сантиметрах от 100 до 240 (например: 172).")
            return
        await state.update_data(height_cm=height)
        await state.set_state(OnboardingStates.weight)
        await message.answer("Шаг 4/6 — твой текущий вес в килограммах? (Например: 74.5)")

    @router.message(OnboardingStates.weight)
    async def on_weight(message: Message, state: FSMContext) -> None:
        try:
            weight = float((message.text or "").strip().replace(",", "."))
            if not 30 <= weight <= 250:
                raise ValueError
        except ValueError:
            await message.answer("Введи вес в кг от 30 до 250 (например: 68).")
            return
        await state.update_data(weight_kg=weight)
        await state.set_state(OnboardingStates.activity)
        await message.answer("Шаг 5/6 — выбери уровень активности:", reply_markup=activity_kb())

    @router.callback_query(OnboardingStates.activity, F.data.startswith("act:"))
    async def on_activity(callback: CallbackQuery, state: FSMContext) -> None:
        activity = callback.data.split(":", 1)[1]
        await state.update_data(activity=activity)
        await state.set_state(OnboardingStates.goal)
        await callback.answer()
        await callback.message.edit_text("Шаг 6/6 — какая у тебя цель?", reply_markup=goal_kb())

    @router.callback_query(OnboardingStates.goal, F.data.startswith("goal:"))
    async def on_goal(callback: CallbackQuery, state: FSMContext) -> None:
        goal = callback.data.split(":", 1)[1]
        data = await state.get_data()
        targets = calculate_targets(
            gender=data["gender"],
            age=data["age"],
            height_cm=data["height_cm"],
            weight_kg=data["weight_kg"],
            activity=data["activity"],
            goal=goal,
        )
        await db.save_profile(
            user_id=callback.from_user.id,
            gender=data["gender"],
            age=data["age"],
            height_cm=data["height_cm"],
            weight_kg=data["weight_kg"],
            activity=data["activity"],
            goal=goal,
            target_kcal=targets.kcal,
            target_protein=targets.protein,
            target_fat=targets.fat,
            target_carbs=targets.carbs,
        )
        await state.clear()
        await callback.answer("Норма рассчитана!")
        await callback.message.edit_text(
            "✅ <b>Твоя ориентировочная дневная норма готова!</b>\n\n"
            f"🎯 Цель: <b>{GOAL_LABELS[goal]}</b>\n"
            f"🔥 Калории: <b>{targets.kcal} ккал</b>\n"
            f"🥩 Белки: <b>{targets.protein} г</b>\n"
            f"🥑 Жиры: <b>{targets.fat} г</b>\n"
            f"🍞 Углеводы: <b>{targets.carbs} г</b>\n\n"
            f"🎁 У тебя есть <b>{config.free_analyses_limit} бесплатных анализов</b>, чтобы попробовать бота.\n"
            "<i>Расчёты носят справочный характер и не заменяют консультацию врача.</i>"
        )
        await callback.message.answer(
            "Теперь просто отправь <b>фото еды</b>, <b>голосовое</b> или <b>текст</b> — я сразу всё посчитаю 👇",
            reply_markup=main_menu_kb(),
        )

    @router.message(F.text == MENU_TODAY)
    @router.message(Command("today"))
    async def show_today(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        meals = await db.get_today_meals(user["user_id"])
        total_kcal = sum(m["kcal"] for m in meals)
        total_p = sum(m["protein"] for m in meals)
        total_f = sum(m["fat"] for m in meals)
        total_c = sum(m["carbs"] for m in meals)

        target_kcal = user["target_kcal"]
        left_kcal = target_kcal - total_kcal
        status_line = (
            f"Осталось: <b>{left_kcal} ккал</b>"
            if left_kcal >= 0
            else f"Превышение на <b>{abs(left_kcal)} ккал</b>"
        )

        lines = [
            "📊 <b>Итоги за сегодня</b>",
            "",
            f"🔥 {progress_bar(total_kcal, target_kcal)} <b>{total_kcal} / {target_kcal} ккал</b>",
            status_line,
            "",
            f"🥩 Белки: <b>{total_p:.1f} / {user['target_protein']} г</b>",
            f"🥑 Жиры: <b>{total_f:.1f} / {user['target_fat']} г</b>",
            f"🍞 Углеводы: <b>{total_c:.1f} / {user['target_carbs']} г</b>",
        ]

        if meals:
            lines.extend(["", "<b>Приёмы пищи:</b>"])
            for m in meals:
                lines.append(f"• {m['local_time']} — {html.escape(m['meal_name'])} (<b>{m['kcal']} ккал</b>)")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Удалить последнюю запись", callback_data="del_last_meal")]
                ]
            )
        else:
            lines.extend(["", "<i>Сегодня ещё нет записей. Отправь фото, голосовое или текст с едой.</i>"])
            kb = None

        await message.answer("\n".join(lines), reply_markup=kb)

    @router.callback_query(F.data == "del_last_meal")
    async def cb_del_last_meal(callback: CallbackQuery) -> None:
        deleted = await db.delete_last_today_meal(callback.from_user.id)
        if not deleted:
            await callback.answer("Нечего удалять.", show_alert=True)
            return
        await callback.answer("Последняя запись удалена")
        await callback.message.answer(
            f"🗑 Удалена запись: <b>{html.escape(deleted['meal_name'])}</b> ({deleted['kcal']} ккал)."
        )

    @router.message(F.text == MENU_WEEK)
    @router.message(Command("week"))
    async def show_week(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        summary = await db.get_week_summary(user["user_id"])
        target = user["target_kcal"]

        lines = [f"📈 <b>Статистика за 7 дней</b> (цель: {target} ккал/день)", ""]
        logged_days = [d for d in summary if d["count"] > 0]
        for row in summary:
            day_label = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%d.%m")
            kcal = int(row["kcal"])
            if row["count"] == 0:
                icon = "▫️"
            elif kcal <= target * 1.1:
                icon = "✅"
            else:
                icon = "⬆️"
            lines.append(
                f"{icon} <b>{day_label}</b>: {kcal} ккал "
                f"(Б:{row['protein']:.0f} Ж:{row['fat']:.0f} У:{row['carbs']:.0f})"
            )

        if logged_days:
            avg_kcal = int(round(sum(d["kcal"] for d in logged_days) / len(logged_days)))
            lines.extend(["", f"Среднее в активные дни: <b>{avg_kcal} ккал</b>"])
        else:
            lines.extend(["", "<i>Пока нет данных за неделю.</i>"])

        await message.answer("\n".join(lines))

    @router.message(F.text == MENU_PROFILE)
    @router.message(Command("profile"))
    async def show_profile(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        if not user["onboarded"]:
            await start_onboarding(message, state)
            return

        gender_str = "Мужчина" if user["gender"] == "male" else "Женщина"
        text = (
            "⚙️ <b>Твой профиль</b>\n\n"
            f"Пол: {gender_str}\n"
            f"Возраст: {user['age']} лет\n"
            f"Рост / вес: {user['height_cm']:.0f} см / {user['weight_kg']:.1f} кг\n"
            f"Активность: {ACTIVITY_LABELS.get(user['activity'], '-')}\n"
            f"Цель: <b>{GOAL_LABELS.get(user['goal'], '-')}</b>\n\n"
            f"🔥 Норма: <b>{user['target_kcal']} ккал</b>\n"
            f"🥩 Б: <b>{user['target_protein']} г</b> | 🥑 Ж: <b>{user['target_fat']} г</b> | 🍞 У: <b>{user['target_carbs']} г</b>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Пересчитать норму", callback_data="recalc_profile")],
                [InlineKeyboardButton(text="📄 Публичная оферта", callback_data="show_full_terms")],
            ]
        )
        await message.answer(text, reply_markup=kb)

    @router.message(F.text == MENU_SUB)
    @router.message(Command("sub"))
    async def show_subscription(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        is_sub = db.is_subscribed(user)
        if is_sub:
            until_dt = datetime.fromisoformat(user["sub_until"]).astimezone()
            status_text = f"✅ <b>Подписка активна</b> до <b>{until_dt.strftime('%d.%m.%Y %H:%M')}</b>"
        else:
            left = max(0, config.free_analyses_limit - user["free_used"])
            status_text = (
                f"🆓 Бесплатный план: осталось <b>{left} из {config.free_analyses_limit}</b> анализов"
            )

        text = (
            "⭐ <b>Подписка Pro</b>\n\n"
            f"{status_text}\n\n"
            "Что даёт подписка:\n"
            "• Безлимитный анализ еды по фото, голосовым и тексту\n"
            "• Умное уточнение порций и ингредиентов\n"
            "• Полная статистика КБЖУ за день и неделю\n"
            "• Разовая оплата — <b>без автосписаний</b>\n\n"
            "Выбери удобный способ оплаты (оплачивая, ты соглашаешься с публичной офертой):"
        )
        await message.answer(text, reply_markup=subscription_kb(config))

    # ---------------- Telegram Stars payment ----------------
    @router.callback_query(F.data.startswith("buy:"))
    async def cb_buy_subscription(callback: CallbackQuery, bot: Bot) -> None:
        plan = callback.data.split(":", 1)[1]
        if plan == "1m":
            stars = config.sub_price_1m_stars
            title = "Подписка Pro — 1 месяц"
            description = "Безлимитный учёт калорий и БЖУ по фото, голосу и тексту на 30 дней."
        elif plan == "3m":
            stars = config.sub_price_3m_stars
            title = "Подписка Pro — 3 месяца"
            description = "Безлимитный учёт калорий и БЖУ по фото, голосу и тексту на 90 дней."
        else:
            await callback.answer("Неизвестный тариф", show_alert=True)
            return

        await callback.answer()
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=title,
            description=description,
            payload=f"sub:{plan}:{callback.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
        )

    @router.pre_checkout_query()
    async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
        await pre_checkout_query.answer(ok=True)

    @router.message(F.successful_payment)
    async def on_successful_payment(message: Message) -> None:
        payment = message.successful_payment
        payload = payment.invoice_payload or ""
        days = 90 if payload.startswith("sub:3m:") else 30

        new_until = await db.grant_subscription(
            user_id=message.from_user.id,
            days=days,
            amount_stars=payment.total_amount,
            charge_id=payment.telegram_payment_charge_id,
            provider="stars",
        )
        local_until = new_until.astimezone().strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"🎉 <b>Спасибо за оплату!</b>\n\n"
            f"Подписка Pro активирована до <b>{local_until}</b>.\n"
            "Отправляй фото, голосовые или текст с едой без ограничений!",
            reply_markup=main_menu_kb(),
        )

    # ---------------- Kaspi manual/semi-auto payment ----------------
    @router.callback_query(F.data.startswith("kaspi:"))
    async def cb_kaspi_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not config.kaspi_enabled:
            await callback.answer("Оплата через Kaspi временно отключена.", show_alert=True)
            return

        plan = callback.data.split(":", 1)[1]
        if plan == "1m":
            days = 30
            amount_kzt = config.kaspi_price_1m_kzt
            plan_title = "1 месяц (30 дней)"
        elif plan == "3m":
            days = 90
            amount_kzt = config.kaspi_price_3m_kzt
            plan_title = "3 месяца (90 дней)"
        else:
            await callback.answer("Неизвестный тариф", show_alert=True)
            return

        await state.set_state(KaspiStates.waiting_receipt)
        await state.update_data(plan=plan, days=days, amount_kzt=amount_kzt)
        await callback.answer()

        amount_fmt = f"{amount_kzt:,}".replace(",", " ")
        lines = [
            f"🇰🇿 <b>Оплата через Kaspi — {plan_title}</b>",
            f"Сумма к оплате: <b>{amount_fmt} ₸</b>",
            "",
        ]
        if config.kaspi_pay_url:
            lines.append(f"🔗 Ссылка на оплату Kaspi Pay:\n{config.kaspi_pay_url}")
        if config.kaspi_phone:
            lines.append(f"📱 Номер Kaspi: <code>{html.escape(config.kaspi_phone)}</code>")
        if config.kaspi_recipient:
            lines.append(f"👤 Получатель: <b>{html.escape(config.kaspi_recipient)}</b>")

        lines.extend(
            [
                "",
                "<b>Как активировать подписку:</b>",
                "1. Оплати точную сумму через приложение Kaspi.kz.",
                "2. Отправь сюда в чат <b>PDF-чек или скриншот чека</b>.",
                "3. После проверки подписка активируется автоматически (обычно 5–15 минут).",
            ]
        )

        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="kaspi_cancel")]
            ]
        )
        await callback.message.answer("\n".join(lines), reply_markup=cancel_kb)

    @router.callback_query(F.data == "kaspi_cancel")
    async def cb_kaspi_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.edit_text("Оплата через Kaspi отменена.")

    @router.message(KaspiStates.waiting_receipt, F.photo | F.document)
    async def on_kaspi_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        await state.clear()

        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        plan = data["plan"]
        days = data["days"]
        amount_kzt = data["amount_kzt"]

        if message.photo:
            receipt_file_id = message.photo[-1].file_id
            receipt_type = "photo"
        else:
            receipt_file_id = message.document.file_id
            receipt_type = "document"

        order_id = await db.create_kaspi_order(
            user_id=user["user_id"],
            plan=plan,
            days=days,
            amount_kzt=amount_kzt,
            receipt_file_id=receipt_file_id,
            receipt_type=receipt_type,
        )

        amount_fmt = f"{amount_kzt:,}".replace(",", " ")
        await message.answer(
            f"✅ <b>Чек получен! Заявка #{order_id}</b>\n\n"
            f"Тариф: <b>{days} дней ({amount_fmt} ₸)</b>\n"
            "Заявка отправлена на проверку. Как только платёж подтвердится, бот сразу пришлёт уведомление.",
            reply_markup=main_menu_kb(),
        )

        if not config.admin_ids:
            logger.warning(
                "Kaspi order #%s created by user %s, but ADMIN_IDS is empty in .env!",
                order_id,
                user["user_id"],
            )
            return

        uname = f"@{user['username']}" if user.get("username") else "без username"
        admin_caption = (
            f"💳 <b>Новая оплата Kaspi — заявка #{order_id}</b>\n\n"
            f"👤 Пользователь: {html.escape(user.get('first_name') or '')} ({uname})\n"
            f"🆔 ID: <code>{user['user_id']}</code>\n"
            f"📦 Тариф: <b>{days} дней</b>\n"
            f"💰 Сумма: <b>{amount_fmt} ₸</b>"
        )
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Одобрить (+{days} дн)",
                        callback_data=f"kaspi_approve:{order_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"kaspi_reject:{order_id}",
                    ),
                ]
            ]
        )

        for admin_id in config.admin_ids:
            try:
                if receipt_type == "photo":
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=receipt_file_id,
                        caption=admin_caption,
                        reply_markup=admin_kb,
                    )
                else:
                    await bot.send_document(
                        chat_id=admin_id,
                        document=receipt_file_id,
                        caption=admin_caption,
                        reply_markup=admin_kb,
                    )
            except Exception as exc:
                logger.error("Failed to notify admin %s about order #%s: %s", admin_id, order_id, exc)

    @router.message(KaspiStates.waiting_receipt)
    async def on_kaspi_receipt_wrong_type(message: Message) -> None:
        await message.answer(
            "Отправь, пожалуйста, <b>скриншот (фото)</b> или <b>PDF-файл чека</b> из Kaspi.\n"
            "Если передумал — нажми «Отмена» выше или отправь /start."
        )

    @router.callback_query(F.data.startswith("kaspi_approve:"))
    async def cb_kaspi_approve(callback: CallbackQuery, bot: Bot) -> None:
        if config.admin_ids and callback.from_user.id not in config.admin_ids:
            await callback.answer("Нет прав.", show_alert=True)
            return

        order_id = int(callback.data.split(":", 1)[1])
        order = await db.resolve_kaspi_order(order_id, "approved")
        if not order:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return

        new_until = await db.grant_subscription(
            user_id=order["user_id"],
            days=order["days"],
            provider="kaspi",
            amount_kzt=order["amount_kzt"],
            charge_id=f"kaspi_order_{order_id}",
        )
        local_until = new_until.astimezone().strftime("%d.%m.%Y %H:%M")

        await callback.answer("Подписка выдана!")
        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + f"\n\n✅ <b>ОДОБРЕНО</b> (до {local_until})"
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"🎉 <b>Оплата по заявке #{order_id} подтверждена!</b>\n\n"
                    f"Подписка Pro активна до <b>{local_until}</b>.\n"
                    "Отправляй фото, голосовые или текст с едой без ограничений!"
                ),
                reply_markup=main_menu_kb(),
            )
        except Exception as exc:
            logger.error("Failed to notify user %s about approval: %s", order["user_id"], exc)

    @router.callback_query(F.data.startswith("kaspi_reject:"))
    async def cb_kaspi_reject(callback: CallbackQuery, bot: Bot) -> None:
        if config.admin_ids and callback.from_user.id not in config.admin_ids:
            await callback.answer("Нет прав.", show_alert=True)
            return

        order_id = int(callback.data.split(":", 1)[1])
        order = await db.resolve_kaspi_order(order_id, "rejected")
        if not order:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return

        await callback.answer("Заявка отклонена.")
        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n❌ <b>ОТКЛОНЕНО</b>"
            )
        except Exception:
            pass

        support_note = f" или напиши в поддержку: {config.support_contact}" if config.support_contact else ""
        try:
            await bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"❌ <b>Не удалось подтвердить чек по заявке #{order_id}.</b>\n\n"
                    f"Проверь сумму и реквизиты и отправь чек повторно через меню «⭐ Подписка»{support_note}."
                ),
            )
        except Exception as exc:
            logger.error("Failed to notify user %s about rejection: %s", order["user_id"], exc)

    # ---------------- Admin stats ----------------
    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        uid = message.from_user.id
        if config.admin_ids and uid not in config.admin_ids:
            return
        stats = await db.get_admin_stats()
        kzt_fmt = f"{stats['total_kzt']:,}".replace(",", " ")
        admin_hint = (
            f"\n\n<i>⚠️ Добавь ADMIN_IDS={uid} в .env и перезапусти бота, чтобы получать чеки Kaspi сюда и скрыть /stats от других.</i>"
            if not config.admin_ids
            else ""
        )
        await message.answer(
            "📈 <b>Статистика бота</b>\n\n"
            f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
            f"⭐ Активных подписок: <b>{stats['active_subs']}</b>\n"
            f"🍽 Записано приёмов пищи: <b>{stats['total_meals']}</b>\n"
            f"💰 Telegram Stars: <b>{stats['total_stars']} ⭐</b>\n"
            f"🇰🇿 Kaspi: <b>{kzt_fmt} ₸</b> (ожидают проверки: {stats['pending_kaspi']})"
            f"{admin_hint}"
        )

    @router.message(Command("users"))
    async def cmd_users(message: Message) -> None:
        uid = message.from_user.id
        if config.admin_ids and uid not in config.admin_ids:
            return
        users = await db.get_recent_users(limit=15)
        if not users:
            await message.answer("Пока нет пользователей.")
            return

        lines = ["👥 <b>Последние 15 пользователей:</b>", ""]
        for idx, u in enumerate(users, start=1):
            name = html.escape(u.get("first_name") or "Без имени")
            uname = f"@{html.escape(u['username'])}" if u.get("username") else f"id:{u['user_id']}"
            src = f" [{html.escape(u['source'])}]" if u.get("source") else ""
            sub_mark = " ⭐" if db.is_subscribed(u) else ""
            try:
                dt_str = datetime.fromisoformat(u["created_at"]).astimezone().strftime("%d.%m %H:%M")
            except ValueError:
                dt_str = ""
            lines.append(
                f"{idx}. <b>{name}</b> ({uname}){sub_mark}{src} — {u['free_used']} ан. | {dt_str}"
            )
        await message.answer("\n".join(lines))

    # ---------------- Food analysis & logging ----------------
    async def handle_analysis_result(
        message: Message,
        user: dict,
        analysis: MealAnalysis,
        source_type: str,
        charge_free_quota: bool,
    ) -> None:
        if not analysis.is_food:
            await message.answer(
                f"🤔 {analysis.error_message or 'Не похоже на еду. Отправь фото блюда, голосовое или описание приёма пищи.'}"
            )
            return

        is_sub = db.is_subscribed(user)
        if charge_free_quota and not is_sub:
            await db.increment_free_used(user["user_id"])
            user["free_used"] += 1

        items_dicts = [item.model_dump() for item in analysis.items]
        pending_id = await db.create_pending_meal(
            user_id=user["user_id"],
            meal_name=analysis.meal_name,
            items=items_dicts,
            kcal=analysis.total_kcal,
            protein=analysis.total_protein,
            fat=analysis.total_fat,
            carbs=analysis.total_carbs,
            short_tip=analysis.short_tip,
            source_type=source_type,
        )

        text = format_meal_preview(
            meal_name=analysis.meal_name,
            items=items_dicts,
            total_kcal=analysis.total_kcal,
            total_protein=analysis.total_protein,
            total_fat=analysis.total_fat,
            total_carbs=analysis.total_carbs,
            short_tip=analysis.short_tip,
        )

        if not is_sub:
            left = max(0, config.free_analyses_limit - user["free_used"])
            text += f"\n\n🆓 Осталось бесплатных анализов: <b>{left}</b>"

        await message.answer(text, reply_markup=pending_meal_kb(pending_id))

    @router.callback_query(F.data.startswith("meal_save:"))
    async def cb_meal_save(callback: CallbackQuery) -> None:
        pending_id = int(callback.data.split(":", 1)[1])
        saved = await db.commit_pending_meal(pending_id, callback.from_user.id)
        if not saved:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            return

        user = await db.get_user(callback.from_user.id)
        today_meals = await db.get_today_meals(callback.from_user.id)
        total_kcal = sum(m["kcal"] for m in today_meals)
        target_kcal = user["target_kcal"] if user else 2000
        left = target_kcal - total_kcal
        left_str = f"осталось {left} ккал" if left >= 0 else f"превышение на {abs(left)} ккал"

        await callback.answer("Записано в дневник!")
        await callback.message.edit_text(
            f"✅ <b>Записано:</b> {html.escape(saved['meal_name'])} (+{saved['kcal']} ккал)\n\n"
            f"🔥 Сегодня: <b>{total_kcal} / {target_kcal} ккал</b> ({left_str})\n"
            f"{progress_bar(total_kcal, target_kcal)}"
        )

    @router.callback_query(F.data.startswith("meal_cancel:"))
    async def cb_meal_cancel(callback: CallbackQuery) -> None:
        pending_id = int(callback.data.split(":", 1)[1])
        await db.delete_pending_meal(pending_id, callback.from_user.id)
        await callback.answer("Отменено")
        await callback.message.edit_text("🗑 Запись отменена.")

    @router.callback_query(F.data.startswith("meal_edit:"))
    async def cb_meal_edit(callback: CallbackQuery, state: FSMContext) -> None:
        pending_id = int(callback.data.split(":", 1)[1])
        pending = await db.get_pending_meal(pending_id, callback.from_user.id)
        if not pending:
            await callback.answer("Запись уже неактуальна.", show_alert=True)
            return
        await state.set_state(CorrectionStates.waiting_correction)
        await state.update_data(pending_id=pending_id)
        await callback.answer()
        await callback.message.answer(
            f"✏️ Что поправить в <b>{html.escape(pending['meal_name'])}</b>?\n"
            "Напиши текстом или отправь голосовое (например: <i>«там было 150 г риса и без масла»</i>)."
        )

    async def download_file_bytes(bot: Bot, file_id: str) -> bytes:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        return buf.getvalue()

    @router.message(CorrectionStates.waiting_correction, F.text | F.voice | F.audio)
    async def on_meal_correction(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        pending_id = data.get("pending_id")
        await state.clear()

        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        pending = await db.get_pending_meal(pending_id, user["user_id"])
        if not pending:
            await message.answer("Не нашёл исходную запись. Отправь блюдо заново.")
            return

        wait_msg = await message.answer("⏳ Пересчитываю...")
        try:
            audio_bytes: Optional[bytes] = None
            audio_mime = "audio/ogg"
            correction_text = message.text or ""
            if message.voice:
                audio_bytes = await download_file_bytes(bot, message.voice.file_id)
                audio_mime = message.voice.mime_type or "audio/ogg"
            elif message.audio:
                audio_bytes = await download_file_bytes(bot, message.audio.file_id)
                audio_mime = message.audio.mime_type or "audio/mpeg"

            analysis = await asyncio.to_thread(
                analyzer.refine_meal,
                previous_meal={
                    "meal_name": pending["meal_name"],
                    "items": pending["items"],
                    "total_kcal": pending["kcal"],
                    "total_protein": pending["protein"],
                    "total_fat": pending["fat"],
                    "total_carbs": pending["carbs"],
                },
                correction_text=correction_text,
                audio_bytes=audio_bytes,
                audio_mime=audio_mime,
            )
            await db.delete_pending_meal(pending_id, user["user_id"])
            await wait_msg.delete()
            await handle_analysis_result(
                message=message,
                user=user,
                analysis=analysis,
                source_type=pending["source_type"],
                charge_free_quota=False,
            )
        except Exception as exc:
            logger.exception("Correction failed: %s", exc)
            await wait_msg.edit_text("⚠️ Не удалось пересчитать блюдо. Попробуй ещё раз.")

    async def ensure_quota_or_paywall(message: Message, user: dict) -> bool:
        if db.is_subscribed(user):
            return True
        if user["free_used"] < config.free_analyses_limit:
            return True

        await message.answer(
            "🔒 <b>Бесплатные анализы закончились</b>\n\n"
            f"Ты уже использовал все {config.free_analyses_limit} бесплатных разбора.\n"
            "Оформи <b>Pro-подписку</b> (без автосписаний), чтобы считать калории без ограничений:",
            reply_markup=subscription_kb(config),
        )
        return False

    @router.message(StateFilter(None), F.photo)
    async def on_food_photo(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        await notify_if_new_user(bot, user)
        if not await ensure_terms_accepted(message, user):
            return
        if not await ensure_quota_or_paywall(message, user):
            return

        wait_msg = await message.answer("⏳ Смотрю на фото и считаю КБЖУ...")
        try:
            photo = message.photo[-1]
            image_bytes = await download_file_bytes(bot, photo.file_id)
            analysis = await asyncio.to_thread(
                analyzer.analyze_photo,
                image_bytes=image_bytes,
                caption=message.caption or "",
            )
            await wait_msg.delete()
            await handle_analysis_result(
                message=message,
                user=user,
                analysis=analysis,
                source_type="photo",
                charge_free_quota=True,
            )
        except Exception as exc:
            logger.exception("Photo analysis failed: %s", exc)
            await wait_msg.edit_text("⚠️ Не получилось разобрать фото. Попробуй отправить ещё раз.")

    @router.message(StateFilter(None), F.voice | F.audio)
    async def on_food_voice(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.clear()
        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        await notify_if_new_user(bot, user)
        if not await ensure_terms_accepted(message, user):
            return
        if not await ensure_quota_or_paywall(message, user):
            return

        wait_msg = await message.answer("⏳ Слушаю голосовое и считаю КБЖУ...")
        try:
            if message.voice:
                audio_bytes = await download_file_bytes(bot, message.voice.file_id)
                mime_type = message.voice.mime_type or "audio/ogg"
            else:
                audio_bytes = await download_file_bytes(bot, message.audio.file_id)
                mime_type = message.audio.mime_type or "audio/mpeg"

            analysis = await asyncio.to_thread(
                analyzer.analyze_audio,
                audio_bytes=audio_bytes,
                mime_type=mime_type,
            )
            await wait_msg.delete()
            await handle_analysis_result(
                message=message,
                user=user,
                analysis=analysis,
                source_type="voice",
                charge_free_quota=True,
            )
        except Exception as exc:
            logger.exception("Voice analysis failed: %s", exc)
            await wait_msg.edit_text("⚠️ Не удалось распознать голосовое. Попробуй записать ещё раз.")

    @router.message(OnboardingStates.gender)
    @router.message(OnboardingStates.activity)
    @router.message(OnboardingStates.goal)
    async def on_onboarding_button_expected(message: Message) -> None:
        await message.answer("Нажми, пожалуйста, на одну из кнопок выше ☝️")

    @router.message(StateFilter(None), F.text)
    async def on_food_text(message: Message, bot: Bot) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return

        user = await db.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        await notify_if_new_user(bot, user)
        if not await ensure_terms_accepted(message, user):
            return
        if not await ensure_quota_or_paywall(message, user):
            return

        wait_msg = await message.answer("⏳ Считаю КБЖУ...")
        try:
            analysis = await asyncio.to_thread(analyzer.analyze_text, text=text)
            await wait_msg.delete()
            await handle_analysis_result(
                message=message,
                user=user,
                analysis=analysis,
                source_type="text",
                charge_free_quota=True,
            )
        except Exception as exc:
            logger.exception("Text analysis failed: %s", exc)
            await wait_msg.edit_text("⚠️ Ошибка при расчёте. Попробуй чуть позже.")

    return router


async def _health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_webhook_server(bot: Bot, dp: Dispatcher, config: Config) -> None:
    secret_token = hashlib.sha256(config.bot_token.encode("utf-8")).hexdigest()[:32]
    webhook_path = "/webhook"
    full_webhook_url = f"{config.webhook_url}{webhook_path}"

    async def on_startup(_app: web.Application) -> None:
        await bot.set_webhook(
            url=full_webhook_url,
            secret_token=secret_token,
            drop_pending_updates=False,
        )
        logger.info("Webhook registered: %s", full_webhook_url)

    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/health", _health_handler)
    app.on_startup.append(on_startup)

    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=secret_token,
    )
    handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.port)
    await site.start()
    logger.info("HTTP webhook server listening on 0.0.0.0:%s", config.port)
    await asyncio.Event().wait()


async def run_polling_with_optional_health(bot: Bot, dp: Dispatcher, port: int) -> None:
    runner: Optional[web.AppRunner] = None
    if port > 0:
        app = web.Application()
        app.router.add_get("/", _health_handler)
        app.router.add_get("/health", _health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        logger.info("Healthcheck server listening on 0.0.0.0:%s", port)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        if runner is not None:
            await runner.cleanup()


async def main() -> None:
    config = load_config()
    db = Database(config.db_path, database_url=config.database_url)
    await db.init()

    analyzer = NutritionAnalyzer(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        fallback_model=config.gemini_fallback_model,
    )

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(build_router(config, db, analyzer))

    try:
        me = await bot.get_me()
        logger.info("Bot started: @%s (id=%s)", me.username, me.id)

        if config.port > 0 and config.webhook_url:
            await run_webhook_server(bot, dp, config)
        else:
            await run_polling_with_optional_health(bot, dp, config.port)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
