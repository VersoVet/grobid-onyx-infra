"""
SSE Event Manager pour grobid-onyx-infra

Permet le monitoring temps réel des extractions GROBID.
"""

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Optional


class EventManager:
    """Gestionnaire d'événements SSE avec historique."""

    def __init__(self, max_history: int = 100):
        self.subscribers: list[asyncio.Queue] = []
        self.history: deque = deque(maxlen=max_history)
        self._lock = asyncio.Lock()

    async def emit(self, event_type: str, data: dict):
        """Émet un événement à tous les subscribers."""
        event = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now().isoformat()
        }
        self.history.append(event)

        # Notifier tous les subscribers
        async with self._lock:
            dead_queues = []
            for queue in self.subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    dead_queues.append(queue)

            # Nettoyer les queues mortes
            for q in dead_queues:
                self.subscribers.remove(q)

    async def subscribe(self) -> asyncio.Queue:
        """Crée une nouvelle subscription SSE."""
        queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self.subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue):
        """Supprime une subscription."""
        async with self._lock:
            if queue in self.subscribers:
                self.subscribers.remove(queue)

    def get_history(self, limit: int = 50) -> list:
        """Retourne les derniers événements."""
        return list(self.history)[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self.subscribers)


# Singleton global
event_manager = EventManager()


# Helpers pour émettre des événements typés
async def emit_extraction_start(filename: str, endpoint: str, file_size_kb: int):
    """Émis au début d'une extraction."""
    await event_manager.emit("extraction_start", {
        "filename": filename,
        "endpoint": endpoint,
        "file_size_kb": file_size_kb
    })


async def emit_extraction_success(
    filename: str,
    endpoint: str,
    latency_ms: float,
    response_size_kb: int,
    status_code: int
):
    """Émis après une extraction réussie."""
    await event_manager.emit("extraction_success", {
        "filename": filename,
        "endpoint": endpoint,
        "latency_ms": round(latency_ms, 1),
        "response_size_kb": response_size_kb,
        "status_code": status_code
    })


async def emit_extraction_failure(
    filename: str,
    endpoint: str,
    error: str,
    latency_ms: float
):
    """Émis après un échec d'extraction."""
    await event_manager.emit("extraction_failure", {
        "filename": filename,
        "endpoint": endpoint,
        "error": error[:200],
        "latency_ms": round(latency_ms, 1)
    })


async def emit_container_event(event_type: str, details: dict):
    """Émis pour les événements Docker."""
    await event_manager.emit(f"container_{event_type}", details)
