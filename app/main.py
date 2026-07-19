import logging
import time

from fastapi import FastAPI, Request

from app.exceptions import register_exception_handlers
from app.routers import health, mappings

# No handler/level configured here on purpose - uvicorn/systemd owns log
# configuration for this process; this module only emits to its logger and
# lets the root logger (propagate=True, the default) decide where it goes.
logger = logging.getLogger("pxe_map_api")

app = FastAPI(title="pxe-map-api")

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
