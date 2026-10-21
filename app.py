import os
from pathlib import Path
from threading import Lock
from datetime import timedelta

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

from assistant import (
    AssistantNotConfiguredError,
    AssistantResponseError,
    catalog_product_options,
    generate_assistant_response,
    is_catalog_request,
    products_mentioned_in_response,
    store_info,
)


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
selected_products: dict[str, str] = {}

STORE_HELP_TEXT = (
        "COMANDOS DE VALVE STORE\n\n"
        "1. /start - Iniciar el asistente y mostrar este menú\n"
        "2. /ayuda - Ver la lista completa de comandos\n"
        "3. /catalogo - Explorar el catálogo de productos\n"
        "4. /laptop_tuf - Ver detalles de la laptop TUF\n"
        "5. /teclado_streamer - Ver detalles del teclado RGB\n"
        "6. /monitor_rog - Ver detalles del monitor ASUS ROG\n"
        "7. /comprar - Ver la confirmación de compra de demostración\n"
        "8. /procesarpago - Ejecutar la simulación de pago (sin cobro real)\n"
        "9. /comprobante - Descargar la factura PDF de demostración\n"
        "10. /gracias - Escuchar el audio del producto seleccionado\n\n"
        "También puedes escribir una pregunta normal para consultar al asistente."
)

STORE_COMMAND_ACTIONS = [
    {"label": "Catálogo", "command": "/catalogo"},
    {"label": "Laptop TUF", "command": "/laptop_tuf"},
    {"label": "Teclado RGB", "command": "/teclado_streamer"},
    {"label": "Monitor ROG", "command": "/monitor_rog"},
    {"label": "Comprar", "command": "/comprar"},
    {"label": "Simular pago", "command": "/procesarpago"},
    {"label": "Comprobante demo", "command": "/comprobante"},
    {"label": "Escuchar audio", "command": "/gracias"},
]

STORE_COMMANDS = {
    "/start": f"¡Bienvenido a Valve Store!\n\n{STORE_HELP_TEXT}",
    "/ayuda": STORE_HELP_TEXT,
    "/comprar": (
        "CONFIRMACIÓN DE COMPRA\n\n"
        "Esta tienda solo ofrece una simulación y no procesa pagos reales.\n"
        "Para iniciar la simulación ejecuta /procesarpago."
    ),
}


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


def process_chat_message(
    user_id: str,
    text: str,
) -> None:
    room = f"user:{user_id}"
    emit("chat_message_received", {"text": text}, room=room)

    with get_conversation_lock(user_id):
        history = conversation_history.get(user_id, [])
        try:
            response_text, updated_history = generate_assistant_response(
                text,
                history,
            )
        except AssistantNotConfiguredError:
            app.logger.error("GEMINI_API_KEY no está configurada; no se puede responder.")
            emit(
                "chat_error",
                {"message": "El asistente no está configurado. Inténtalo más tarde."},
                room=room,
            )
            return
        except genai_errors.APIError:
            app.logger.exception("Gemini no pudo generar una respuesta.")
            emit(
                "chat_error",
                {"message": "Gemini no pudo responder ahora. Inténtalo de nuevo."},
                room=room,
            )
            return
        except AssistantResponseError:
            app.logger.error("Gemini devolvió una respuesta vacía.")
            emit(
                "chat_error",
                {"message": "Gemini no devolvió una respuesta. Inténtalo de nuevo."},
                room=room,
            )
            return

        conversation_history[user_id] = updated_history
        emit(
            "chat_response",
            {
                "text": response_text,
                "products": (
                    catalog_product_options()
                    if is_catalog_request(text)
                    else products_mentioned_in_response(response_text)
                ),
            },
            room=room,
        )


def product_details_payload(user_id: str, product: dict[str, object]) -> dict[str, object]:
    image = product.get("image")
    audio = product.get("audio")
    selected_products[user_id] = str(product["id"])
    return {
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
        "audio": url_for("audio_asset", filename=Path(audio).name)
        if isinstance(audio, str)
        else "",
    }


