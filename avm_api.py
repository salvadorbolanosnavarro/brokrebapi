"""
avm_api.py — Motor backend del módulo AVM
==========================================
Mini-servidor web (Flask) que recibe la colonia y ciudad
desde la página avm.html, ejecuta la búsqueda en Google
y devuelve los comparables en formato JSON.

Instalación en cPanel (una sola vez):
  1. En "Setup Python App" instala Flask y requests:
     pip install flask requests

  2. Asegúrate de que WSGI apunte a esta función: application
"""

import re
import os
from flask import Flask, request, jsonify
import requests as req

app = Flask(__name__)

# ─────────────────────────────────────────────
# CREDENCIALES — Rellena con tus datos reales
# ─────────────────────────────────────────────
API_KEY          = ""   # Tu Google API Key
SEARCH_ENGINE_ID = ""   # Tu Search Engine ID (cx)

BASE_URL = "https://www.googleapis.com/customsearch/v1"

# Permite llamadas desde tu propio dominio (CORS)
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

@app.route("/buscar", methods=["POST", "OPTIONS"])
def buscar():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    datos   = request.get_json(force=True)
    colonia = datos.get("colonia", "").strip()
    ciudad  = datos.get("ciudad",  "").strip()

    if not colonia or not ciudad:
        return jsonify({"error": "Colonia y ciudad son requeridos."}), 400

    if not API_KEY or not SEARCH_ENGINE_ID:
        return jsonify({"error": "API_KEY y SEARCH_ENGINE_ID no configurados."}), 500

    # ── Query dinámico ──────────────────────────────────
    query = f'"venta" "casa" "{colonia}" "{ciudad}" "$"'

    try:
        resp = req.get(BASE_URL, params={
            "key": API_KEY,
            "cx":  SEARCH_ENGINE_ID,
            "q":   query,
            "num": 10,
            "lr":  "lang_es",
        }, timeout=15)
        resp.raise_for_status()
    except req.exceptions.RequestException as e:
        return jsonify({"error": f"Error al consultar Google: {str(e)}"}), 502

    items = resp.json().get("items", [])
    comparables = []

    for item in items:
        titulo  = item.get("title",   "")
        url     = item.get("link",    "")
        snippet = item.get("snippet", "")

        # ── Filtro de zona ──────────────────────────────
        if colonia.lower() not in f"{titulo} {snippet}".lower():
            continue

        # ── Extracción con Regex ────────────────────────
        texto   = f"{titulo} {snippet}"
        precio  = _extraer_precio(texto)
        m2_t, m2_c = _extraer_metros(texto)

        comparables.append({
            "titulo":          titulo,
            "url":             url,
            "snippet":         snippet,
            "precio_mxn":      precio,
            "m2_terreno":      m2_t,
            "m2_construccion": m2_c,
        })

    return jsonify({"comparables": comparables, "total": len(comparables)})


# ── Funciones de extracción ─────────────────────────────
def _limpiar(txt):
    try:
        return float(txt.replace(",", "").replace(" ", ""))
    except Exception:
        return None

def _extraer_precio(texto):
    patrones = [
        r'\$\s*([\d,\.]+)',
        r'([\d,\.]+)\s*millones?',
    ]
    for p in patrones:
        m = re.search(p, texto, re.IGNORECASE)
        if m:
            v = _limpiar(m.group(1))
            if v:
                if "millon" in texto[max(0,m.start()-5):m.end()+10].lower():
                    return v * 1_000_000
                return v if v >= 1000 else v * 1000
    return None

def _extraer_metros(texto):
    m2_t = m2_c = None
    mt = re.search(r'([\d,\.]+)\s*m[²2]?\s*(?:de\s*)?terreno', texto, re.I)
    mc = re.search(r'([\d,\.]+)\s*m[²2]?\s*(?:de\s*)?(?:construcción|construcc?\.?)', texto, re.I)
    if mt: m2_t = _limpiar(mt.group(1))
    if mc: m2_c = _limpiar(mc.group(1))
    if m2_t is None and m2_c is None:
        mg = re.search(r'([\d,\.]+)\s*m[²2]', texto, re.I)
        if mg: m2_t = _limpiar(mg.group(1))
    return m2_t, m2_c


# Punto de entrada WSGI para cPanel
application = app

if __name__ == "__main__":
    app.run(debug=True, port=5000)
