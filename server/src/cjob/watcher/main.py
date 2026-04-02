import logging
import pathlib
import signal
import time

from kubernetes import config as k8s_config

from cjob.config import get_settings
from cjob.db import create_session

from .node_sync import sync_node_resources
from .quota_sync import sync_flavor_quotas
from .reconciler import list_cjob_k8s_jobs, reconcile_cycle
from .resource_quota_sync import sync_resource_quotas

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
    sync_interval = settings.NODE_RESOURCE_SYNC_INTERVAL_SEC
    cycles_per_sync = max(1, sync_interval // interval)
    rq_sync_interval = settings.RESOURCE_QUOTA_SYNC_INTERVAL_SEC
    cycles_per_rq_sync = max(1, rq_sync_interval // interval)
    logger.info(
        "Watcher main loop started (interval=%ds, node_sync every %d cycles, "
        "rq_sync every %d cycles)",
        interval,
        cycles_per_sync,
        cycles_per_rq_sync,
    )

    cycle_count = 0
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

        # Sync node resources and flavor quotas every N cycles (including first cycle)
        if cycle_count % cycles_per_sync == 0:
            session = create_session()
            try:
                sync_node_resources(session, settings)
            except Exception:
                logger.exception("Error in node resource sync")
                session.rollback()
            finally:
                session.close()

            session = create_session()
            try:
                sync_flavor_quotas(session, settings)
            except Exception:
                logger.exception("Error in flavor quota sync")
                session.rollback()
            finally:
                session.close()

        # Sync resource quotas on a separate (shorter) cycle
        if cycle_count % cycles_per_rq_sync == 0:
            session = create_session()
            try:
                sync_resource_quotas(session, settings)
            except Exception:
                logger.exception("Error in resource quota sync")
                session.rollback()
            finally:
                session.close()

        cycle_count += 1
        LIVENESS_PATH.touch()
        time.sleep(interval)

    logger.info("Watcher stopped")


if __name__ == "__main__":
    run()