def send_store_command(user_id: str, command: str) -> None:
    room = f"user:{user_id}"
    emit("chat_message_received", {"text": command}, room=room)

    product = next(
        (
            product
            for product in store_info.get("products", [])
            if isinstance(product, dict)
            and product.get("telegram_command") == command
        ),
        None,
    )
    if product is not None:
        emit(
            "product_selected",
            product_details_payload(user_id, product),
            room=room,
        )
        return

    if command == "/catalogo":
        emit(
            "chat_response",
            {
                "text": "CATÁLOGO VALVE STORE\n\nElige un producto para ver sus detalles.",
                "products": catalog_product_options(),
            },
            room=room,
        )
        return

    if command in {"/start", "/ayuda", "/comprar"}:
        response = {"text": STORE_COMMANDS[command]}
        if command in {"/start", "/ayuda"}:
            response["actions"] = STORE_COMMAND_ACTIONS
        if command == "/comprar":
            response["actions"] = [
                {"label": "Simular pago", "command": "/procesarpago"},
                {
                    "label": "Comprobante de demostración",
                    "command": "/comprobante",
                },
            ]
        emit("chat_response", response, room=room)
        return

    if command == "/procesarpago":
        emit(
            "chat_response",
            {
                "text": (
                    "Se inició la simulación de pago. No se realizó ningún cobro "
                    "ni se confirmó una compra."
                )
            },
            room=room,
        )
        socketio.start_background_task(simulate_payment, user_id)
        return

    if command == "/comprobante":
        emit(
            "chat_response",
            {
                "text": (
                    "Documento PDF de demostración; no acredita un pago real."
                ),
                "attachment": {
                    "url": url_for("download_invoice"),
                    "label": "Descargar comprobante de demostración (PDF)",
                },
            },
            room=room,
        )
        return

    if command == "/gracias":
        product_id = selected_products.get(user_id, "laptop_tuf")
        selected_product = next(
            (
                product
                for product in store_info.get("products", [])
                if isinstance(product, dict) and product.get("id") == product_id
            ),
            None,
        )
        audio = selected_product.get("audio") if selected_product else None
        if not isinstance(audio, str):
            app.logger.error("No hay un audio configurado para el producto %s.", product_id)
            emit(
                "chat_error",
                {"message": "No se encontró el audio de este producto."},
                room=room,
            )
            return

        emit(
            "chat_response",
            {
                "text": (
                    "¡Gracias por visitar Valve Store! Aquí tienes información "
                    f"sobre {selected_product['name']}."
                ),
                "audio": {
                    "url": url_for("audio_asset", filename=Path(audio).name),
                    "label": f"Audio de {selected_product['name']}",
                },
            },
            room=room,
        )
        return

    emit(
        "chat_error",
        {"message": "No reconozco ese comando. Escribe /ayuda para ver las opciones."},
        room=room,
    )


def simulate_payment(user_id: str) -> None:
    room = f"user:{user_id}"
    for attempt in range(1, 5):
        socketio.sleep(1)
        socketio.emit(
            "chat_response",
            {"text": f"Simulación de pago... {attempt}/4"},
            to=room,
        )
    socketio.emit(
        "chat_response",
        {"text": "Simulación finalizada. No se realizó ningún cobro."},
        to=room,
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
        selected_products.pop(user_id, None)
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

    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        send_store_command(user_id, command)
        return

    process_chat_message(user_id, text)


@socketio.on("store_command")
def receive_store_command(payload: object) -> None:
    user_id = get_authenticated_socket_user()
    if user_id is None:
        return

    command = payload.get("command") if isinstance(payload, dict) else None
    if not isinstance(command, str) or len(command) > 40:
        emit("chat_error", {"message": "El comando no es válido."}, to=request.sid)
        return

    send_store_command(user_id, command.lower())


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

    emit(
        "product_selected",
        product_details_payload(user_id, product),
        room=f"user:{user_id}",
    )

@app.get("/comprobante")
def download_invoice():
    if not session.get("firebase_uid"):
        return redirect(url_for("login_page"))
    return send_from_directory(
        BASE_DIR / "pdfs",
        "factura.pdf",
        as_attachment=True,
        download_name="factura.pdf",
    )


@app.get("/audio/<path:filename>")
def audio_asset(filename: str):
    if not session.get("firebase_uid"):
        return redirect(url_for("login_page"))
    allowed_audio = {
        Path(product["audio"]).name
        for product in store_info.get("products", [])
        if isinstance(product, dict) and isinstance(product.get("audio"), str)
    }
    if filename not in allowed_audio:
        return jsonify(error="No se encontró el audio solicitado."), 404
    return send_from_directory(BASE_DIR / "audio", filename)


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
