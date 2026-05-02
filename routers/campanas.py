from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
from datetime import date, datetime, timedelta
import httpx
import os
import json
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

router = APIRouter()

META_GRAPH = "https://graph.facebook.com/v19.0"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

# IMPORTANTE:
# Por ahora NO usamos OUTCOME_LEADS nativo.
# "Conseguir leads" se trata como tráfico web para captar contactos en tu sitio.
OBJETIVO_MAP = {
    "Conseguir leads":         "OUTCOME_TRAFFIC",
    "Llevar tráfico a mi web": "OUTCOME_TRAFFIC",
    "Dar a conocer mi marca":  "OUTCOME_AWARENESS",
    "OUTCOME_LEADS":           "OUTCOME_TRAFFIC",
    "OUTCOME_TRAFFIC":         "OUTCOME_TRAFFIC",
    "OUTCOME_AWARENESS":       "OUTCOME_AWARENESS",
}

OPTIM_MAP = {
    "OUTCOME_TRAFFIC":   "LINK_CLICKS",
    "OUTCOME_AWARENESS": "REACH",
}


def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}


def _read_secret(env_name: str, config_name: str) -> tuple[str, str]:
    value = os.environ.get(env_name, "")
    if value and value.strip():
        return value.strip(), "env"

    config = _load_config()
    value = str(config.get(config_name, "") or "")
    if value and value.strip():
        return value.strip(), "config.json"

    return "", "missing"


def _meta_credentials() -> tuple[str, str, dict]:
    app_id, app_id_source = _read_secret("META_APP_ID", "meta_app_id")
    app_secret, app_secret_source = _read_secret("META_APP_SECRET", "meta_app_secret")

    status = {
        "meta_app_id_configured": bool(app_id),
        "meta_app_secret_configured": bool(app_secret),
        "meta_app_id_source": app_id_source,
        "meta_app_secret_source": app_secret_source,
    }

    return app_id, app_secret, status


def _meta_error(data: dict, paso: str = "") -> str:
    err = data.get("error", {})
    code = err.get("code", 0)
    message = err.get("message", "Error desconocido de Meta Ads.")
    subcode = err.get("error_subcode")
    user_msg = err.get("error_user_msg")

    partes = []
    if paso:
        partes.append(f"Paso: {paso}")
    partes.append(f"Meta error {code}" + (f" / subcode {subcode}" if subcode else ""))
    partes.append(user_msg or message)

    return " — ".join(partes)


def _normalizar_account(account: str) -> str:
    account = (account or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="Falta seleccionar una cuenta publicitaria.")
    return account if account.startswith("act_") else f"act_{account}"


def _validar_fecha_inicio(fecha_inicio: str) -> str:
    try:
        dt = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="La fecha de inicio debe tener formato YYYY-MM-DD.")

    hoy = date.today()
    if dt < hoy:
        raise HTTPException(status_code=400, detail="La fecha de inicio no puede ser anterior a hoy.")

    return fecha_inicio + "T00:00:00-0600"


def _validar_fecha_fin(fecha_inicio: str, fecha_fin: Optional[str]) -> Optional[str]:
    if not fecha_fin:
        return None

    try:
        ini = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
        fin = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="La fecha fin debe tener formato YYYY-MM-DD.")

    if fin <= ini:
        raise HTTPException(status_code=400, detail="La fecha fin debe ser posterior a la fecha de inicio.")

    return fecha_fin + "T23:59:59-0600"


def _extraer_url(texto: str) -> str:
    m = re.search(r"https?://[^\s]+", texto or "")
    return m.group(0).rstrip(".,)") if m else ""


def _extraer_presupuesto(texto: str) -> float:
    texto = texto or ""
    m = re.search(r"\$?\s?(\d+(?:[.,]\d+)?)\s*(?:pesos|mxn|diarios|al día|por día)?", texto, re.I)
    if not m:
        return 150.0
    return float(m.group(1).replace(",", "."))


