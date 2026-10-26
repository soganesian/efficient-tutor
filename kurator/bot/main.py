"""Entry point: long polling (webhook is not used in v1)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from kurator.config import load_config, load_secrets
from kurator.core.app import Kurator
from kurator.core.clock import RealClock
from kurator.core.scheduling import build_scheduler, ensure_sessions, tick
from kurator.gateway.telegram import TelegramGateway
from kurator.grading.llm import build_client
from kurator.grading.pipeline import Grader
from kurator.logging_setup import log_event, setup_logging
from kurator.reports.sheets import SheetsExporter
from kurator.storage.db import init_engine
from kurator.storage.migrate import run_migrations

log = logging.getLogger(__name__)


def build_kurator(gateway) -> Kurator:
    cfg = load_config()
    secrets = load_secrets()
    engine = init_engine(cfg.abs_path(cfg.storage.db_path))
    applied = run_migrations(engine)
    if applied:
        log_event(log, "migrations.applied", versions=applied)

    client = build_client(cfg, secrets)
    grader = Grader(client, cfg) if client else None
    if grader is None:
        log_event(
            log,
            "grader.disabled",
            provider=cfg.llm.provider,
            reason="no key for the selected provider",
        )

    sheets = SheetsExporter(engine, cfg, secrets.google_sa_json)
    return Kurator(engine, gateway, cfg, RealClock(), grader=grader, sheets=sheets)


async def sandbox_ticks(registry) -> None:
    """One loop for all demo families instead of a scheduler job per sandbox."""
    while True:
        await asyncio.sleep(60)
        for box in registry.all():
            try:
                ensure_sessions(box.kurator)
                await tick(box.kurator)
            except Exception:  # noqa: BLE001 — one broken sandbox must not stop the rest
                log.exception("sandbox tick failed")


async def run_sandbox(bot: Bot) -> None:
    from kurator.bot import demo_handlers, handlers
    from kurator.sandbox import SandboxRegistry

    cfg, secrets = load_config(), load_secrets()
    client = build_client(cfg, secrets)
    registry = SandboxRegistry(cfg, TelegramGateway(bot), lambda: None)
    registry.grader_factory = (lambda: Grader(client, registry.cfg)) if client else (lambda: None)

    dispatcher = Dispatcher()
    dispatcher.include_router(demo_handlers.setup(registry))
    dispatcher.include_router(handlers.setup(None, registry))
    ticker = asyncio.create_task(sandbox_ticks(registry))
    me = await bot.get_me()
    log_event(
        log, "bot.started", username=me.username, mode="sandbox", grader="on" if client else "off"
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        ticker.cancel()
        await bot.session.close()


async def run() -> None:
    setup_logging()
    secrets = load_secrets()
    bot = Bot(token=secrets.bot_token)
    if load_config().sandbox.enabled:
        await run_sandbox(bot)
        return
    kurator = build_kurator(TelegramGateway(bot))

    from kurator.bot import handlers

    dispatcher = Dispatcher()
    dispatcher.include_router(handlers.setup(kurator))

    created = ensure_sessions(kurator)
    log_event(log, "sessions.ensured", created=len(created))

    scheduler = build_scheduler(kurator)
    scheduler.start()

    me = await bot.get_me()
    log_event(
        log, "bot.started", username=me.username, id=me.id, grader="on" if kurator.grader else "off"
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
