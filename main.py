from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os
import time
import re
from typing import Optional
from datetime import datetime

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EB_API_KEY = os.environ.get("EB_API_KEY", "")
EB_BASE    = "https://api.easybroker.com/v1"

# ── CACHE EN MEMORIA (TTL 6h) ──
_cache: dict = {}
CACHE_TTL = 21600  # 6 hours

def cache_get(key):
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None

def cache_set(key, data):
    _cache[key] = (data, time.time())

def eb_headers():
    return {"X-Authorization": EB_API_KEY, "accept": "application/json"}

# ────────────────────────────────────────────
# EASYBROKER — BASE ENDPOINTS
# ────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Brokr API activa", "version": "4.0"}

@app.get("/propiedad/{property_id}")
async def get_propiedad(property_id: str):
    if not EB_API_KEY:
        raise HTTPException(status_code=500, detail="EB_API_KEY no configurada")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{EB_BASE}/properties/{property_id}",
                             headers=eb_headers())
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Propiedad no encontrada")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Error EasyBroker")
        return r.json()

@app.get("/propiedades")
async def get_propiedades(page: int = 1, limit: int = 20):
    if not EB_API_KEY:
        raise HTTPException(status_code=500, detail="EB_API_KEY no configurada")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{EB_BASE}/properties", headers=eb_headers(),
                             params={"page": page, "limit": limit})
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Error EasyBroker")
        return r.json()

# ────────────────────────────────────────────
# COLONIAS AUTOCOMPLETE
# ────────────────────────────────────────────
async def fetch_all_properties() -> list:
    """Fetch all properties from EB and cache them."""
    cached = cache_get("all_properties")
    if cached is not None:
        return cached

    all_props = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(f"{EB_BASE}/properties", headers=eb_headers(),
                                 params={"limit": 50, "page": page})
            if r.status_code != 200:
                break
            data = r.json()
            props = data.get("content", [])
            if not props:
                break
            all_props.extend(props)
            # Stop if we have enough or no more pages
            total = data.get("pagination", {}).get("total", 0)
            if len(all_props) >= min(total, 3000):  # cap at 3000 for speed
                break
            if not data.get("pagination", {}).get("next_page"):
                break
            page += 1
            if page > 60:  # safety cap
                break

    cache_set("all_properties", all_props)
    return all_props

def extract_colonia(location_str: str) -> str:
    """Extract colonia from 'Colonia, Ciudad, Estado' string."""
    if not location_str:
        return ""
    parts = [p.strip() for p in location_str.split(",")]
    return parts[0] if parts else location_str.strip()

def normalize(s: str) -> str:
    for a, b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ü','u'),('ñ','n')]:
        s = s.lower().replace(a, b)
    return s

@app.get("/colonias")
async def get_colonias(q: str = Query("", min_length=2), ciudad: str = "Morelia"):
    """Return unique colonias matching search query — fast direct EB search."""
    if not EB_API_KEY:
        raise HTTPException(status_code=500, detail="EB_API_KEY no configurada")

    cache_key = f"colonias_{normalize(ciudad)}"
    colonias_map = cache_get(cache_key)

    if colonias_map is None:
        # Build index: paginate EB and collect all colonias
        colonias_map = {}
        page = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while page <= 80:  # up to 4000 properties
                r = await client.get(
                    f"{EB_BASE}/properties",
                    headers=eb_headers(),
                    params={"limit": 50, "page": page}
                )
                if r.status_code != 200:
                    break
                data = r.json()
                props = data.get("content", [])
                if not props:
                    break
                for p in props:
                    loc = p.get("location", "")
                    if not loc or normalize(ciudad) not in normalize(loc):
                        continue
                    # Only published + 2024+
                    status = p.get("status", "").lower()
                    if status and status not in ("published","publicada","activa","active",""):
                        continue
                    updated = p.get("updated_at","")
                    if updated:
                        try:
                            if int(updated[:4]) < 2024:
                                continue
                        except:
                            pass
                    col = extract_colonia(loc)
                    if col and len(col) > 2:
                        colonias_map[col] = colonias_map.get(col, 0) + 1
                if not data.get("pagination",{}).get("next_page"):
                    break
                page += 1
        cache_set(cache_key, colonias_map)

    q_norm = normalize(q)
    matches = [
        {"colonia": col, "count": cnt}
        for col, cnt in colonias_map.items()
        if q_norm in normalize(col)
    ]
    matches.sort(key=lambda x: -x["count"])
    return {"colonias": matches[:12], "total_colonias": len(colonias_map)}

# ────────────────────────────────────────────
# AVM — HELPERS
# ────────────────────────────────────────────
class AVMRequest(BaseModel):
    colonia: str
    ciudad: str
    tipo: str
    operacion: str
    m2_construccion: Optional[float] = None
    m2_terreno:      Optional[float] = None
    recamaras:       Optional[int]   = None
    banos:           Optional[float] = None
    estado:          Optional[str]   = "bueno"
    anio_construccion: Optional[int] = None