def _extraer_ciudad(texto: str) -> str:
    texto_low = (texto or "").lower()
    ciudades = ["Morelia", "Uruapan", "Pátzcuaro", "Ciudad de México", "Guadalajara", "Querétaro", "Monterrey"]
    for ciudad in ciudades:
        if ciudad.lower() in texto_low:
            return ciudad
    return "Morelia"


class CampanaRequest(BaseModel):
    nombre: str
    objetivo: str
    presupuesto_diario_mxn: float
    fecha_inicio: str
    fecha_fin: Optional[str] = None
    ciudad: str
    edad_min: int = 25
    edad_max: int = 55
    texto_anuncio: str
    url_destino: str
    imagen_base64: Optional[str] = None
    page_id: Optional[str] = None
    meta_access_token: str
    meta_ad_account_id: str


class DraftCampanaRequest(BaseModel):
    prompt: str
    meta_access_token: Optional[str] = None
    meta_ad_account_id: Optional[str] = None
    page_id: Optional[str] = None


@router.post("/api/campanas/draft")
async def draft_campana(req: DraftCampanaRequest):
    prompt = (req.prompt or "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Escribe instrucciones para crear el borrador de campaña.")

    url = _extraer_url(prompt)
    presupuesto = _extraer_presupuesto(prompt)
    ciudad = _extraer_ciudad(prompt)

    objetivo = "OUTCOME_TRAFFIC"
    if any(x in prompt.lower() for x in ["reconocimiento", "awareness", "dar a conocer", "marca"]):
        objetivo = "OUTCOME_AWARENESS"

    manana = date.today() + timedelta(days=1)

    nombre = "Campaña Brokr"
    if len(prompt) > 8:
        nombre = "Campaña - " + prompt[:45].strip().rstrip(".,")

    texto_anuncio = prompt
    if len(texto_anuncio) > 250:
        texto_anuncio = texto_anuncio[:247] + "..."

    return {
        "draft": {
            "nombre": nombre,
            "objetivo": objetivo,
            "presupuesto_diario_mxn": max(presupuesto, 50),
            "fecha_inicio": manana.strftime("%Y-%m-%d"),
            "fecha_fin": None,
            "ciudad": ciudad,
            "edad_min": 25,
            "edad_max": 55,
            "texto_anuncio": texto_anuncio,
            "url_destino": url,
            "page_id": req.page_id or "",
            "meta_access_token": req.meta_access_token or "",
            "meta_ad_account_id": req.meta_ad_account_id or "",
        },
        "nota": "Esto es un borrador. Muéstralo al usuario y pide confirmación antes de llamar /api/campanas/crear.",
    }


