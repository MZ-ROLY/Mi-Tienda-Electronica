import json
import os
import unicodedata
from threading import Lock
from datetime import timedelta
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_firebase_token


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

FIREBASE_CONFIG_KEYS = {
    "apiKey": "FIREBASE_API_KEY",
    "authDomain": "FIREBASE_AUTH_DOMAIN",
    "projectId": "FIREBASE_PROJECT_ID",
    "storageBucket": "FIREBASE_STORAGE_BUCKET",
    "messagingSenderId": "FIREBASE_MESSAGING_SENDER_ID",
    "appId": "FIREBASE_APP_ID",
    "measurementId": "FIREBASE_MEASUREMENT_ID",
}
REQUIRED_FIREBASE_KEYS = tuple(
    key for key in FIREBASE_CONFIG_KEYS if key != "measurementId"
)


def get_firebase_config() -> dict[str, str]:
    config = {
        config_key: os.environ.get(env_key, "").strip()
        for config_key, env_key in FIREBASE_CONFIG_KEYS.items()
    }
    missing = [
        FIREBASE_CONFIG_KEYS[key]
        for key in REQUIRED_FIREBASE_KEYS
        if not config[key]
    ]
    if missing:
        raise RuntimeError(
            "Faltan variables de Firebase en .env: " + ", ".join(missing)
        )
    return {key: value for key, value in config.items() if value}


secret_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not secret_key:
    raise RuntimeError("Falta la variable privada FLASK_SECRET_KEY en .env.")

firebase_config = get_firebase_config()
project_id = firebase_config["projectId"]
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
    "catálogos, productos, ayuda adicional ni comandos. No menciones ni uses comandos "
    "de Telegram en este chat. No inventes "
    "productos, precios vigentes, existencias, garantías, envíos ni políticas. Los "
    "precios identificados como demo no son precios confirmados y no debes presentarlos "
    "como vigentes; si preguntan el precio, indica que deben confirmarlo con la tienda. "
    "No pidas contraseñas, datos de tarjetas ni otra información de pago.\n\n"
    "Si la respuesta menciona productos del catálogo, usa sus nombres exactamente "
    "como aparecen en los datos para que el chat pueda mostrar opciones para abrirlos.\n\n"
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

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.environ.get("FLASK_COOKIE_SECURE", "").lower() == "true"
        or os.environ.get("RENDER", "").lower() == "true"
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
)
socketio = SocketIO(app, async_mode="threading", max_http_buffer_size=64 * 1024)
socket_users: dict[str, str] = {}
conversation_history: dict[str, list[types.Content]] = {}
conversation_locks: dict[str, Lock] = {}
conversation_locks_guard = Lock()


def get_conversation_lock(user_id: str) -> Lock:
    with conversation_locks_guard:
        if user_id not in conversation_locks:
            conversation_locks[user_id] = Lock()
        return conversation_locks[user_id]


def get_authenticated_socket_user() -> str | None:
    user_id = session.get("firebase_uid")
    if (
        not isinstance(user_id, str)
        or not user_id
        or socket_users.get(request.sid) != user_id
    ):
        return None
    return user_id


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
    product_is_mentioned = False
    for product in store_info.get("products", []):
        keywords = product.get("keywords", [])
        if any(
            normalize_text(keyword) in normalized_response
            for keyword in keywords
            if isinstance(keyword, str)
        ):
            product_is_mentioned = True
            break
    return catalog_product_options() if product_is_mentioned else []


def process_chat_message(
    user_id: str,
    text: str,
) -> None:
    room = f"user:{user_id}"
    emit("chat_message_received", {"text": text}, room=room)

    if is_catalog_request(text):
        user_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=text)],
        )
        with get_conversation_lock(user_id):
            history = conversation_history.get(user_id, [])
            greeting = "¡Hola!" if not history else "¡Sí!"
            response_text = (
                f"{greeting} \U0001f44b Elige un catálogo para explorar nuestros productos."
            )
            assistant_message = types.Content(
                role="model",
                parts=[types.Part.from_text(text=response_text)],
            )
            conversation_history[user_id] = [
                *history,
                user_message,
                assistant_message,
            ][-MAX_HISTORY_MESSAGES:]
            emit(
                "chat_response",
                {"text": response_text, "products": catalog_product_options()},
                room=room,
            )
        return

    if gemini_client is None:
        app.logger.error("GEMINI_API_KEY no está configurada; no se puede responder.")
        emit(
            "chat_error",
            {"message": "El asistente no está configurado. Inténtalo más tarde."},
            room=room,
        )
        return

    user_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=text)],
    )
    with get_conversation_lock(user_id):
        history = conversation_history.get(user_id, [])
        try:
            response = gemini_client.models.generate_content(
                model=gemini_model,
                contents=[*history, user_message],
                config=types.GenerateContentConfig(
                    system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                ),
            )
        except genai_errors.APIError:
            app.logger.exception("Gemini no pudo generar una respuesta.")
            emit(
                "chat_error",
                {"message": "Gemini no pudo responder ahora. Inténtalo de nuevo."},
                room=room,
            )
            return

        response_text = response.text
        if not response_text or not response_text.strip():
            app.logger.error("Gemini devolvió una respuesta vacía.")
            emit(
                "chat_error",
                {"message": "Gemini no devolvió una respuesta. Inténtalo de nuevo."},
                room=room,
            )
            return

        response_text = response_text.strip()
        assistant_message = types.Content(
            role="model",
            parts=[types.Part.from_text(text=response_text)],
        )
        conversation_history[user_id] = [
            *history,
            user_message,
            assistant_message,
        ][-MAX_HISTORY_MESSAGES:]
        emit(
            "chat_response",
            {
                "text": response_text,
                "products": products_mentioned_in_response(response_text),
            },
            room=room,
        )


