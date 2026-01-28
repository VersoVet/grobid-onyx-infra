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
from sse_starlette.sse import EventSourceResponse
from contextlib import asynccontextmanager
from pathlib import Path
import docker
import asyncio
import httpx
import uvicorn
import logging
import os
import time
import json

from src.events import (
    event_manager,
    emit_extraction_start,
    emit_extraction_success,
    emit_extraction_failure,
    emit_container_event
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("grobid-onyx-infra")

# Configuration (via env ou defaut)
SKILL_NAME = os.getenv("SKILL_NAME", "grobid-onyx-infra")
BRAIN_AREA = os.getenv("BRAIN_AREA", "prefrontal")
WRAPPER_PORT = int(os.getenv("WRAPPER_PORT", "8072"))  # 8071 est utilisé par GROBID admin
GROBID_PORT = int(os.getenv("GROBID_PORT", "8070"))
GROBID_URL = f"http://localhost:{GROBID_PORT}"
DOCKER_COMPOSE_PATH = Path(__file__).parent.parent / "docker" / "docker-compose.yml"

# Docker client
docker_client = docker.from_env()

# Onyx SDK (optionnel, ne bloque pas si non disponible)
try:
    from onyx_sdk import OnyxClient
    onyx_client = OnyxClient(SKILL_NAME, BRAIN_AREA, port=WRAPPER_PORT)
    HAS_ONYX_SDK = True
    logger.info("Onyx SDK charge")
except ImportError:
    onyx_client = None
    HAS_ONYX_SDK = False
    logger.info("Onyx SDK non disponible")


def set_status(status: str, message: str = ""):
    """Set Onyx status if SDK available."""
    if onyx_client:
        try:
            if status == "working":
                onyx_client.working(message)
            elif status == "idle":
                onyx_client.idle()
            elif status == "error":
                onyx_client.error(message)
        except Exception as e:
            logger.warning(f"Erreur SDK Onyx: {e}")


async def start_containers():
    """Demarre les containers Docker via docker-compose (async).

    Si GROBID est déjà en cours d'exécution, skip le démarrage.
    """
    # Vérifier d'abord si GROBID est déjà prêt
    if await check_grobid_ready():
        logger.info("GROBID déjà en cours d'exécution, skip docker-compose")
        return

    set_status("working", "Demarrage du conteneur Grobid...")
    logger.info("Demarrage des containers Docker...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(DOCKER_COMPOSE_PATH), "up", "-d",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=DOCKER_COMPOSE_PATH.parent
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            # Si c'est juste un conflit de nom de container (GROBID existe déjà), c'est OK
            if "already in use" in error_msg or "Conflict" in error_msg:
                logger.info("Container GROBID existe déjà, continuing...")
                return
            set_status("error", f"Echec demarrage: {error_msg}")
            logger.error(f"Docker compose failed: {error_msg}")
            raise RuntimeError(f"Docker compose failed: {error_msg}")

        logger.info("Containers demarres")

    except FileNotFoundError:
        error_msg = "docker compose non trouve"
        set_status("error", error_msg)
        logger.error(error_msg)
        raise RuntimeError(error_msg)


async def stop_containers():
    """Arrete les containers Docker (async)."""
    logger.info("Arret des containers Docker...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(DOCKER_COMPOSE_PATH), "down",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=DOCKER_COMPOSE_PATH.parent
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(f"Docker compose down warning: {stderr.decode() if stderr else ''}")
        else:
            logger.info("Containers arretes")

    except Exception as e:
        logger.warning(f"Erreur arret containers: {e}")


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
        logger.error(f"Erreur check containers: {e}")
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
    try:
        # Startup
        await start_containers()

        # Attendre que Grobid soit pret (timeout 3 minutes pour chargement des modeles)
        set_status("working", "Chargement des modeles Grobid...")
        logger.info("Attente chargement modeles Grobid...")

        for i in range(180):
            if await check_grobid_ready():
                break
            await asyncio.sleep(1)
            if i % 30 == 0 and i > 0:
                set_status("working", f"Chargement des modeles... ({i}s)")
                logger.info(f"Chargement modeles: {i}s...")

        if await check_grobid_ready():
            set_status("idle")
            logger.info("Grobid pret")
        else:
            set_status("error", "Grobid n'a pas demarre correctement")
            logger.error("Grobid n'a pas demarre dans le delai imparti")

        yield

    finally:
        # Shutdown (toujours execute)
        await stop_containers()


# FastAPI app
app = FastAPI(
    title="Grobid Onyx Infrastructure",
    description="Service d'extraction de metadonnees PDF via Grobid avec monitoring SSE",
    version="1.1.0",
    lifespan=lifespan
)


# === SSE Endpoints ===

@app.get("/events")
async def sse_events():
    """Stream temps réel des événements d'extraction via SSE."""
    async def event_generator():
        queue = await event_manager.subscribe()
        try:
            # Event de connexion
            yield {
                "event": "connected",
                "data": json.dumps({
                    "type": "connected",
                    "message": "Connected to grobid-onyx-infra SSE stream",
                    "subscribers": event_manager.subscriber_count
                })
            }

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event["type"],
                        "data": json.dumps(event)
                    }
                except asyncio.TimeoutError:
                    # Ping keepalive
                    yield {"event": "ping", "data": "{}"}

        except asyncio.CancelledError:
            pass
        finally:
            await event_manager.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@app.get("/events/history")
async def events_history(limit: int = 50):
    """Historique des derniers événements."""
    return {
        "events": event_manager.get_history(limit),
        "total": len(event_manager.history),
        "limit": limit
    }


@app.get("/health")
async def health():
    """Health check unifie (containers + Grobid API)."""
    container_health = await asyncio.to_thread(check_containers_health)
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
    container_health = await asyncio.to_thread(check_containers_health)
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
    logger.info(f"processFulltextDocument: {input.filename}")

    # Lire le contenu et mesurer la taille
    file_content = await input.read()
    file_size_kb = len(file_content) // 1024

    # Émettre event de début
    await emit_extraction_start(input.filename, "processFulltextDocument", file_size_kb)

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            files = {"input": (input.filename, file_content, input.content_type)}
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

            latency_ms = (time.time() - start_time) * 1000
            response_size_kb = len(response.content) // 1024

            # Émettre event de succès
            await emit_extraction_success(
                input.filename,
                "processFulltextDocument",
                latency_ms,
                response_size_kb,
                response.status_code
            )

            set_status("idle")
            logger.info(f"processFulltextDocument: {input.filename} - {response.status_code} ({latency_ms:.0f}ms)")
            return Response(
                content=response.content,
                media_type="application/xml",
                status_code=response.status_code
            )

    except httpx.TimeoutException:
        latency_ms = (time.time() - start_time) * 1000
        await emit_extraction_failure(input.filename, "processFulltextDocument", "Timeout after 300s", latency_ms)
        set_status("error", "Timeout")
        logger.error(f"Timeout: {input.filename}")
        raise HTTPException(status_code=504, detail="Grobid processing timeout")
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        await emit_extraction_failure(input.filename, "processFulltextDocument", str(e), latency_ms)
        set_status("error", "Erreur traitement")
        logger.error(f"Erreur: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")


@app.post("/api/processHeaderDocument")
async def process_header_document(
    input: UploadFile = File(...),
    consolidateHeader: int = Form(default=1)
):
    """Extrait uniquement les metadonnees d'en-tete (titre, auteurs, abstract)."""
    set_status("working", f"Extraction header: {input.filename}")
    logger.info(f"processHeaderDocument: {input.filename}")

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

    except httpx.TimeoutException:
        set_status("error", "Timeout")
        raise HTTPException(status_code=504, detail="Grobid processing timeout")
    except Exception as e:
        set_status("error", "Erreur traitement")
        logger.error(f"Erreur processHeaderDocument: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")


@app.post("/api/processReferences")
async def process_references(
    input: UploadFile = File(...),
    consolidateCitations: int = Form(default=0)
):
    """Extrait et parse les references bibliographiques."""
    set_status("working", f"Extraction references: {input.filename}")
    logger.info(f"processReferences: {input.filename}")

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

    except httpx.TimeoutException:
        set_status("error", "Timeout")
        raise HTTPException(status_code=504, detail="Grobid processing timeout")
    except Exception as e:
        set_status("error", "Erreur traitement")
        logger.error(f"Erreur processReferences: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")


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
        logger.error(f"Erreur processCitation: {e}")
        raise HTTPException(status_code=500, detail="Internal processing error")


# === Endpoints de gestion Docker ===

@app.post("/docker/restart")
async def restart_containers():
    """Redemarre les containers Grobid."""
    set_status("working", "Redemarrage des containers...")
    logger.info("Redemarrage containers demande")

    await stop_containers()
    await asyncio.sleep(2)
    await start_containers()

    # Attendre que Grobid soit pret
    for _ in range(120):
        if await check_grobid_ready():
            set_status("idle")
            logger.info("Redemarrage termine, Grobid pret")
            return {"status": "restarted", "grobid_ready": True}
        await asyncio.sleep(1)

    set_status("error", "Grobid non pret apres redemarrage")
    logger.warning("Grobid non pret apres redemarrage")
    return {"status": "restarted", "grobid_ready": False}


@app.get("/docker/logs")
async def get_logs(lines: int = 100):
    """Recupere les logs du container Grobid."""
    try:
        containers = await asyncio.to_thread(
            docker_client.containers.list,
            filters={"label": f"onyx.skill={SKILL_NAME}"}
        )

        logs = {}
        for c in containers:
            logs[c.name] = c.logs(tail=lines).decode("utf-8", errors="replace")

        return logs

    except Exception as e:
        logger.error(f"Erreur get_logs: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve logs")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=WRAPPER_PORT)
