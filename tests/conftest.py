# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import io
import sys
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from museum_multimodal_lab.config import Settings
from museum_multimodal_lab.service import LabService


class FakeModels:
    def __init__(self, *, image_text: str = "架空展示 GX-418 の観測値は 13 単位です。"):
        self.image_text = image_text
        self.embed_calls: list[str] = []
        self.image_calls = 0
        self.answer_calls: list[tuple[str, list[dict[str, str]]]] = []
        self.fail_images = False
        self.fail_embedding = False
        self.forged_citation = False
        self.unverified_number = False

    def models(self) -> list[str]:
        return ["embeddinggemma:latest", "qwen3:4b-instruct-2507-q4_K_M", "qwen3.5:2b"]

    def supports_vision(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        from museum_multimodal_lab.providers import ModelError

        if self.fail_embedding:
            raise ModelError("埋め込みモデルがありません")
        self.embed_calls.append(text)
        return [1.0, 0.0, 0.25]

    def inspect_image(self, image: bytes) -> str:
        from museum_multimodal_lab.providers import ModelError

        self.image_calls += 1
        if self.fail_images:
            raise ModelError("画像モデルがありません")
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        return self.image_text

    def answer(self, question: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
        self.answer_calls.append((question, evidence))
        if self.forged_citation:
            return {"answer": "値は 31 です。", "source_ids": ["S999"]}
        if self.unverified_number:
            return {"answer": "値は 9999 です。", "source_ids": ["S1"]}
        if not evidence:
            return {"answer": "", "source_ids": []}
        first = evidence[0]["text"]
        if "31" in first:
            answer = "GX-418 の観測値は 31 単位です。"
        elif "13" in first:
            answer = "GX-418 の観測値は 13 単位です。"
        elif "7.2" in first:
            answer = "音声にある GX-418 の値は 7.2 です。"
        else:
            answer = first[:90]
        return {"answer": answer, "source_ids": [evidence[0]["id"]]}


class FakeAsr:
    def __init__(self):
        self.segments: list[dict[str, Any]] = [
            {"start_sec": 0.1, "end_sec": 0.7, "text": "GX-418 の値は 7.2 です。"},
        ]
        self.calls = 0

    def available(self) -> bool:
        return True

    def transcribe(self, path: Path) -> list[dict[str, Any]]:
        self.calls += 1
        assert path.is_file()
        return list(self.segments)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "local", ollama_url="http://127.0.0.1:11434")


@pytest.fixture
def fake_models() -> FakeModels:
    return FakeModels()


@pytest.fixture
def fake_asr() -> FakeAsr:
    return FakeAsr()


@pytest.fixture
def service(settings: Settings, fake_models: FakeModels, fake_asr: FakeAsr):
    instance = LabService(settings, fake_models, fake_asr)
    try:
        yield instance
    finally:
        instance.close()


def wait_for_job(service: LabService, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.job(job_id)
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not complete: {job_id}")


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (180, 90), "white").save(output, format="PNG")
    return output.getvalue()


def wav_bytes(seconds: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 8000 * seconds)
    return output.getvalue()
