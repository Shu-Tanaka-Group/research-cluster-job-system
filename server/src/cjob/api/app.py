import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cjob.config import get_settings

from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load K8s config at startup
    try:
        from kubernetes import config as k8s_config

        k8s_config.load_incluster_config()
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to load incluster K8s config. "
            "TokenReview auth will not work outside a K8s cluster."
        )
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    app = FastAPI(title="CJob Submit API", lifespan=lifespan)
    app.include_router(router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
