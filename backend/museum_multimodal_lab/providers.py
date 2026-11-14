# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import base64
import importlib.util
import json
import math
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import Settings


class ModelError(RuntimeError):
    """A local model is absent, unavailable, or returned unusable output."""


class ModelProvider(Protocol):
    def models(self) -> list[str]: ...
    def supports_vision(self) -> bool: ...
    def embed(self, text: str) -> list[float]: ...
    def inspect_image(self, image: bytes) -> str: ...
    def answer(self, question: str, evidence: list[dict[str, str]]) -> dict[str, Any]: ...


class AsrProvider(Protocol):
    def transcribe(self, path: Path) -> list[dict[str, Any]]: ...
    def available(self) -> bool: ...


class OllamaProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.ollama_url,
            timeout=httpx.Timeout(180.0, connect=3.0),
            trust_env=False,
            follow_redirects=False,
            headers={"Content-Type": "application/json"},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.client.request(method, path, **kwargs)
            if response.is_redirect:
                raise ModelError("ローカルAIが予期しない転送を返しました")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ModelError("ローカルAIの応答形式が不正です")
            return body
        except (httpx.HTTPError, ValueError) as exc:
            # Never copy the server response or user content into a UI/log error.
            raise ModelError("ローカルAIへ接続できないか、応答を読めません") from exc

    def models(self) -> list[str]:
        body = self._request("GET", "/api/tags")
        items = body.get("models")
        if not isinstance(items, list):
            raise ModelError("ローカルAIのモデル一覧を確認できません")
        return [item["name"] for item in items if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]]

    def supports_vision(self) -> bool:
        body = self._request("POST", "/api/show", json={"model": self.settings.vision_model})
        capabilities = body.get("capabilities")
        if not isinstance(capabilities, list):
            raise ModelError("画像モデルの対応形式を確認できません")
        return "vision" in capabilities

    def _require_model(self, name: str) -> None:
        installed = self.models()
        if name not in installed and not (name.endswith(":latest") and name[:-7] in installed):
            raise ModelError(f"必要なローカルモデルがありません: {name}")

    def embed(self, text: str) -> list[float]:
        self._require_model(self.settings.embedding_model)
        body = self._request("POST", "/api/embed", json={"model": self.settings.embedding_model, "input": text, "keep_alive": 0})
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings or not isinstance(embeddings[0], list):
            raise ModelError("埋め込みを取得できませんでした")
        if any(not isinstance(number, (int, float)) or isinstance(number, bool) for number in embeddings[0]):
            raise ModelError("埋め込みの値が不正です")
        vector = [float(number) for number in embeddings[0]]
        if not vector or any(not math.isfinite(number) for number in vector):
            raise ModelError("埋め込みの値が不正です")
        return vector

    def inspect_image(self, image: bytes) -> str:
        self._require_model(self.settings.vision_model)
        if not self.supports_vision():
            raise ModelError("設定したモデルは画像入力に対応していません")
        prompt = (
            "この資料画像に明示された内容だけを日本語で抽出してください。図のラベル・矢印・軸・凡例、"
            "表の行見出し・列見出し・数値・単位・対応関係を保ってください。"
            "読み取れない値は推測せず『判読できない』と書いてください。"
            "画像からの解釈であり原文引用ではありません。前置きや感想は不要です。"
        )
        body = self._request(
            "POST",
            "/api/generate",
            json={
                "model": self.settings.vision_model,
                "prompt": prompt,
                "images": [base64.b64encode(image).decode("ascii")],
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": 480},
            },
        )
        raw_response = body.get("response")
        result = raw_response.strip() if isinstance(raw_response, str) else ""
        if not result:
            raise ModelError("画像から文字や関係を抽出できませんでした")
        return result

    def answer(self, question: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
        self._require_model(self.settings.answer_model)
        snippets = "\n".join(f"[{item['id']}] {item['name']} ({item['kind']}): {item['text']}" for item in evidence)
        prompt = (
            "あなたは科学館職員が確認する説明案を作成します。以下の確認済み資料だけを根拠に、"
            "質問へ日本語で簡潔に答えてください。資料にない数値・理由・固有名詞は補わないでください。"
            "足りない場合は answer を空文字、source_ids を空配列にします。"
            "回答する場合は実際に使った資料IDだけを source_ids に指定します。"
            "JSONオブジェクト {\"answer\":\"...\",\"source_ids\":[\"S1\"]} だけ返してください。\n"
            f"質問: {question}\n確認済み資料:\n{snippets}"
        )
        body = self._request(
            "POST",
            "/api/generate",
            json={
                "model": self.settings.answer_model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": 300},
            },
        )
        raw_response = body.get("response")
        if not isinstance(raw_response, str):
            raise ModelError("回答の形式を確認できませんでした")
        try:
            parsed = json.loads(raw_response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ModelError("回答の形式を確認できませんでした") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("answer"), str) or not isinstance(parsed.get("source_ids"), list):
            raise ModelError("回答の形式を確認できませんでした")
        return parsed


class FasterWhisperProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def transcribe(self, path: Path) -> list[dict[str, Any]]:
        if not self.available():
            raise ModelError("音声認識が未導入です。READMEの手順で faster-whisper を準備してください")
        from faster_whisper import WhisperModel

        try:
            model = WhisperModel(
                self.settings.asr_model,
                device="cpu",
                compute_type="int8",
                local_files_only=True,
                num_workers=1,
            )
            segments, _ = model.transcribe(str(path), language="ja", beam_size=3, vad_filter=True)
            result = [
                {"start_sec": float(segment.start), "end_sec": float(segment.end), "text": segment.text.strip()}
                for segment in segments
                if segment.text.strip()
            ]
        except Exception as exc:
            raise ModelError("音声モデルを読み込めないか、音声を認識できませんでした。モデルの事前配置を確認してください") from exc
        if not result:
            raise ModelError("音声から発話を検出できませんでした")
        for segment in result:
            if segment["start_sec"] < 0 or segment["end_sec"] <= segment["start_sec"]:
                raise ModelError("音声の時刻情報が不正です")
        return result
