# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import io
from pathlib import Path

import av
import httpx
import pymupdf as fitz
import pytest
from conftest import FakeAsr, FakeModels
from museum_multimodal_lab.config import Settings
from museum_multimodal_lab.extraction import (
    ExtractionError,
    _audio_duration,
    extract_document,
    validate_upload,
)
from museum_multimodal_lab.providers import ModelError, OllamaProvider
from PIL import Image


def _image_bytes(format_name: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(output, format=format_name)
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "data"),
    [("wrong.png", _image_bytes("JPEG")), ("wrong.jpg", _image_bytes("PNG"))],
)
def test_image_extension_must_match_actual_format(settings: Settings, name: str, data: bytes) -> None:
    with pytest.raises(ExtractionError):
        validate_upload(name, data, settings)


def test_single_vector_drawing_pdf_page_is_not_silently_skipped(
    tmp_path: Path, settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr
) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Diagram labels A and B")
    page.draw_line((72, 100), (300, 130), color=(0, 0, 0), width=2)
    path = tmp_path / "single-diagram.pdf"
    path.write_bytes(pdf.tobytes())
    pdf.close()

    units, label, warning = extract_document(path, "application/pdf", settings, fake_models, fake_asr)
    assert [(unit["kind"], unit["page_number"]) for unit in units] == [
        ("pdf_text", 1),
        ("image_interpretation", 1),
    ]
    assert fake_models.image_calls == 1
    assert label == f"PyMuPDF + {settings.vision_model}"
    assert warning is None


def _encoded_silence(format_name: str, codec: str) -> bytes:
    output = io.BytesIO()
    with av.open(output, mode="w", format=format_name) as container:
        stream = container.add_stream(codec, rate=44_100)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=44_100)
        frame.sample_rate = 44_100
        frame.planes[0].update(b"\x00\x00" * 44_100)
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "format_name", "codec", "mime_type"),
    [
        ("synthetic.mp3", "mp3", "mp3", "audio/mpeg"),
        ("synthetic.m4a", "mp4", "aac", "audio/mp4"),
    ],
)
def test_encoded_audio_is_validated_and_decoded_before_fake_asr(
    tmp_path: Path,
    settings: Settings,
    fake_models: FakeModels,
    fake_asr: FakeAsr,
    name: str,
    format_name: str,
    codec: str,
    mime_type: str,
) -> None:
    data = _encoded_silence(format_name, codec)
    assert validate_upload(name, data, settings) == (mime_type, Path(name).suffix)
    path = tmp_path / name
    path.write_bytes(data)
    assert 0 < _audio_duration(path) < 2

    units, label, warning = extract_document(path, mime_type, settings, fake_models, fake_asr)
    assert len(units) == 1
    assert units[0]["kind"] == "audio_transcript"
    assert (units[0]["start_sec"], units[0]["end_sec"]) == (0.1, 0.7)
    assert fake_asr.calls == 1
    assert label == f"faster-whisper:{settings.asr_model}"
    assert warning is None


def test_local_model_invalid_metadata_becomes_clear_model_error(settings: Settings) -> None:
    payloads: dict[str, dict] = {"/api/tags": {"models": None}, "/api/show": {"capabilities": None}}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[request.url.path])

    provider = OllamaProvider(settings)
    provider.client.close()
    provider.client = httpx.Client(base_url=settings.ollama_url, transport=httpx.MockTransport(respond), trust_env=False)
    try:
        with pytest.raises(ModelError, match="モデル一覧"):
            provider.models()
        with pytest.raises(ModelError, match="対応形式"):
            provider.supports_vision()
    finally:
        provider.client.close()


def test_local_model_malformed_outputs_are_not_indexed(settings: Settings) -> None:
    payloads: dict[str, dict] = {
        "/api/tags": {"models": [{"name": settings.embedding_model}, {"name": settings.vision_model}, {"name": settings.answer_model}]},
        "/api/show": {"capabilities": ["vision"]},
        "/api/embed": {"embeddings": [["not-a-number"]]},
        "/api/generate": {"response": None},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[request.url.path])

    provider = OllamaProvider(settings)
    provider.client.close()
    provider.client = httpx.Client(base_url=settings.ollama_url, transport=httpx.MockTransport(respond), trust_env=False)
    try:
        with pytest.raises(ModelError, match="埋め込みの値"):
            provider.embed("確認")
        with pytest.raises(ModelError, match="画像から文字"):
            provider.inspect_image(_image_bytes("PNG"))
        with pytest.raises(ModelError, match="回答の形式"):
            provider.answer("確認", [{"id": "S1", "name": "架空資料", "kind": "pdf_text", "text": "確認"}])
    finally:
        provider.client.close()


def test_loopback_model_listing_cannot_enable_remote_inference(settings: Settings) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="ローカルモデル名"):
        replace(settings, answer_model="qwen3:4b-cloud")
    received = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request.url.path)
        return httpx.Response(200, json={"models": [{"name": settings.answer_model, "remote_host": "remote.invalid"}]})

    provider = OllamaProvider(settings)
    provider.client.close()
    provider.client = httpx.Client(base_url=settings.ollama_url, transport=httpx.MockTransport(respond), trust_env=False)
    try:
        assert provider.models() == []
        with pytest.raises(ModelError, match="ローカルモデル"):
            provider.plan_retrieval("fixture", 2)
        assert received == ["/api/tags", "/api/tags"]
    finally:
        provider.client.close()
