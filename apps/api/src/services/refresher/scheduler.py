"""Daily tick for the learner refresher (see refresher.py). Same shape as the
nudge scheduler, minus the SaaS gate and the Redis day-lock: the only switch
is LEARNHOUSE_REFRESHER_ENABLED. Several replicas may each run the job; the
ledger's unique dedupe key means they collide in the database, not in an
inbox. Never raises during startup."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)
RUN_AT_HOUR_UTC = 10
STARTUP_DELAY_SECONDS = 45
_task: Optional[asyncio.Task] = None


def _seconds_until_next_run(now: datetime) -> float:
    target = now.replace(hour=RUN_AT_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _run_once() -> None:
    from src.core.events.database import _async_session_factory
    from src.services.refresher.refresher import run_refreshers
    async with _async_session_factory() as db_session:
        stats = await run_refreshers(db_session)
    logger.info("Refresher run: %s", stats)


async def _loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await _run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed run must not kill the loop
            logger.exception("Refresher run failed, will retry tomorrow: %s", exc)
        await asyncio.sleep(_seconds_until_next_run(datetime.now(timezone.utc)))


def start_scheduler() -> None:
    """Start the daily tick, unless the feature is switched off. Never raises."""
    global _task
    try:
        from src.services.refresher.refresher import refresher_enabled
        if not refresher_enabled():
            logger.info("Refresher scheduler idle: LEARNHOUSE_REFRESHER_ENABLED is not set")
            return
        _task = asyncio.create_task(_loop())
        logger.info("Refresher scheduler started (daily at %02d:00 UTC)", RUN_AT_HOUR_UTC)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Refresher scheduler not started: %s", exc)


async def stop_scheduler() -> None:
    """Stop the tick. Never raises, for the same reason as `start_scheduler`."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        _task = None
