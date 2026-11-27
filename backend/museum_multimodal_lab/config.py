# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cloud_model_name(name: str) -> bool:
    return bool(re.search(r"(?:^|[:-])cloud(?=[:-]|$)", name, re.IGNORECASE))


def validate_ollama_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("Ollama の接続先は http://127.0.0.1:<port> のみ指定できます")
    if parsed.query or parsed.fragment or not parsed.port:
        raise ValueError("Ollama の接続先にポートを指定してください")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ValueError("Ollama の接続先は IP アドレスで指定してください") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError("Ollama の接続先はローカルに限ります")
    return f"http://{address}:{parsed.port}"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ollama_url: str
    frontend_port: int = 5174
    answer_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    embedding_model: str = "embeddinggemma:latest"
    vision_model: str = "qwen3.5:2b"
    asr_model: str = "tiny"
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 10
    max_audio_seconds: int = 120
    max_image_pixels: int = 20_000_000
    max_vlm_pdf_pages: int = 3
    visual_model_dir: Path | None = None
    visual_assist: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "ollama_url", validate_ollama_url(self.ollama_url))
        if self.visual_model_dir is not None:
            object.__setattr__(self, "visual_model_dir", Path(self.visual_model_dir).resolve())
        if not 1 <= self.frontend_port <= 65535:
            raise ValueError("画面のポート番号は1〜65535で指定してください")
        for name in ("answer_model", "embedding_model", "vision_model"):
            value = getattr(self, name)
            if not value or "/" in value or "\\" in value or "@" in value or value.startswith("http") or cloud_model_name(value):
                raise ValueError(f"無効なローカルモデル名: {name}")

    @classmethod
    def from_env(cls) -> Settings:
        visual_dir = os.getenv("MML_VISUAL_MODEL_DIR")
        return cls(
            data_dir=Path(os.getenv("MML_DATA_DIR", str(PROJECT_ROOT / "data"))),
            ollama_url=os.getenv("MML_OLLAMA_URL", "http://127.0.0.1:11436"),
            frontend_port=int(os.getenv("MML_FRONTEND_PORT", "5174")),
            answer_model=os.getenv("MML_ANSWER_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
            embedding_model=os.getenv("MML_EMBEDDING_MODEL", "embeddinggemma:latest"),
            vision_model=os.getenv("MML_VISION_MODEL", "qwen3.5:2b"),
            asr_model=os.getenv("MML_ASR_MODEL", "tiny"),
            visual_model_dir=Path(visual_dir) if visual_dir else None,
            visual_assist=os.getenv("MML_VISUAL_ASSIST") == "1",
        )
