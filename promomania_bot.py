"""
Promo Manía - Bot de detección de ofertas en Mercado Libre
------------------------------------------------------------
Busca productos con descuento mayor al 20% en todas las categorías
del sitio de Argentina y envía una alerta por Telegram por cada
oportunidad nueva que encuentra.

Variables de entorno necesarias (se configuran en Railway, nunca
se escriben acá adentro del código):

  ML_CLIENT_ID        -> Client ID de tu app de Mercado Libre
  ML_CLIENT_SECRET    -> Client Secret de tu app de Mercado Libre
  ML_REFRESH_TOKEN    -> Refresh Token obtenido en la autorización inicial
  TELEGRAM_BOT_TOKEN  -> Token que te dio BotFather
  TELEGRAM_CHAT_ID    -> Tu Chat ID de Telegram

  DESCUENTO_MINIMO    -> (opcional) % mínimo de descuento, default 20
  INTERVALO_MINUTOS   -> (opcional) cada cuánto vuelve a buscar, default 30
"""

import os
import time
import json
import sqlite3
import requests
from datetime import datetime

# ---------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------

ML_CLIENT_ID = os.environ["ML_CLIENT_ID"]
ML_CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
ML_REFRESH_TOKEN = os.environ["ML_REFRESH_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DESCUENTO_MINIMO = float(os.environ.get("DESCUENTO_MINIMO", 20))
INTERVALO_MINUTOS = int(os.environ.get("INTERVALO_MINUTOS", 30))
PRECIO_MINIMO = float(os.environ.get("PRECIO_MINIMO", 10000))
MAX_ALERTAS_POR_CICLO = int(os.environ.get("MAX_ALERTAS_POR_CICLO", 3))

SITE_ID = "MLA"  # Argentina
ML_API = "https://api.mercadolibre.com"

DB_PATH = "ofertas_enviadas.db"

# Identificación para evitar que la API nos bloquee como tráfico genérico/bot
HEADERS_BASE = {
    "User-Agent": "PromoManiaBot/1.0 (+https://github.com/)"
}

# ---------------------------------------------------------------------
# Base de datos simple para no repetir la misma alerta dos veces
# ---------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enviados (
            item_id TEXT PRIMARY KEY,
            fecha TEXT
        )
    """)
    conn.commit()
    return conn


def ya_fue_enviado(conn, item_id):
    cur = conn.execute("SELECT 1 FROM enviados WHERE item_id = ?", (item_id,))
    return cur.fetchone() is not None


def marcar_enviado(conn, item_id):
    conn.execute(
        "INSERT OR IGNORE INTO enviados (item_id, fecha) VALUES (?, ?)",
        (item_id, datetime.now().isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------
# Autenticación con Mercado Libre
# ---------------------------------------------------------------------

def obtener_access_token():
    """Usa el refresh token para conseguir un access token nuevo."""
    url = f"{ML_API}/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": ML_REFRESH_TOKEN,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"]


# ---------------------------------------------------------------------
# Mercado Libre: categorías y búsqueda de ofertas
# ---------------------------------------------------------------------

def obtener_categorias(access_token):
    url = f"{ML_API}/sites/{SITE_ID}/categories"
    resp = requests.get(url, headers=HEADERS_BASE, timeout=15)
    resp.raise_for_status()
    return [c["id"] for c in resp.json()]


def buscar_ofertas_en_categoria(access_token, category_id, limite_paginas=1):
    """
    Recorre los resultados de búsqueda de una categoría y devuelve
    los productos cuyo descuento supera DESCUENTO_MINIMO.
    """
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {access_token}"}
    ofertas = []

    for pagina in range(limite_paginas):
        offset = pagina * 50
        url = f"{ML_API}/sites/{SITE_ID}/search"
        params = {"category": category_id, "limit": 50, "offset": offset}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"     (timeout o error en esta página, se saltea: {e})", flush=True)
            break

        data = resp.json()
        resultados = data.get("results", [])
        if not resultados:
            break

        for item in resultados:
            precio_actual = item.get("price")
            precio_original = item.get("original_price")

            if not precio_original or not precio_actual:
                continue  # este producto no tiene descuento activo

            if precio_original <= precio_actual:
                continue

            if precio_actual < PRECIO_MINIMO:
                continue  # no llega al piso mínimo definido

            descuento = round((1 - (precio_actual / precio_original)) * 100, 1)

            if descuento >= DESCUENTO_MINIMO:
                ofertas.append({
                    "id": item.get("id"),
                    "titulo": item.get("title"),
                    "precio_actual": precio_actual,
                    "precio_original": precio_original,
                    "descuento": descuento,
                    "link": item.get("permalink"),
                    "categoria": category_id,
                })

    return ofertas


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def enviar_alerta_telegram(oferta):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    mensaje = (
        f"🔥 OPORTUNIDAD DETECTADA ({oferta['descuento']}% OFF)\n\n"
        f"📦 {oferta['titulo']}\n\n"
        f"💰 Antes: ${oferta['precio_original']:,.2f}\n"
        f"✅ Ahora: ${oferta['precio_actual']:,.2f}\n\n"
        f"🔗 {oferta['link']}"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
    }

    try:
        requests.post(url, data=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Telegram: {e}")


# ---------------------------------------------------------------------
# Ciclo principal
# ---------------------------------------------------------------------

def seleccionar_mejores(ofertas, cantidad):
    """
    Ordena por % de descuento (mayor a menor) y elige hasta 'cantidad'
    ofertas, sin repetir categoría entre ellas.
    """
    ofertas_ordenadas = sorted(ofertas, key=lambda o: o["descuento"], reverse=True)

    seleccionadas = []
    categorias_usadas = set()

    for oferta in ofertas_ordenadas:
        if oferta["categoria"] in categorias_usadas:
            continue
        seleccionadas.append(oferta)
        categorias_usadas.add(oferta["categoria"])

        if len(seleccionadas) >= cantidad:
            break

    return seleccionadas


def ejecutar_busqueda():
    print(f"[{datetime.now()}] Iniciando búsqueda de ofertas...", flush=True)

    conn = init_db()
    access_token = obtener_access_token()
    categorias = obtener_categorias(access_token)
    print(f"[{datetime.now()}] Se encontraron {len(categorias)} categorías para revisar.", flush=True)

    todas_las_ofertas = []

    for i, cat_id in enumerate(categorias, start=1):
        print(f"[{datetime.now()}] Revisando categoría {i}/{len(categorias)}: {cat_id}", flush=True)
        try:
            ofertas = buscar_ofertas_en_categoria(access_token, cat_id)
        except requests.exceptions.RequestException as e:
            print(f"  -> Error en categoría {cat_id}: {e}", flush=True)
            continue

        # Descartamos las que ya te mandamos en ciclos anteriores
        ofertas_nuevas = [o for o in ofertas if not ya_fue_enviado(conn, o["id"])]
        todas_las_ofertas.extend(ofertas_nuevas)
        print(f"  -> {len(ofertas_nuevas)} oportunidades nuevas encontradas en esta categoría.", flush=True)

    mejores = seleccionar_mejores(todas_las_ofertas, MAX_ALERTAS_POR_CICLO)

    for oferta in mejores:
        enviar_alerta_telegram(oferta)
        marcar_enviado(conn, oferta["id"])
        time.sleep(1)  # para no saturar la API de Telegram

    conn.close()
    print(
        f"[{datetime.now()}] Búsqueda terminada. "
        f"Oportunidades detectadas: {len(todas_las_ofertas)} | "
        f"Alertas enviadas: {len(mejores)}"
    )


if __name__ == "__main__":
    ejecutar_busqueda()
