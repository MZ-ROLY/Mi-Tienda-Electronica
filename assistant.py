import json
import os
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
gemini_client = genai.Client(api_key=gemini_api_key) if gemini_api_key else None

try:
    store_info = json.loads(
        (BASE_DIR / "catalogo_tienda.json").read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError) as error:
    raise RuntimeError("No se pudo cargar catalogo_tienda.json.") from error
if not isinstance(store_info, dict):
    raise RuntimeError("catalogo_tienda.json debe contener un objeto JSON.")

GEMINI_SYSTEM_INSTRUCTION = (
    "Eres el asistente de Valve Store. Responde en español, de forma amable y clara. "
    "Usa solo la información de la tienda incluida al final de estas instrucciones. "
    "Si el usuario solo saluda, responde únicamente con un saludo amable, sin ofrecer "
    "catálogos, productos ni ayuda adicional. No inventes productos, precios vigentes, "
    "existencias, garantías, envíos ni políticas. Los precios identificados como demo "
    "no son precios confirmados y no debes presentarlos como vigentes; si preguntan "
    "el precio, indica que deben confirmarlo con la tienda. No pidas contraseñas, "
    "datos de tarjetas ni otra información de pago.\n\n"
    "Si la respuesta menciona productos del catálogo, usa sus nombres exactamente "
    "como aparecen en los datos.\n\n"
    "Información disponible de Valve Store:\n"
    + json.dumps(
        {
            "store_name": store_info.get("store_name"),
            "products": [
                {
                    key: product[key]
                    for key in (
                        "name",
                        "keywords",
                        "features",
                        "demo_price",
                        "price_status",
                    )
                    if key in product
                }
                for product in store_info.get("products", [])
            ],
            "known_limits": store_info.get("known_limits", []),
        },
        ensure_ascii=False,
        indent=2,
    )
)
MAX_HISTORY_MESSAGES = 10


class AssistantNotConfiguredError(RuntimeError):
    pass


class AssistantResponseError(RuntimeError):
    pass


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(
        char if char.isalnum() or char.isspace() else " "
        for char in normalized
        if not unicodedata.combining(char)
    )
    return " ".join(normalized.split())


def is_catalog_request(text: str) -> bool:
    normalized_text = normalize_text(text)
    catalog_phrases = (
        "catalogo",
        "catalogos",
        "que productos",
        "cuales productos",
        "tienen productos",
        "hay productos",
        "mostrar productos",
        "muestrame productos",
        "lista de productos",
        "que venden",
    )
    return any(phrase in normalized_text for phrase in catalog_phrases)


def catalog_product_options() -> list[dict[str, str]]:
    return [
        {
            "id": product["id"],
            "name": product["name"],
            "option_label": product.get("option_label", product["name"]),
        }
        for product in store_info.get("products", [])
    ]


def products_mentioned_in_response(response_text: str) -> list[dict[str, str]]:
    normalized_response = normalize_text(response_text)
    product_is_mentioned = any(
        normalize_text(keyword) in normalized_response
        for product in store_info.get("products", [])
        for keyword in product.get("keywords", [])
        if isinstance(keyword, str)
    )
    return catalog_product_options() if product_is_mentioned else []


def generate_assistant_response(
    text: str,
    history: list[types.Content],
) -> tuple[str, list[types.Content]]:
    user_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=text)],
    )

    if is_catalog_request(text):
        greeting = "¡Hola!" if not history else "¡Sí!"
        response_text = (
            f"{greeting} \U0001f44b Elige un catálogo para explorar nuestros productos."
        )
    else:
        if gemini_client is None:
            raise AssistantNotConfiguredError(
                "GEMINI_API_KEY no está configurada."
            )
        response = gemini_client.models.generate_content(
            model=gemini_model,
            contents=[*history, user_message],
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_INSTRUCTION,
            ),
        )
        response_text = response.text
        if not response_text or not response_text.strip():
            raise AssistantResponseError("Gemini devolvió una respuesta vacía.")
        response_text = response_text.strip()

    assistant_message = types.Content(
        role="model",
        parts=[types.Part.from_text(text=response_text)],
    )
    updated_history = [
        *history,
        user_message,
        assistant_message,
    ][-MAX_HISTORY_MESSAGES:]
    return response_text, updated_history
