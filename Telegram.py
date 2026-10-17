import os
from multiprocessing import Process
from pathlib import Path
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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

def simulacion_pago():
    print("Iniciando validación bancaria en Valve Store...")
    for intento in range(1, 5):
        print(f"Comprobando pago... {intento}")
        time.sleep(1)
    print("Simulación finalizada. No se realizó ningún cobro.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Bienvenido a Valve Store!\n\n"
        "Usa /ayuda para ver todos los comandos o /catalogo para ver los productos."
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "COMANDOS DE VALVE STORE\n\n"
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

if __name__ == "__main__":
    TOKEN = os.environ.get("TELEGRAM")

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

    print("Conectando con Telegram...", flush=True)
    app.run_polling()