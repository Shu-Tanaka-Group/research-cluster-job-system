import logging
import pathlib
import signal
import time

from kubernetes import config as k8s_config

from cjob.config import get_settings
from cjob.db import create_session

from .reconciler import list_cjob_k8s_jobs, reconcile_cycle

logger = logging.getLogger(__name__)

LIVENESS_PATH = pathlib.Path("/tmp/liveness")
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %d, shutting down", signum)
    _shutdown = True


def run():
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    logger.info("Watcher starting")
    k8s_config.load_incluster_config()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = settings.DISPATCH_BUDGET_CHECK_INTERVAL_SEC
    logger.info("Watcher main loop started (interval=%ds)", interval)

    while not _shutdown:
        session = create_session()
        try:
            k8s_jobs = list_cjob_k8s_jobs()
            reconcile_cycle(session, k8s_jobs)
            session.commit()
        except Exception:
            logger.exception("Error in reconcile cycle")
            session.rollback()
        finally:
            session.close()

        LIVENESS_PATH.touch()
        time.sleep(interval)

    logger.info("Watcher stopped")


if __name__ == "__main__":
    run()
