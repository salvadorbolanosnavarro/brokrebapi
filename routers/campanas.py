from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import httpx
import os
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

router = APIRouter()

TIPO_CAMBIO_MXN_USD = 17.5
META_GRAPH      = "https://graph.facebook.com/v19.0"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

OBJETIVO_MAP = {
    "Conseguir leads":         "OUTCOME_LEADS",
    "Llevar tráfico a mi web": "OUTCOME_TRAFFIC",
    "Dar a conocer mi marca":  "OUTCOME_AWARENESS",
}

OPTIM_MAP = {
    "OUTCOME_LEADS":     "LEAD_GENERATION",
    "OUTCOME_TRAFFIC":   "LINK_CLICKS",
    "OUTCOME_AWARENESS": "REACH",
}

ERRORES_META = {
    100:     "Los datos enviados no son válidos. Revisa el presupuesto y las fechas.",
    190:     "Tu sesión de Meta Ads expiró. Vuelve a conectar tu cuenta de Facebook.",
    200:     "No tienes permisos suficientes en esta cuenta de anuncios.",
    273:     "Esta cuenta de anuncios no está activa o fue deshabilitada por Meta.",
    2635:    "El texto del anuncio fue rechazado por las políticas de Meta.",
    1487297: "La imagen no cumple los requisitos: mínimo 600×314 px, máximo 30 MB.",
    17:      "Límite de llamadas a la API alcanzado. Espera unos minutos e intenta de nuevo.",
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


class CampanaRequest(BaseModel):
    nombre: str
    objetivo: str
    presupuesto_diario_mxn: float
    fecha_inicio: str       # YYYY-MM-DD
    fecha_fin: Optional[str] = None
    ciudad: str
    edad_min: int = 25
    edad_max: int = 55
    texto_anuncio: str
    url_destino: str
    imagen_base64: Optional[str] = None
    page_id: Optional[str] = None
    meta_access_token: str
    meta_ad_account_id: str  # act_XXXXXXXXX


@router.post("/api/campanas/crear")
async def crear_campana(req: CampanaRequest):
    token   = req.meta_access_token
    account = req.meta_ad_account_id
    if not account.startswith("act_"):
        account = f"act_{account}"

    # MXN → centavos USD
    daily_budget_cents = int((req.presupuesto_diario_mxn / TIPO_CAMBIO_MXN_USD) * 100)

    objetivo_api = req.objetivo if req.objetivo.startswith("OUTCOME_") else OBJETIVO_MAP.get(req.objetivo, req.objetivo)
    optim_goal   = OPTIM_MAP.get(objetivo_api, "LINK_CLICKS")

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Buscar geo key de la ciudad
        geo_targeting: dict = {"countries": ["MX"]}
        r_geo = await client.get(f"{META_GRAPH}/search", params={
            "type": "adgeolocation", "q": req.ciudad,
            "location_types": '["city"]', "access_token": token, "limit": 1,
        })
        if r_geo.is_success:
            geos = r_geo.json().get("data", [])
            if geos:
                geo_targeting = {"cities": [{"key": geos[0]["key"], "radius": 25, "distance_unit": "kilometer"}]}

       # 2. Crear campaña (siempre PAUSED)
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

        # 3. Crear ad set
        adset_payload: dict = {
            "name":              f"Conjunto - {req.nombre}",
            "campaign_id":       campaign_id,
            "billing_event":     "IMPRESSIONS",
            "optimization_goal": optim_goal,
            "daily_budget":      daily_budget_cents,
            "targeting":         {"geo_locations": geo_targeting, "age_min": req.edad_min, "age_max": req.edad_max},
            "status":            "PAUSED",
            "start_time":        req.fecha_inicio + "T00:00:00-0600",
        }
        if req.fecha_fin:
            adset_payload["end_time"] = req.fecha_fin + "T23:59:59-0600"

        r = await client.post(f"{META_GRAPH}/{account}/adsets", params={"access_token": token}, json=adset_payload)
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
            if r.is_success and "images" in d:
                image_hash = list(d["images"].values())[0].get("hash")

        # 5. Crear ad creative
        link_data: dict = {
            "message":         req.texto_anuncio,
            "link":            req.url_destino,
            "call_to_action":  {"type": "LEARN_MORE"},
        }
        if image_hash:
            link_data["image_hash"] = image_hash

        creative_spec: dict = {"link_data": link_data}
        if req.page_id:
            creative_spec["page_id"] = req.page_id

        r = await client.post(
            f"{META_GRAPH}/{account}/adcreatives",
            params={"access_token": token},
            json={"name": f"Anuncio - {req.nombre}", "object_story_spec": creative_spec},
        )
        d = r.json()
        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear creativo"))
        creative_id = d["id"]

        # 6. Crear anuncio (siempre PAUSED)
        r = await client.post(
            f"{META_GRAPH}/{account}/ads",
            params={"access_token": token},
            json={"name": req.nombre, "adset_id": adset_id, "creative": {"creative_id": creative_id}, "status": "PAUSED"},
        )
        d = r.json()
        if not r.is_success or "id" not in d:
            raise HTTPException(status_code=400, detail=_meta_error(d, "crear anuncio"))
        ad_id = d["id"]

    account_num = account.replace("act_", "")
    return {
        "campaign_id":    campaign_id,
        "adset_id":       adset_id,
        "ad_id":          ad_id,
        "nombre":         req.nombre,
        "estado":         "pausada",
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
        r = await client.get(f"{META_GRAPH}/oauth/access_token", params={
            "client_id":     app_id,
            "client_secret": app_secret,
            "redirect_uri":  redirect_uri,
            "code":          code,
        })

        d = r.json()

        if not r.is_success or "access_token" not in d:
            msg = d.get("error", {}).get("message", "No se pudo completar la autorización con Meta.")
            raise HTTPException(status_code=400, detail=msg)

        return {"access_token": d["access_token"]}


@router.get("/meta-ads/accounts")
async def meta_ads_accounts(access_token: str):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{META_GRAPH}/me/adaccounts", params={
            "access_token": access_token,
            "fields": "id,name,account_status",
        })
        d = r.json()
        if not r.is_success:
            msg = d.get("error", {}).get("message", "No se pudieron obtener las cuentas de anuncios.")
            raise HTTPException(status_code=400, detail=msg)
        accounts = [
            {"id": a["id"], "name": a.get("name", a["id"])}
            for a in d.get("data", [])
            if a.get("account_status", 1) == 1  # 1 = cuenta activa
        ]
        if not accounts:
            raise HTTPException(status_code=400, detail="No encontramos cuentas de anuncios activas en esta cuenta de Facebook. Asegúrate de tener una cuenta de Meta Ads Business.")
        return {"accounts": accounts}
