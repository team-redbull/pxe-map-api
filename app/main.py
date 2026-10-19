import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import get_config
from app.exceptions import register_exception_handlers
from app.routers import health, infra_env_boot, mappings

# No handler/level configured here on purpose - uvicorn/systemd owns log
# configuration for this process; this module only emits to its logger and
# lets the root logger (propagate=True, the default) decide where it goes.
logger = logging.getLogger("pxe_map_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # get_config() is normally only resolved lazily per-request (via
    # Depends), which means a missing PXE_MAP_DIR wouldn't surface until
    # the first request to a route that needs it. Calling it here forces
    # that RuntimeError before the server starts accepting connections at
    # all, regardless of whether it's launched via the uvicorn CLI or
    # `python -m app.main`.
    get_config()
    yield


app = FastAPI(title="pxe-map-api", lifespan=lifespan)

register_exception_handlers(app)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Logs exactly once per request, for every route on every router,
    regardless of whether the response came from a normal return or one of
    the global exception handlers - the ASGI-middleware equivalent of
    'wrap every route with a decorator', without touching each route."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.exception("%s %s crashed after %.1fms", request.method, request.url.path, elapsed_ms)
        raise
    elapsed_ms = (time.monotonic() - start) * 1000
    level = logging.INFO if response.status_code >= 400 else logging.DEBUG
    logger.log(
        level, "%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, elapsed_ms
    )
    return response


app.include_router(health.router)
app.include_router(mappings.router)
app.include_router(infra_env_boot.router)


if __name__ == "__main__":
    # `uvicorn app.main:app` (the CLI form) resolves --host/--port before
    # it ever imports this module, so a bare .env can't reach it there -
    # uvicorn only reads real environment variables (UVICORN_HOST/PORT) at
    # that point, not this project's .env file. Running this module
    # directly sidesteps that: get_config() has already run load_dotenv()
    # by the time we get here, so PXE_MAP_BIND from .env drives the bind
    # address with no flags and no manual export/source.
    import uvicorn

    config = get_config()
    host, _, port = config.pxe_map_bind.rpartition(":")
    if not host or not port.isdigit():
        raise RuntimeError(
            f"PXE_MAP_BIND={config.pxe_map_bind!r} is not a valid host:port pair"
        )
    uvicorn.run(app, host=host, port=int(port))