@router.post("/api/campanas/crear")
async def crear_campana(req: CampanaRequest):
    token = (req.meta_access_token or "").strip()
    account = _normalizar_account(req.meta_ad_account_id)

    if not token:
        raise HTTPException(status_code=400, detail="Falta conectar la cuenta de Meta Ads.")

    if not req.nombre.strip():
        raise HTTPException(status_code=400, detail="Falta el nombre de la campaña.")

    if req.presupuesto_diario_mxn < 50:
        raise HTTPException(status_code=400, detail="El presupuesto diario mínimo recomendado es $50 MXN.")

    if req.edad_min < 18:
        raise HTTPException(status_code=400, detail="La edad mínima no puede ser menor a 18.")

    if req.edad_max < req.edad_min:
        raise HTTPException(status_code=400, detail="La edad máxima debe ser mayor o igual a la edad mínima.")

    objetivo_api = OBJETIVO_MAP.get(req.objetivo, req.objetivo)
    if objetivo_api == "OUTCOME_LEADS":
        objetivo_api = "OUTCOME_TRAFFIC"

    if objetivo_api not in ("OUTCOME_TRAFFIC", "OUTCOME_AWARENESS"):
        raise HTTPException(
            status_code=400,
            detail="Por ahora este módulo solo soporta tráfico web y reconocimiento. Leads nativos requieren otro flujo con formularios instantáneos.",
        )

    optim_goal = OPTIM_MAP.get(objetivo_api, "LINK_CLICKS")

    if objetivo_api == "OUTCOME_TRAFFIC":
        if not req.url_destino.strip():
            raise HTTPException(status_code=400, detail="Falta la URL destino para la campaña de tráfico.")
        if not req.url_destino.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="La URL destino debe empezar con http:// o https://.")

    if not req.page_id:
        raise HTTPException(status_code=400, detail="Falta seleccionar una página de Facebook para crear el anuncio.")

    start_time = _validar_fecha_inicio(req.fecha_inicio)
    end_time = _validar_fecha_fin(req.fecha_inicio, req.fecha_fin)

    # Meta espera presupuesto en la unidad mínima de la moneda de la cuenta.
    # Si la cuenta está en MXN, $200 MXN = 20000 centavos.
    daily_budget_cents = int(req.presupuesto_diario_mxn * 100)

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Buscar geo key de la ciudad
        geo_targeting: dict = {"countries": ["MX"]}

        if req.ciudad:
            r_geo = await client.get(
                f"{META_GRAPH}/search",
                params={
                    "type": "adgeolocation",
                    "q": req.ciudad,
                    "location_types": '["city"]',
                    "access_token": token,
                    "limit": 1,
                },
            )

            if r_geo.is_success:
                geos = r_geo.json().get("data", [])
                if geos:
                    geo_targeting = {
                        "cities": [
                            {
                                "key": geos[0]["key"],
                                "radius": 25,
                                "distance_unit": "kilometer",
                            }
                        ]
                    }

        # 2. Crear campaña
        r = await client.post(
            f"{META_GRAPH}/{account}/campaigns",
            params={"access_token": token},
            json={
                "name": req.nombre,
                "objective": objetivo_api,
                "status": "PAUSED",
                "special_ad_categories": [],
                "is_adset_budget_sharing_enabled": False,
            },
        )

        d = r.json()

        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear campaña"))

        campaign_id = d["id"]

        # 3. Crear conjunto de anuncios
        adset_payload: dict = {
            "name": f"Conjunto - {req.nombre}",
            "campaign_id": campaign_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": optim_goal,
            "daily_budget": daily_budget_cents,
            "targeting": {
                "geo_locations": geo_targeting,
                "age_min": req.edad_min,
                "age_max": req.edad_max,
            },
            "status": "PAUSED",
            "start_time": start_time,
        }

        if objetivo_api == "OUTCOME_TRAFFIC":
            adset_payload["destination_type"] = "WEBSITE"

        if end_time:
            adset_payload["end_time"] = end_time

        r = await client.post(
            f"{META_GRAPH}/{account}/adsets",
            params={"access_token": token},
            json=adset_payload,
        )

        d = r.json()

        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear conjunto de anuncios"))

        adset_id = d["id"]

        # 4. Subir imagen si viene
        image_hash: Optional[str] = None

        if req.imagen_base64:
            img_b64 = req.imagen_base64

            if "," in img_b64:
                img_b64 = img_b64.split(",", 1)[1]

            r = await client.post(
                f"{META_GRAPH}/{account}/adimages",
                params={"access_token": token},
                json={"bytes": img_b64},
            )

            d = r.json()

            if not r.is_success:
                raise HTTPException(status_code=400, detail=_meta_error(d, "subir imagen"))

            if "images" in d and d["images"]:
                image_hash = list(d["images"].values())[0].get("hash")

        # 5. Crear creative
        link_data: dict = {
            "message": req.texto_anuncio or req.nombre,
            "link": req.url_destino,
            "call_to_action": {
                "type": "LEARN_MORE",
                "value": {
                    "link": req.url_destino,
                },
            },
        }

        if image_hash:
            link_data["image_hash"] = image_hash

        creative_spec: dict = {
            "page_id": req.page_id,
            "link_data": link_data,
        }

        r = await client.post(
            f"{META_GRAPH}/{account}/adcreatives",
            params={"access_token": token},
            json={
                "name": f"Anuncio - {req.nombre}",
                "object_story_spec": creative_spec,
            },
        )

        d = r.json()

        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear creativo"))

        creative_id = d["id"]

        # 6. Crear anuncio
        r = await client.post(
            f"{META_GRAPH}/{account}/ads",
            params={"access_token": token},
            json={
                "name": req.nombre,
                "adset_id": adset_id,
                "creative": {"creative_id": creative_id},
                "status": "PAUSED",
            },
        )

        d = r.json()

        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear anuncio"))

        ad_id = d["id"]

    account_num = account.replace("act_", "")

    return {
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "ad_id": ad_id,
        "nombre": req.nombre,
        "objetivo": objetivo_api,
        "estado": "pausada",
        "ads_manager_url": f"https://www.facebook.com/adsmanager/manage/campaigns?act={account_num}&selected_campaign_ids={campaign_id}",
    }


