"""
Grobid Onyx Infrastructure - Wrapper Python pour Grobid Docker

Ce skill encapsule le conteneur Grobid et fournit:
- Gestion du cycle de vie Docker (start/stop)
- Proxy API vers Grobid
- Integration Onyx SDK (statuts, events)
- Health checks unifies
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from contextlib import asynccontextmanager
from pathlib import Path
import docker
import subprocess
import asyncio
import httpx
import uvicorn
import os

# Configuration
SKILL_NAME = "grobid-onyx-infra"
BRAIN_AREA = "prefrontal"
WRAPPER_PORT = 8071  # Port du wrapper Python
GROBID_PORT = 8070   # Port interne Grobid
GROBID_URL = f"http://localhost:{GROBID_PORT}"
DOCKER_COMPOSE_PATH = Path(__file__).parent.parent / "docker" / "docker-compose.yml"

# Docker client
docker_client = docker.from_env()

# Onyx SDK (optionnel, ne bloque pas si non disponible)
try:
    from onyx_sdk import OnyxClient
    onyx_client = OnyxClient(SKILL_NAME, BRAIN_AREA, port=WRAPPER_PORT)
    HAS_ONYX_SDK = True
except ImportError:
    onyx_client = None
    HAS_ONYX_SDK = False


def set_status(status: str, message: str = ""):
    """Set Onyx status if SDK available."""
    if onyx_client:
        if status == "working":
            onyx_client.working(message)
        elif status == "idle":
            onyx_client.idle()
        elif status == "error":
            onyx_client.error(message)


async def start_containers():
    """Demarre les containers Docker via docker-compose."""
    set_status("working", "Demarrage du conteneur Grobid...")

    result = subprocess.run(
        ["docker", "compose", "-f", str(DOCKER_COMPOSE_PATH), "up", "-d"],
        capture_output=True,
        text=True,
        cwd=DOCKER_COMPOSE_PATH.parent
    )

    if result.returncode != 0:
        set_status("error", f"Echec demarrage: {result.stderr}")
        raise RuntimeError(f"Docker compose failed: {result.stderr}")


async def stop_containers():
    """Arrete les containers Docker."""
    subprocess.run(
        ["docker", "compose", "-f", str(DOCKER_COMPOSE_PATH), "down"],
        capture_output=True,
        cwd=DOCKER_COMPOSE_PATH.parent
    )


def check_containers_health() -> dict:
    """Verifie la sante des containers Grobid."""
    try:
        containers = docker_client.containers.list(
            filters={"label": f"onyx.skill={SKILL_NAME}"}
        )

        if not containers:
            return {"healthy": False, "containers": {}, "message": "No containers found"}

        statuses = {}
        for c in containers:
            statuses[c.name] = {
                "status": c.status,
                "healthy": c.status == "running"
            }

        all_healthy = all(s["healthy"] for s in statuses.values())
        return {"healthy": all_healthy, "containers": statuses}

    except Exception as e:
        return {"healthy": False, "containers": {}, "error": str(e)}


async def check_grobid_ready() -> bool:
    """Verifie si Grobid est pret a recevoir des requetes."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{GROBID_URL}/api/isalive")
            return response.status_code == 200 and response.text.strip() == "true"
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: demarre les containers au startup, les arrete au shutdown."""
    # Startup
    await start_containers()

    # Attendre que Grobid soit pret (timeout 3 minutes pour chargement des modeles)
    set_status("working", "Chargement des modeles Grobid...")
    for i in range(180):
        if await check_grobid_ready():
            break
        await asyncio.sleep(1)
        if i % 30 == 0:
            set_status("working", f"Chargement des modeles... ({i}s)")

    if await check_grobid_ready():
        set_status("idle")
    else:
        set_status("error", "Grobid n'a pas demarre correctement")

    yield

    # Shutdown
    await stop_containers()


# FastAPI app
app = FastAPI(
    title="Grobid Onyx Infrastructure",
    description="Service d'extraction de metadonnees PDF via Grobid",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health():
    """Health check unifie (containers + Grobid API)."""
    container_health = check_containers_health()
    grobid_ready = await check_grobid_ready()

    healthy = container_health["healthy"] and grobid_ready

    if not healthy:
        raise HTTPException(status_code=503, detail={
            "containers": container_health,
            "grobid_api": grobid_ready
        })

    return {
        "status": "healthy",
        "containers": container_health,
        "grobid_api": grobid_ready
    }


@app.get("/status")
async def status():
    """Statut detaille du service."""
    container_health = check_containers_health()
    grobid_ready = await check_grobid_ready()

    return {
        "skill": SKILL_NAME,
        "version": "1.0.0",
        "grobid_url": GROBID_URL,
        "containers": container_health,
        "grobid_api_ready": grobid_ready,
        "onyx_sdk": HAS_ONYX_SDK
    }


# === Proxy endpoints vers Grobid ===

@app.get("/api/isalive")
async def is_alive():
    """Proxy vers Grobid /api/isalive."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{GROBID_URL}/api/isalive")
        return Response(content=response.content, media_type="text/plain")


