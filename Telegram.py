import asyncio
import logging
import os
from multiprocessing import Process
from pathlib import Path
from threading import Lock
import time

from dotenv import load_dotenv
from google.genai import errors as genai_errors
from google.genai import types
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from assistant import (
    AssistantNotConfiguredError,
    AssistantResponseError,
    catalog_product_options,
    generate_assistant_response,
    is_catalog_request,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger(__name__)

PRODUCTS = {
    "laptop_tuf": {
        "id": "laptop_tuf",
        "name": "Laptop TUF Gaming 16″ Ryzen 5, 16 GB RAM, 512 GB",
        "price": "Bs 4.500 (precio demo)",
        "image": "image/lap2.jpg",
        "audio": "audio/laptop.mp3",
    },
    "teclado_streamer": {
        "id": "teclado_streamer",
        "name": "Teclado RGB con teclas multimedia y pantalla",
        "price": "Bs 950 (precio demo)",
        "image": "image/tec2.jpg",
        "audio": "audio/teclado.mp3",
    },
    "monitor_rog": {
        "id": "monitor_rog",
        "name": "Monitor ASUS ROG OLED 26,5″ 240 Hz",
        "price": "Bs 6.500 (precio demo)",
        "image": "image/mon2.jpg",
        "audio": "audio/monitor.mp3",
    },
}
conversation_history: dict[int, list[types.Content]] = {}
conversation_locks: dict[int, asyncio.Lock] = {}
conversation_locks_guard = Lock()


def get_conversation_lock(user_id: int) -> asyncio.Lock:
    with conversation_locks_guard:
        if user_id not in conversation_locks:
            conversation_locks[user_id] = asyncio.Lock()
        return conversation_locks[user_id]

def simulacion_pago():
    print("Iniciando validación bancaria en Valve Store...")
    for intento in range(1, 5):
        print(f"Comprobando pago... {intento}")
        time.sleep(1)
    print("Simulación finalizada. No se realizó ningún cobro.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Bienvenido a Valve Store!\n\n"
        "Puedes preguntarme por nuestros productos o usar /ayuda para ver los comandos."
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "COMANDOS DE VALVE STORE\n\n"
        "También puedes escribirme normalmente para consultar el catálogo.\n\n"
        "1. /start - Iniciar el bot\n"
        "2. /ayuda - Lista completa de comandos\n"
        "3. /catalogo - Ver catálogo de productos\n"
        "4. /laptop_tuf - Ver imagen y precio de laptop\n"
        "5. /teclado_streamer - Ver imagen y precio de teclado\n"
        "6. /monitor_rog - Ver imagen y precio de monitor\n"
        "7. /comprar - Confirmación de compra\n"
        "8. /procesarpago - Ejecutar pago\n"
        "9. /comprobante - Descargar factura PDF\n"
        "10. /gracias - Escuchar audios"
    )

async def catalogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CATÁLOGO VALVE STORE\n\n"
        + "\n".join(
            f"{index}. /{product['id']} - {product['name']} ({product['price']})"
            for index, product in enumerate(PRODUCTS.values(), start=1)
        )
    )

async def comprar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CONFIRMACIÓN DE COMPRA\n\n"
        "Este bot solo ofrece una simulación y no procesa pagos reales.\n"
        "Para iniciar la simulación ejecuta:\n"
        "/procesarpago"
    )

async def procesar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proceso = Process(target=simulacion_pago)
    proceso.start()

    await update.message.reply_text(
        "Se inició la simulación de pago.\n"
        "No se realizó ningún cobro ni se confirmó una compra."
    )

async def enviar_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None or not message.text:
        raise ValueError("El comando del producto debe incluir un mensaje.")

    command = message.text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lstrip("/").lower()
    product = PRODUCTS.get(command)
    if product is None:
        raise ValueError(f"No existe el producto solicitado: {command}")

    context.user_data["producto_actual"] = product["id"]
    image_path = BASE_DIR / product["image"]
    with image_path.open("rb") as image:
        await message.reply_photo(
            photo=image,
            caption=f"{product['name']} - Precio: {product['price']} | Para comprar ejecuta: /comprar",
        )

async def enviar_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_path = BASE_DIR / "pdfs" / "factura.pdf"
    with pdf_path.open("rb") as pdf:
        await update.message.reply_document(
            document=pdf,
            filename=pdf_path.name,
            caption=(
                "Documento PDF de demostración; no acredita un pago real. "
                "Para escuchar el audio de Valve Store ejecuta: /gracias"
            ),
        )

async def enviar_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product = PRODUCTS.get(context.user_data.get("producto_actual"), PRODUCTS["laptop_tuf"])
    audio_path = BASE_DIR / product["audio"]
    with audio_path.open("rb") as audio:
        await update.message.reply_audio(
            audio=audio,
            caption=(
                f"¡Gracias por visitar Valve Store! Aquí tienes información sobre "
                f"{product['name']}.\n\nPara seguir explorando ejecuta: /catalogo"
            ),
        )


async def responder_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return

    text = message.text.strip()
    if not text:
        await message.reply_text("Escribe un mensaje para consultar al asistente.")
        return
    if len(text) > 1000:
        await message.reply_text("El mensaje supera el máximo de 1000 caracteres.")
        return

    async with get_conversation_lock(user.id):
        history = conversation_history.get(user.id, [])
        try:
            response_text, updated_history = await asyncio.to_thread(
                generate_assistant_response,
                text,
                history,
            )
        except AssistantNotConfiguredError:
            await message.reply_text(
                "El asistente no está configurado. Inténtalo más tarde."
            )
            return
        except genai_errors.APIError:
            logger.exception("Gemini no pudo generar una respuesta para Telegram.")
            await message.reply_text(
                "El asistente no pudo responder ahora. Inténtalo de nuevo."
            )
            return
        except AssistantResponseError:
            logger.error("Gemini devolvió una respuesta vacía para Telegram.")
            await message.reply_text(
                "El asistente no devolvió una respuesta. Inténtalo de nuevo."
            )
            return

        conversation_history[user.id] = updated_history

    if is_catalog_request(text):
        product_commands = "\n".join(
            f"- {product['option_label']}: {product['name']} (/{product['id']})"
            for product in catalog_product_options()
        )
        response_text = f"{response_text}\n\n{product_commands}"

    for start_index in range(0, len(response_text), 3500):
        await message.reply_text(response_text[start_index : start_index + 3500])


if __name__ == "__main__":
    TOKEN = os.environ.get("TELEGRAM", "").strip()
    if not TOKEN:
        raise RuntimeError(
            "Falta la variable TELEGRAM. Configura el token del bot en el entorno."
        )

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("catalogo", catalogo))
    for product_id in PRODUCTS:
        app.add_handler(CommandHandler(product_id, enviar_producto))
    app.add_handler(CommandHandler("comprar", comprar))
    app.add_handler(CommandHandler("procesarpago", procesar_pago))
    app.add_handler(CommandHandler("comprobante", enviar_comprobante))
    app.add_handler(CommandHandler("gracias", enviar_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder_chat))

    print("Conectando con Telegram...", flush=True)
    app.run_polling()