def parse_price(val) -> Optional[float]:
    if not val:
        return None
    try:
        v = float(str(val).replace(",", ""))
        if 50_000 <= v <= 999_000_000:
            return v
    except:
        pass
    return None

TIPO_MAP = {
    "casa":          ["Casa", "casa"],
    "departamento":  ["Departamento", "departamento"],
    "terreno":       ["Terreno", "terreno"],
    "local":         ["Local comercial", "local"],
    "comercial":     ["Local comercial", "Oficina", "Bodega"],
    "oficina":       ["Oficina", "oficina"],
    "bodega":        ["Bodega", "bodega"],
}
OP_MAP = {
    "venta": "sale",
    "renta": "rental",
}

async def get_comparables_eb(colonia: str, ciudad: str,
                              tipo: str, operacion: str) -> list:
    """Fetch comparables from EB properties filtered by colonia+tipo+operacion."""
    cache_key = f"comp_{colonia}_{ciudad}_{tipo}_{operacion}".lower().replace(" ","_")
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    tipo_labels = TIPO_MAP.get(tipo.lower(), [tipo])
    op_type     = OP_MAP.get(operacion.lower(), "sale")

    comparables = []
    page = 1

    async with httpx.AsyncClient(timeout=20) as client:
        while len(comparables) < 50 and page <= 20:
            r = await client.get(
                f"{EB_BASE}/properties",
                headers=eb_headers(),
                params={"limit": 50, "page": page}
            )
            if r.status_code != 200:
                break
            data = r.json()
            props = data.get("content", [])
            if not props:
                break

            for p in props:
                # Location filter — colonia must match
                loc = p.get("location", "")
                col_prop = extract_colonia(loc)

                # Fuzzy colonia match
                def norm(s):
                    for a,b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n')]:
                        s = s.lower().replace(a,b)
                    return s

                if norm(colonia) not in norm(loc):
                    continue

                # Ciudad filter
                if ciudad.lower() not in loc.lower():
                    continue

                # Tipo filter
                prop_type = p.get("property_type", "")
                if not any(t.lower() in prop_type.lower() for t in tipo_labels):
                    continue

                # Operation filter
                ops = p.get("operations", [])
                matching_op = None
                for op in ops:
                    if op.get("type") == op_type:
                        matching_op = op
                        break
                if not matching_op:
                    continue

                price = parse_price(matching_op.get("amount"))
                if not price:
                    continue

                # ── STATUS FILTER: only published properties ──
                status = p.get("status", "").lower()
                if status and status not in ("published", "publicada", "activa", "active", ""):
                    continue

                # ── DATE FILTER: updated in 2024 or later ──
                updated_at = p.get("updated_at", "")
                if updated_at:
                    try:
                        year = int(updated_at[:4])
                        if year < 2024:
                            continue
                    except:
                        pass

                comparables.append({
                    "precio":          price,
                    "titulo":          p.get("title", "")[:80],
                    "m2_construccion": p.get("construction_size"),
                    "m2_terreno":      p.get("lot_size"),
                    "recamaras":       p.get("bedrooms"),
                    "banos":           p.get("bathrooms"),
                    "colonia":         col_prop,
                    "fuente":          "EasyBroker",
                    "public_id":       p.get("public_id", ""),
                    "updated_at":      updated_at[:10] if updated_at else "",
                })

            if not data.get("pagination", {}).get("next_page"):
                break
            page += 1

    # Remove outliers
    if len(comparables) >= 3:
        prices = sorted(c["precio"] for c in comparables)
        median = prices[len(prices)//2]
        comparables = [c for c in comparables
                       if median * 0.25 <= c["precio"] <= median * 4.0]

    cache_set(cache_key, comparables[:30])
    return comparables[:30]

# ────────────────────────────────────────────
# HEDONIC MODEL
# ────────────────────────────────────────────
def ajuste_hedonico(comp: dict, sujeto: dict) -> dict:
    precio_base = comp["precio"]
    ajustes = []
    factor  = 1.0

    # m² construction (sqrt scaling)
    m2s = sujeto.get("m2_construccion")
    m2c = comp.get("m2_construccion")
    if m2s and m2c and m2c > 0 and abs(m2s - m2c) > 5:
        ratio = (m2s / m2c) ** 0.5
        factor *= ratio
        diff = m2s - m2c
        ajustes.append(f"m² ({'+' if diff>0 else ''}{diff:.0f}): "
                       f"{'+' if ratio>1 else ''}{(ratio-1)*100:.1f}%")

    # Bedrooms (4% per room)
    rs = sujeto.get("recamaras")
    rc = comp.get("recamaras")
    if rs and rc and rs != rc:
        diff = rs - rc
        factor *= (1 + diff * 0.04)
        ajustes.append(f"recámaras ({'+' if diff>0 else ''}{diff}): "
                       f"{'+' if diff>0 else ''}{diff*4}%")

    # Conservation state
    estado_adj = {"malo":-0.15,"regular":-0.07,"bueno":0.0,"excelente":0.08}
    adj_e = estado_adj.get(sujeto.get("estado","bueno"), 0.0)
    if adj_e != 0:
        factor *= (1 + adj_e)
        ajustes.append(f"estado ({sujeto.get('estado')}): "
                       f"{'+' if adj_e>0 else ''}{adj_e*100:.0f}%")

    # Age (1.5% per decade over 10 years)
    anio = sujeto.get("anio_construccion")
    if anio:
        anos = datetime.now().year - anio
        age_adj = max(-0.20, min(0.15, -0.015 * ((anos - 10) / 10)))
        if abs(age_adj) > 0.01:
            factor *= (1 + age_adj)
            ajustes.append(f"antigüedad ({anos} años): "
                           f"{'+' if age_adj>0 else ''}{age_adj*100:.1f}%")

    # EB properties are already real transaction prices
    # No offer-to-close discount needed (unlike portal listings)
    if not ajustes:
        ajustes.append("sin ajustes — comparable directo")

    return {
        **comp,
        "precio_ajustado": round(precio_base * factor, -3),
        "factor_total":    round(factor, 4),
        "ajustes":         ajustes,
    }

# ────────────────────────────────────────────
# AVM ENDPOINT
# ────────────────────────────────────────────
@app.post("/avm")
async def calcular_avm(req: AVMRequest):
    if not EB_API_KEY:
        raise HTTPException(status_code=500, detail="EB_API_KEY no configurada")

    comparables_raw = await get_comparables_eb(
        req.colonia, req.ciudad, req.tipo, req.operacion
    )

    nivel = 1
    nivel_msg = ""

    if len(comparables_raw) < 3:
        # Expand: try nearby colonias by fetching more broadly
        nivel = 2
        comparables_raw = await get_comparables_eb(
            "", req.ciudad, req.tipo, req.operacion
        )
        nivel_msg = (f"Pocos comparables en {req.colonia}. "
                     f"Se amplió a toda la ciudad filtrando por precio/m².")

    if len(comparables_raw) < 2:
        raise HTTPException(
            status_code=422,
            detail=(f"No se encontraron comparables de {req.tipo} en {req.operacion} "
                    f"en {req.ciudad}. Verifica el tipo de operación e inmueble.")
        )

    sujeto = {
        "m2_construccion":   req.m2_construccion,
        "m2_terreno":        req.m2_terreno,
        "recamaras":         req.recamaras,
        "banos":             req.banos,
        "estado":            req.estado,
        "anio_construccion": req.anio_construccion,
    }

    # Apply hedonic adjustments
    ajustados = []
    for comp in comparables_raw:
        try:
            ajustados.append(ajuste_hedonico(comp, sujeto))
        except:
            continue

    if not ajustados:
        raise HTTPException(status_code=422, detail="Error procesando comparables")

    # Filter by price/m² if we have m2 data (nivel 2 only)
    if nivel == 2 and req.m2_construccion and req.m2_construccion > 0:
        pm2s = [(c, c["precio_ajustado"] / req.m2_construccion)
                for c in ajustados]
        if len(pm2s) >= 5:
            vals = sorted(p for _, p in pm2s)
            median_pm2 = vals[len(vals)//2]
            ajustados = [c for c, pm2 in pm2s
                         if median_pm2 * 0.65 <= pm2 <= median_pm2 * 1.35]

    # Calculate value range
    precios = sorted(c["precio_ajustado"] for c in ajustados)
    n       = len(precios)
    trim    = max(1, n // 10)
    p_trim  = precios[trim: n-trim] if n > 4 else precios

    valor_minimo   = round(min(p_trim), -3)
    valor_probable = round(sum(p_trim) / len(p_trim), -3)
    valor_maximo   = round(max(p_trim), -3)

    # Price per m²
    pm2_list = []
    for c in ajustados:
        m2 = c.get("m2_construccion") or req.m2_construccion
        if m2 and m2 > 0:
            pm2_list.append(c["precio_ajustado"] / m2)
    pm2_prom = round(sum(pm2_list) / len(pm2_list)) if pm2_list else None

    nivel_labels = {
        1: f"Alta confianza — {len(ajustados)} comparables en {req.colonia}",
        2: f"Confianza media — {len(ajustados)} comparables en {req.ciudad} (filtrado por precio/m²)",
    }

    return {
        "colonia":            req.colonia,
        "ciudad":             req.ciudad,
        "tipo":               req.tipo,
        "operacion":          req.operacion,
        "nivel":              nivel,
        "nivel_mensaje":      nivel_labels.get(nivel, nivel_msg),
        "fuentes":            ["EasyBroker"],
        "num_comparables":    len(ajustados),
        "valor_minimo":       valor_minimo,
        "valor_probable":     valor_probable,
        "valor_maximo":       valor_maximo,
        "precio_m2_promedio": pm2_prom,
        "comparables":        ajustados[:10],
        "nota": ("Valores calculados con base en propiedades publicadas en la bolsa "
                 "EasyBroker (2024-2025), con ajustes hedónicos por m², recámaras, "
                 "estado y antigüedad. El valor definitivo requiere inspección física "
                 "y avalúo formal."),
        "timestamp": time.strftime("%Y-%m-%d %H:%M"),
    }