@app.get("/api/version")
async def version():
    """Proxy vers Grobid /api/version."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{GROBID_URL}/api/version")
        return Response(content=response.content, media_type="text/plain")


@app.post("/api/processFulltextDocument")
async def process_fulltext_document(
    input: UploadFile = File(...),
    consolidateHeader: int = Form(default=1),
    consolidateCitations: int = Form(default=0),
    includeRawCitations: int = Form(default=0),
    includeRawAffiliations: int = Form(default=0),
    teiCoordinates: str = Form(default=""),
    segmentSentences: int = Form(default=0)
):
    """
    Traite un document PDF complet et retourne le TEI XML.

    Principal endpoint pour l'extraction de texte structure.
    """
    set_status("working", f"Traitement: {input.filename}")

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            files = {"input": (input.filename, await input.read(), input.content_type)}
            data = {
                "consolidateHeader": consolidateHeader,
                "consolidateCitations": consolidateCitations,
                "includeRawCitations": includeRawCitations,
                "includeRawAffiliations": includeRawAffiliations,
                "teiCoordinates": teiCoordinates,
                "segmentSentences": segmentSentences
            }

            response = await client.post(
                f"{GROBID_URL}/api/processFulltextDocument",
                files=files,
                data=data
            )

            set_status("idle")
            return Response(
                content=response.content,
                media_type="application/xml",
                status_code=response.status_code
            )

    except Exception as e:
        set_status("error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/processHeaderDocument")
async def process_header_document(
    input: UploadFile = File(...),
    consolidateHeader: int = Form(default=1)
):
    """Extrait uniquement les metadonnees d'en-tete (titre, auteurs, abstract)."""
    set_status("working", f"Extraction header: {input.filename}")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            files = {"input": (input.filename, await input.read(), input.content_type)}
            data = {"consolidateHeader": consolidateHeader}

            response = await client.post(
                f"{GROBID_URL}/api/processHeaderDocument",
                files=files,
                data=data
            )

            set_status("idle")
            return Response(
                content=response.content,
                media_type="application/xml",
                status_code=response.status_code
            )

    except Exception as e:
        set_status("error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/processReferences")
async def process_references(
    input: UploadFile = File(...),
    consolidateCitations: int = Form(default=0)
):
    """Extrait et parse les references bibliographiques."""
    set_status("working", f"Extraction references: {input.filename}")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            files = {"input": (input.filename, await input.read(), input.content_type)}
            data = {"consolidateCitations": consolidateCitations}

            response = await client.post(
                f"{GROBID_URL}/api/processReferences",
                files=files,
                data=data
            )

            set_status("idle")
            return Response(
                content=response.content,
                media_type="application/xml",
                status_code=response.status_code
            )

    except Exception as e:
        set_status("error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/processCitation")
async def process_citation(
    citations: str = Form(...),
    consolidateCitations: int = Form(default=0)
):
    """Parse une ou plusieurs citations en texte brut."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            data = {
                "citations": citations,
                "consolidateCitations": consolidateCitations
            }

            response = await client.post(
                f"{GROBID_URL}/api/processCitation",
                data=data
            )

            return Response(
                content=response.content,
                media_type="application/xml",
                status_code=response.status_code
            )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Endpoints de gestion Docker ===

@app.post("/docker/restart")
async def restart_containers():
    """Redemarre les containers Grobid."""
    set_status("working", "Redemarrage des containers...")

    await stop_containers()
    await asyncio.sleep(2)
    await start_containers()

    # Attendre que Grobid soit pret
    for _ in range(120):
        if await check_grobid_ready():
            set_status("idle")
            return {"status": "restarted", "grobid_ready": True}
        await asyncio.sleep(1)

    set_status("error", "Grobid non pret apres redemarrage")
    return {"status": "restarted", "grobid_ready": False}


@app.get("/docker/logs")
async def get_logs(lines: int = 100):
    """Recupere les logs du container Grobid."""
    try:
        containers = docker_client.containers.list(
            filters={"label": f"onyx.skill={SKILL_NAME}"}
        )

        logs = {}
        for c in containers:
            logs[c.name] = c.logs(tail=lines).decode("utf-8", errors="replace")

        return logs

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=WRAPPER_PORT)
