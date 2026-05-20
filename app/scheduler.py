"""APScheduler wrapper that runs incremental sync hourly."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _make_sync_job(app):
    """Return a closure that runs sync inside the Flask app context."""
    def _job():
        with app.app_context():
            from .graph_client import GraphClient
            from .sync_service import run_sync
            from .config import Config

            cfg = Config()
            if not cfg.graph_configured:
                logger.warning("Scheduled sync skipped – Graph credentials not configured.")
                return

            client = GraphClient(
                tenant_id=cfg.TENANT_ID,
                client_id=cfg.CLIENT_ID,
                client_secret=cfg.CLIENT_SECRET,
                scope=cfg.GRAPH_SCOPE,
            )
            logger.info("Scheduled sync starting.")
            stats = run_sync(
                client,
                lookback_minutes=cfg.SYNC_LOOKBACK_MINUTES,
                page_size=cfg.GRAPH_PAGE_SIZE,
            )
            logger.info("Scheduled sync finished: %s", stats)

    return _job


def _make_archive_job(app):
    """Return a closure that runs the archive sweep inside the Flask app context."""
    def _job():
        with app.app_context():
            from .archive_service import run_archive_sweep
            from .config import Config

            cfg = Config()
            if not cfg.archive_configured:
                logger.info("Scheduled archive skipped – ARCHIVE_ENABLED=false or R2 not configured.")
                return
            logger.info("Scheduled archive sweep starting.")
            stats = run_archive_sweep(cfg)
            logger.info("Scheduled archive sweep finished: %s", stats)

    return _job


def start_scheduler(app) -> BackgroundScheduler:
    global _scheduler
    from .config import Config

    cfg = Config()
    interval_minutes = cfg.SYNC_INTERVAL_MINUTES

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _make_sync_job(app),
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="incremental_sync",
        name="M365 Sign-In Log Sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _make_archive_job(app),
        trigger=CronTrigger(hour=3, minute=15),  # 03:15 UTC nightly
        id="archive_sweep",
        name="R2 Archive Sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started – sync every %d min; archive sweep at 03:15 UTC.",
        interval_minutes,
    )
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
