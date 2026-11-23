import base64
import json
import logging
from typing import Any, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger("calorie_bot.ai")

SYSTEM_INSTRUCTION = (
    "Ты опытный нутрициолог и эксперт по оценке калорийности и БЖУ блюд. "
    "Отвечай строго на русском языке в формате JSON по заданной схеме. "
    "Если пользователь прислал еду (на фото, в голосовом сообщении или тексте), "
    "установи is_food=true, оцени реалистичный вес каждого продукта в граммах, "
    "посчитай калории, белки, жиры и углеводы по стандартным таблицам пищевой ценности. "
    "Сумма total_kcal, total_protein, total_fat, total_carbs должна соответствовать сумме items. "
    "Добавь короткий полезный совет (short_tip, 1 короткое предложение без воды). "
    "Если на фото, в голосе или тексте нет еды/напитков — установи is_food=false, "
    "пустой список items, нули в итогах и вежливое объяснение в error_message."
)


class FoodItem(BaseModel):
    name: str = Field(description="Название продукта или блюда на русском.")
    grams: int = Field(description="Оценочный вес порции в граммах.")
    kcal: int = Field(description="Калорийность порции в ккал.")
    protein: float = Field(description="Белки в граммах.")
    fat: float = Field(description="Жиры в граммах.")
    carbs: float = Field(description="Углеводы в граммах.")


class MealAnalysis(BaseModel):
    is_food: bool = Field(description="True, если во входных данных есть еда или напитки.")
    meal_name: str = Field(description="Короткое понятное название приёма пищи (например: 'Завтрак: омлет с тостом').")
    items: list[FoodItem] = Field(default_factory=list, description="Список распознанных продуктов.")
    total_kcal: int = Field(default=0, description="Общая калорийность, ккал.")
    total_protein: float = Field(default=0.0, description="Общие белки, г.")
    total_fat: float = Field(default=0.0, description="Общие жиры, г.")
    total_carbs: float = Field(default=0.0, description="Общие углеводы, г.")
    short_tip: Optional[str] = Field(default=None, description="Короткий практичный совет нутрициолога (1 предложение).")
    error_message: Optional[str] = Field(default=None, description="Причина, если is_food=false.")


class NutritionAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        fallback_model: str = "gemini-3.8-flash",
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=20000),
        )

    def _run_structured(self, input_payload: Any) -> MealAnalysis:
        models_to_try = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models_to_try.append(self.fallback_model)

        last_err: Optional[Exception] = None
        for idx, model_name in enumerate(models_to_try):
            try:
                interaction = self.client.interactions.create(
                    model=model_name,
                    system_instruction=SYSTEM_INSTRUCTION,
                    input=input_payload,
                    store=False,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": MealAnalysis.model_json_schema(),
                    },
                )
                raw = (interaction.output_text or "").strip()
                return MealAnalysis.model_validate_json(raw)
            except Exception as exc:
                last_err = exc
                logger.warning("Model %s failed (%s: %s)", model_name, type(exc).__name__, exc)
                if idx + 1 < len(models_to_try):
                    logger.info("Falling back to %s", models_to_try[idx + 1])

        assert last_err is not None
        raise last_err

    def analyze_text(self, text: str) -> MealAnalysis:
        prompt = f"Оцени калорийность и БЖУ следующего приёма пищи:\n{text}"
        return self._run_structured(prompt)

    def analyze_photo(self, image_bytes: bytes, caption: str = "") -> MealAnalysis:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        prompt = "Определи все блюда и продукты на этом фото, оцени вес порций и посчитай калории и БЖУ."
        if caption.strip():
            prompt += f"\nКомментарий пользователя: {caption.strip()}"
        return self._run_structured(
            [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "data": image_b64,
                    "mime_type": "image/jpeg",
                },
            ]
        )

    def analyze_audio(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> MealAnalysis:
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        return self._run_structured(
            [
                {
                    "type": "text",
                    "text": (
                        "Прослушай голосовое сообщение пользователя о том, что он съел или выпил. "
                        "Распознай все продукты, оцени порции и посчитай калории и БЖУ."
                    ),
                },
                {
                    "type": "audio",
                    "data": audio_b64,
                    "mime_type": mime_type,
                },
            ]
        )

    def refine_meal(
        self,
        previous_meal: dict[str, Any],
        correction_text: str = "",
        audio_bytes: Optional[bytes] = None,
        audio_mime: str = "audio/ogg",
    ) -> MealAnalysis:
        prev_json = json.dumps(previous_meal, ensure_ascii=False)
        base_prompt = (
            f"Исходный расчёт приёма пищи:\n{prev_json}\n\n"
            "Пользователь вносит уточнение. Пересчитай вес, калории и БЖУ с учётом поправки."
        )
        if audio_bytes:
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            return self._run_structured(
                [
                    {"type": "text", "text": base_prompt},
                    {"type": "audio", "data": audio_b64, "mime_type": audio_mime},
                ]
            )
        return self._run_structured(f"{base_prompt}\nУточнение: {correction_text}")
