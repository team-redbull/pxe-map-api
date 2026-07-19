"""Central place where app/mapping.py's plain-Python exceptions are
translated into HTTP responses, registered once in app/main.py so every
router gets this behavior automatically instead of each route needing its
own try/except.

FileNotFoundError/FileExistsError/ValueError are generic stdlib types, which
would be too broad to catch globally in a bigger app - it's a safe choice
here specifically because app/mapping.py (pure, no FastAPI dependency) is
the only code in this service that touches the filesystem or does input
validation, so these types are unambiguous in this codebase.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.mapping import MappingFormatError


async def _mapping_not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "mapping not found"},
    )


async def _mapping_already_exists(request: Request, exc: FileExistsError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "mapping already exists"},
    )


async def _mapping_corrupt(request: Request, exc: MappingFormatError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )


async def _invalid_input(request: Request, exc: ValueError) -> JSONResponse:
    # Defense in depth: every route validates its MAC/URL before calling
    # into mapping.py (via Pydantic validators in schemas.py, or the
    # mac-path dependencies in dependencies.py), so this should be
    # unreachable. If mapping.py's internal checks ever catch something the
    # API layer didn't, fail with a clean 422 instead of an unhandled 500.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc)},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(FileNotFoundError, _mapping_not_found)
    app.add_exception_handler(FileExistsError, _mapping_already_exists)
    app.add_exception_handler(MappingFormatError, _mapping_corrupt)
    app.add_exception_handler(ValueError, _invalid_input)
