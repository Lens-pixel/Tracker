from dataclasses import dataclass
from typing import Iterable


ACTIVITY_MULTIPLIERS = {
    "low": 1.2,
    "light": 1.375,
    "medium": 1.55,
    "high": 1.725,
}

ACTIVITY_LABELS = {
    "low": "Сидячий (офис, мало шагов)",
    "light": "Лёгкая (1–2 тренировки в неделю)",
    "medium": "Средняя (3–4 тренировки в неделю)",
    "high": "Высокая (спорт почти каждый день)",
}

GOAL_LABELS = {
    "lose": "Похудение",
    "maintain": "Поддержание веса",
    "gain": "Набор массы",
}


@dataclass(frozen=True)
class DailyTargets:
    kcal: int
    protein: int
    fat: int
    carbs: int


def calculate_targets(
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity: str,
    goal: str,
) -> DailyTargets:
    # Mifflin-St Jeor formula
    if gender == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    tdee = bmr * ACTIVITY_MULTIPLIERS.get(activity, 1.375)

    if goal == "lose":
        kcal = max(1200, int(round(tdee * 0.82)))
        protein = int(round(weight_kg * 1.8))
        fat = int(round(weight_kg * 0.85))
    elif goal == "gain":
        kcal = int(round(tdee * 1.12))
        protein = int(round(weight_kg * 1.8))
        fat = int(round(weight_kg * 0.95))
    else:
        kcal = int(round(tdee))
        protein = int(round(weight_kg * 1.6))
        fat = int(round(weight_kg * 0.9))

    used_kcal = protein * 4 + fat * 9
    carbs = max(50, int(round((kcal - used_kcal) / 4)))

    return DailyTargets(kcal=kcal, protein=protein, fat=fat, carbs=carbs)


def progress_bar(current: float, target: float, length: int = 10) -> str:
    if target <= 0:
        return "░" * length
    ratio = min(max(current / target, 0.0), 1.0)
    filled = int(round(ratio * length))
    return "█" * filled + "░" * (length - filled)


def format_meal_preview(
    meal_name: str,
    items: Iterable[dict],
    total_kcal: int,
    total_protein: float,
    total_fat: float,
    total_carbs: float,
    short_tip: str | None = None,
) -> str:
    lines = [f"🍽 <b>{meal_name}</b>", ""]
    for item in items:
        name = item.get("name", "Продукт")
        grams = item.get("grams", 0)
        kcal = item.get("kcal", 0)
        lines.append(f"• {name} (~{grams} г) — <b>{kcal} ккал</b>")

    lines.extend(
        [
            "",
            f"🔥 Итого: <b>{total_kcal} ккал</b>",
            f"🥩 Белки: <b>{total_protein:.1f} г</b>  |  🥑 Жиры: <b>{total_fat:.1f} г</b>  |  🍞 Углеводы: <b>{total_carbs:.1f} г</b>",
        ]
    )
    if short_tip:
        lines.extend(["", f"💡 <i>{short_tip}</i>"])
    return "\n".join(lines)
