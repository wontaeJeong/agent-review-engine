"""Run a dedicated worker: python -m agent_review_engine.worker"""
from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from .config import get_settings
from .engine import ReviewEngine
from .github import GitHubAPI
from .providers import JevClassifier, LLMReviewer
from .store import Store


async def main() -> None:
    settings = get_settings()
    missing = settings.missing()
    if missing:
        raise RuntimeError("Missing required service settings: " + ", ".join(missing))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        github = GitHubAPI(settings, client)
        engine = ReviewEngine(settings, Store(settings.database_url, auto_create=settings.schema_auto_create), github,
                              JevClassifier(settings, client), LLMReviewer(settings, client))
        await engine.worker(stop)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
