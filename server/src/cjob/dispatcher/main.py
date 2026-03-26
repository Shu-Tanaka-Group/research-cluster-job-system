import logging
import pathlib
import signal
import time

from kubernetes import config as k8s_config

from cjob.config import get_settings
from cjob.db import create_session
from cjob.models import Job

from .k8s_job import PermanentK8sError, TemporaryK8sError, build_k8s_job, create_k8s_job
from .scheduler import (
    apply_gap_filling,
    cas_update_to_dispatching,
    fetch_dispatchable_jobs,
    increment_retry,
    mark_dispatched,
    mark_failed,
    reset_stale_dispatching,
)

logger = logging.getLogger(__name__)

LIVENESS_PATH = pathlib.Path("/tmp/liveness")
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %d, shutting down", signum)
    _shutdown = True


def dispatch_one(session, job, settings):
    """Dispatch a single job: CAS -> create K8s Job -> update DB."""
    ns, jid = job.namespace, job.job_id

    if not cas_update_to_dispatching(session, ns, jid):
        logger.debug("CAS failed for %s/%d (likely cancelled)", ns, jid)
        session.commit()
        return

    try:
        k8s_job_name = create_k8s_job(build_k8s_job(job, settings))
        mark_dispatched(session, ns, jid, k8s_job_name)
        session.commit()
    except TemporaryK8sError as e:
        session.rollback()
        # Re-fetch in a clean state to get current retry_count
        current_job = session.get(Job, (ns, jid))
        if current_job is None or current_job.status != "DISPATCHING":
            return

        if current_job.retry_count + 1 >= settings.DISPATCH_MAX_RETRIES:
            mark_failed(session, ns, jid, f"max retries exceeded: {e}")
        else:
            increment_retry(session, ns, jid, settings.DISPATCH_RETRY_INTERVAL_SEC)
        session.commit()
        logger.warning("Temporary K8s error for %s/%d: %s", ns, jid, e)
    except PermanentK8sError as e:
        session.rollback()
        mark_failed(session, ns, jid, str(e))
        session.commit()
        logger.error("Permanent K8s error for %s/%d: %s", ns, jid, e)


def run():
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    logger.info("Dispatcher starting")
    k8s_config.load_incluster_config()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Startup: reset stale DISPATCHING jobs
    session = create_session()
    try:
        reset_stale_dispatching(session)
    finally:
        session.close()

    logger.info(
        "Dispatcher main loop started (interval=%ds, batch_size=%d)",
        settings.DISPATCH_BUDGET_CHECK_INTERVAL_SEC,
        settings.DISPATCH_BATCH_SIZE,
    )

    while not _shutdown:
        session = create_session()
        try:
            candidates = fetch_dispatchable_jobs(session, settings)
            candidates = apply_gap_filling(session, candidates, settings)
            if candidates:
                logger.info("Found %d dispatchable jobs", len(candidates))
            for job in candidates:
                if _shutdown:
                    break
                dispatch_one(session, job, settings)
        except Exception:
            logger.exception("Error in dispatch cycle")
            session.rollback()
        finally:
            session.close()

        LIVENESS_PATH.touch()
        time.sleep(settings.DISPATCH_BUDGET_CHECK_INTERVAL_SEC)

    logger.info("Dispatcher stopped")


if __name__ == "__main__":
    run()