# ── Meta Ads OAuth helpers ──────────────────────────────────────────────────────

@router.get("/meta-ads/config")
async def meta_ads_config():
    app_id, _app_secret, status = _meta_credentials()

    return {
        **status,
        "meta_app_id_last4": app_id[-4:] if app_id else "",
        "graph_api_version": META_GRAPH.rsplit("/", 1)[-1],
    }


@router.get("/meta-ads/callback")
async def meta_ads_callback(code: str, redirect_uri: str):
    app_id, app_secret, status = _meta_credentials()

    if not app_id or not app_secret:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "META_APP_ID o META_APP_SECRET no están disponibles para este proceso del backend.",
                "diagnostico": status,
                "accion": "Abre /meta-ads/config en la misma URL del backend que usa la app y confirma que ambas aparezcan como configured=true.",
            },
        )

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{META_GRAPH}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

        d = r.json()

        if not r.is_success or "access_token" not in d:
            msg = d.get("error", {}).get(
                "message",
                "No se pudo completar la autorización con Meta.",
            )
            raise HTTPException(status_code=400, detail=msg)

        return {"access_token": d["access_token"]}


@router.get("/meta-ads/accounts")
async def meta_ads_accounts(access_token: str):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{META_GRAPH}/me/adaccounts",
            params={
                "access_token": access_token,
                "fields": "id,name,account_status",
            },
        )

        d = r.json()

        if not r.is_success:
            msg = d.get("error", {}).get(
                "message",
                "No se pudieron obtener las cuentas de anuncios.",
            )
            raise HTTPException(status_code=400, detail=msg)

        accounts = [
            {"id": a["id"], "name": a.get("name", a["id"])}
            for a in d.get("data", [])
            if a.get("account_status", 1) == 1
        ]

        if not accounts:
            raise HTTPException(
                status_code=400,
                detail="No encontramos cuentas de anuncios activas en esta cuenta de Facebook. Asegúrate de tener una cuenta de Meta Ads Business.",
            )

        return {"accounts": accounts}


@router.get("/meta-ads/pages")
async def meta_ads_pages(access_token: str):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{META_GRAPH}/me/accounts",
            params={
                "access_token": access_token,
                "fields": "id,name,access_token",
                "limit": 100,
            },
        )

        d = r.json()

        if not r.is_success:
            msg = d.get("error", {}).get(
                "message",
                "No se pudieron obtener las páginas de Facebook.",
            )
            raise HTTPException(status_code=400, detail=msg)

        pages = [
            {
                "id": p["id"],
                "name": p.get("name", p["id"]),
            }
            for p in d.get("data", [])
        ]

        if not pages:
            raise HTTPException(
                status_code=400,
                detail="No encontramos páginas de Facebook disponibles para esta cuenta.",
            )

        return {"pages": pages}