def render_page(filename: str) -> str:
    page = (BASE_DIR / filename).read_text(encoding="utf-8")
    return render_template_string(page, firebase_config=firebase_config)


@app.get("/")
def login_page():
    if session.get("firebase_uid"):
        return redirect(url_for("chat_page"))
    return render_page("login.html")


@app.get("/chat")
def chat_page():
    if not session.get("firebase_uid"):
        return redirect(url_for("login_page"))
    return render_page("chat.html")


@app.post("/session")
def create_session():
    authorization = request.headers.get("Authorization", "")
    scheme, _, id_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not id_token:
        return jsonify(error="Falta el token de autenticación de Firebase."), 401

    try:
        claims = verify_firebase_token(
            id_token,
            Request(),
            audience=project_id,
        )
    except (ValueError, GoogleAuthError):
        return jsonify(error="Firebase rechazó la sesión."), 401
    except OSError:
        app.logger.exception("No fue posible consultar los certificados de Firebase.")
        return jsonify(error="No se pudo verificar la sesión en este momento."), 503

    user_id = claims.get("user_id") or claims.get("sub")
    if not user_id:
        return jsonify(error="El token de Firebase no contiene un usuario válido."), 401

    session.clear()
    session.permanent = True
    session["firebase_uid"] = user_id
    session["firebase_email"] = claims.get("email", "")
    return jsonify(ok=True)


@app.post("/logout")
def logout():
    user_id = session.get("firebase_uid")
    if isinstance(user_id, str):
        conversation_history.pop(user_id, None)
        for sid, connected_user_id in tuple(socket_users.items()):
            if connected_user_id == user_id:
                socket_users.pop(sid, None)
                socketio.emit("session_expired", {}, to=sid)
    session.clear()
    return "", 204


@socketio.on("connect")
def connect_chat(_auth: dict[str, object] | None = None) -> bool | None:
    _ = _auth
    user_id = session.get("firebase_uid")
    if not isinstance(user_id, str) or not user_id:
        return False

    socket_users[request.sid] = user_id
    join_room(f"user:{user_id}")
    emit("socket_ready", {"connected": True}, to=request.sid)


@socketio.on("disconnect")
def disconnect_chat() -> None:
    socket_users.pop(request.sid, None)


@socketio.on("chat_message")
def receive_chat_message(payload: object) -> None:
    user_id = get_authenticated_socket_user()
    if user_id is None:
        return

    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        emit("chat_error", {"message": "El mensaje debe ser texto."}, to=request.sid)
        return

    text = text.strip()
    if not text:
        emit("chat_error", {"message": "Escribe un mensaje antes de enviarlo."}, to=request.sid)
        return
    if len(text) > 1000:
        emit("chat_error", {"message": "El mensaje supera el máximo de 1000 caracteres."}, to=request.sid)
        return

    process_chat_message(user_id, text)


@socketio.on("product_details")
def request_product_details(payload: object) -> None:
    user_id = get_authenticated_socket_user()
    if user_id is None:
        return

    product_id = payload.get("product_id") if isinstance(payload, dict) else None
    product = next(
        (
            product
            for product in store_info.get("products", [])
            if isinstance(product, dict) and product.get("id") == product_id
        ),
        None,
    )
    if product is None:
        emit(
            "chat_error",
            {"message": "No encontré ese producto en el catálogo."},
            to=request.sid,
        )
        return

    image = product.get("image")
    emit(
        "product_selected",
        {
            "id": product["id"],
            "name": product["name"],
            "option_label": product.get("option_label", product["name"]),
            "image": url_for(
                "image_asset",
                filename=image.split("/", 1)[1],
            )
            if isinstance(image, str) and "/" in image
            else "",
            "features": product.get("features", []),
            "demo_price": product.get("demo_price"),
            "price_status": product.get("price_status"),
        },
        room=f"user:{user_id}",
    )


@app.get("/image/<path:filename>")
def image_asset(filename: str):
    return send_from_directory(BASE_DIR / "image", filename)


@app.get("/animacion/<path:filename>")
def animation_asset(filename: str):
    return send_from_directory(BASE_DIR / "animacion", filename)


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
