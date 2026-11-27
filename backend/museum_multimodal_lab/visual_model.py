# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

"""Offline Japanese CLIP adapter for one explicitly prepared ONNX artifact set."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

ARTIFACT_REVISION = "c924148be2e25b6e4d98e66d8dc1768adb72d079"
ARTIFACT_REPO = "AUXOUT-TEAM/clip-japanese-base-v2-onnx"
ARTIFACTS = {
    "onnx/vision_model_q4f16.onnx": (47651034, "ef224e6127d6b66e79eab7ec2b4245acfa9ca070f0c66dbb27e93732bf09a76f"),
    "onnx/text_model_q4f16.onnx": (94564770, "653d6e3393d4c989cee268a5280fccd96dab1f6a93223e56bcb6cb9757503cb6"),
    "tokenizer.json": (2411619, "c11bafa5bdbcc1470ef2c81c6f32686547acace5c5e5a6b67a1d3b8501df6b86"),
}
IMAGE_SIZE = 224
TEXT_MAX_LENGTH = 77
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)


def _artifact_path(root: Path, filename: str) -> Path:
    path = root / filename
    size, expected_hash = ARTIFACTS[filename]
    try:
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f"Missing or incomplete visual model artifact: {filename}")
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
    except OSError as exc:
        raise ValueError(f"Could not read visual model artifact: {filename}") from exc
    if digest != expected_hash:
        raise ValueError(f"Unrecognized visual model artifact: {filename}")
    return path


class LocalVisualModel:
    """256-D text/image joint space; CPU sessions always use local files only."""

    dimension = 256
    model_id = f"line-corporation/clip-japanese-base-v2:onnx-q4f16@{ARTIFACT_REVISION}:letterbox-cls-v1"

    def __init__(self, model_dir: Path):
        # Verify the full artifact set before importing or loading either tower.
        vision_path, text_path, tokenizer_path = [_artifact_path(model_dir, filename) for filename in ARTIFACTS]

        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        if self._tokenizer.token_to_id("[CLS]") != 4:
            raise ValueError("Visual tokenizer [CLS] ID is incompatible")
        self._tokenizer.enable_truncation(max_length=TEXT_MAX_LENGTH - 1)

        session_config = ort.SessionOptions()
        session_config.intra_op_num_threads = 2
        session_config.inter_op_num_threads = 1
        # Do not include the Azure execution provider even when installed.
        # No model loader, cloud fallback, or hub client is used at runtime.
        self._image = ort.InferenceSession(str(vision_path), sess_options=session_config, providers=["CPUExecutionProvider"])
        self._text = ort.InferenceSession(str(text_path), sess_options=session_config, providers=["CPUExecutionProvider"])

    def encode_image(self, image: Image.Image) -> list[float]:
        """Bicubic resize of longest edge, centered black letterbox, CLIP normalization."""
        scale = IMAGE_SIZE / max(image.size)
        width, height = (max(1, round(length * scale)) for length in image.size)
        resized = image.convert("RGB").resize((width, height), Image.Resampling.BICUBIC)
        square = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE))
        square.paste(resized, ((IMAGE_SIZE - width) // 2, (IMAGE_SIZE - height) // 2))
        np = self._np
        pixels = np.asarray(square, dtype=np.float32) / np.float32(255)
        pixels = (pixels - np.asarray(CLIP_MEAN, dtype=np.float32)) / np.asarray(CLIP_STD, dtype=np.float32)
        chw = np.ascontiguousarray(pixels.transpose((2, 0, 1))[None, ...])
        output = self._image.run(["image_features"], {"pixel_values": chw})[0]
        return self._normalize(output)

    def encode_text(self, text: str) -> list[float]:
        """Apply the pinned clyp Japanese tokenizer and prepend the CLS token."""
        if not text.strip():
            raise ValueError("Visual query must not be empty")
        ids = [4, *self._tokenizer.encode(text, add_special_tokens=False).ids]
        np = self._np
        inputs = {
            "input_ids": np.asarray([ids], dtype=np.int64),
            "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
            "position_ids": np.arange(len(ids), dtype=np.int64)[None, :],
        }
        output = self._text.run(["text_features"], inputs)[0]
        return self._normalize(output)

    def _normalize(self, output) -> list[float]:
        np = self._np
        if output.shape != (1, self.dimension) or not np.isfinite(output).all():
            raise ValueError("Visual embedding was non-finite or had the wrong dimension")
        vector = output[0]
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm <= 0:
            raise ValueError("Visual embedding norm is invalid")
        return [float(value) for value in vector / norm]
