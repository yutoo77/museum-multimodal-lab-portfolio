# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from .config import Settings
from .extraction import ExtractionError
from .providers import (
    AsrProvider,
    FasterWhisperProvider,
    ModelError,
    ModelProvider,
    OllamaProvider,
)
from .service import ConflictError, LabService, NotFoundError


class UnitPatch(BaseModel):
    corrected_text: str = Field(max_length=20_000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1500)


def create_app(
    settings: Settings | None = None,
    models: ModelProvider | None = None,
    asr: AsrProvider | None = None,
) -> FastAPI:
    test_client = settings is not None and models is not None and asr is not None
    settings = settings or Settings.from_env()
    models = models or OllamaProvider(settings)
    asr = asr or FasterWhisperProvider(settings)
    service = LabService(settings, models, asr)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service.close()

    app = FastAPI(title="Museum Multimodal Lab", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    local_origins = [f"http://127.0.0.1:{settings.frontend_port}", f"http://localhost:{settings.frontend_port}"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"] if test_client else ["127.0.0.1", "localhost"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=local_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def reject_foreign_browser_writes(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if (origin and origin not in local_origins) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "外部のWebページからは操作できません"})
        return await call_next(request)

    @app.exception_handler(NotFoundError)
    async def not_found(_, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ExtractionError)
    async def bad_document(_, exc: ExtractionError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ModelError)
    async def model_unavailable(_, exc: ModelError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/api/health")
    def health():
        return service.health()

    @app.post("/api/documents", status_code=201)
    async def upload(file: Annotated[UploadFile, File()]):
        try:
            data = await file.read(settings.max_upload_bytes + 1)
            return service.upload(file.filename or "", data)
        finally:
            await file.close()

    @app.get("/api/documents")
    def list_documents():
        return service.list_documents()

    @app.get("/api/documents/{document_id}")
    def document(document_id: str):
        return service.document(document_id)

    @app.get("/api/documents/{document_id}/original")
    def original(document_id: str):
        path, mime_type, name = service.original_path(document_id)
        return FileResponse(path, media_type=mime_type, filename=name, content_disposition_type="inline")

    @app.get("/api/documents/{document_id}/pages/{page_number}/preview")
    def pdf_preview(document_id: str, page_number: int):
        path, mime_type, _ = service.original_path(document_id)
        if mime_type != "application/pdf":
            raise ExtractionError("PDF資料ではありません")
        import pymupdf as fitz

        with fitz.open(path) as document:
            if page_number < 1 or page_number > document.page_count:
                raise NotFoundError("指定したPDFページがありません")
            page = document[page_number - 1]
            scale = min(1.5, 1200 / page.rect.width, 1600 / page.rect.height)
            preview = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
        return Response(preview, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/documents/{document_id}/extract", status_code=202)
    def extract(document_id: str):
        return service.extract(document_id)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return service.job(job_id)

    @app.patch("/api/extracted-units/{unit_id}")
    def edit_unit(unit_id: str, body: UnitPatch):
        return service.edit_unit(unit_id, body.corrected_text)

    @app.post("/api/documents/{document_id}/approve", status_code=202)
    def approve(document_id: str):
        return service.approve(document_id)

    @app.post("/api/documents/{document_id}/reindex", status_code=202)
    def reindex(document_id: str):
        return service.reindex(document_id)

    @app.delete("/api/documents/{document_id}", status_code=204)
    def delete(document_id: str):
        service.delete(document_id)

    @app.post("/api/ask")
    def ask(body: AskRequest):
        return service.ask(body.question)

    return app


class _LazyApp:
    """Let uvicorn keep `api:app` without opening the live DB on a test import."""

    def __init__(self) -> None:
        self._instance: FastAPI | None = None
        self._lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = create_app()
        await self._instance(scope, receive, send)


app = _LazyApp()
