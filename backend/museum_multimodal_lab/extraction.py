# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import io
import math
import wave
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .providers import AsrProvider, ModelError, ModelProvider

SUPPORTED = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}


class ExtractionError(ValueError):
    pass


def validate_upload(name: str, data: bytes, settings: Settings) -> tuple[str, str]:
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED or not name or name in {".", ".."}:
        raise ExtractionError("PDF・PNG・JPEG・WAV・MP3・M4Aのみ登録できます")
    if not data:
        raise ExtractionError("空のファイルは登録できません")
    if len(data) > settings.max_upload_bytes:
        raise ExtractionError(f"ファイルは {settings.max_upload_bytes // (1024 * 1024)} MB 以下にしてください")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ExtractionError("PDFの内容を確認できません")
        try:
            import pymupdf as fitz

            with fitz.open(stream=data, filetype="pdf") as document:
                if document.is_encrypted or not 1 <= document.page_count <= settings.max_pdf_pages:
                    raise ExtractionError(f"PDFは暗号化なし・{settings.max_pdf_pages}ページ以下にしてください")
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("PDFが破損しているか、読み込めません") from exc
    elif suffix in {".png", ".jpg", ".jpeg"}:
        try:
            with Image.open(io.BytesIO(data)) as image:
                expected_format = "PNG" if suffix == ".png" else "JPEG"
                if image.format != expected_format or image.width * image.height > settings.max_image_pixels:
                    raise ExtractionError("画像形式または画素数が上限を超えています")
                image.verify()
        except ExtractionError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ExtractionError("画像が破損しているか、読み込めません") from exc
    elif suffix == ".wav":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
            raise ExtractionError("WAVの内容を確認できません")
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, ZeroDivisionError, EOFError) as exc:
            raise ExtractionError("WAVが破損しているか、読み込めません") from exc
        if not 0 < duration <= settings.max_audio_seconds:
            raise ExtractionError(f"音声は {settings.max_audio_seconds} 秒以下にしてください")
    elif suffix == ".mp3":
        if not (data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)):
            raise ExtractionError("MP3の内容を確認できません")
    elif suffix == ".m4a" and data[4:8] != b"ftyp":
        raise ExtractionError("M4Aの内容を確認できません")
    return SUPPORTED[suffix], suffix


def _image_for_vlm(data: bytes, settings: Settings) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            if image.width * image.height > settings.max_image_pixels:
                raise ExtractionError("画像の画素数が上限を超えています")
            image.thumbnail((1280, 1280))
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ExtractionError("画像を解析用に変換できません") from exc


def _audio_duration(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    try:
        import av
    except ImportError as exc:
        raise ExtractionError("MP3/M4Aの確認には音声デコーダが必要です") from exc
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ExtractionError("音声トラックがありません")
            duration = container.duration
            if duration is None:
                stream = container.streams.audio[0]
                duration = float(stream.duration * stream.time_base * 1_000_000) if stream.duration else None
            if duration is None:
                raise ExtractionError("音声の長さを確認できません")
            return float(duration) / 1_000_000
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("音声を読み込めません") from exc


def extract_document(
    path: Path,
    mime_type: str,
    settings: Settings,
    models: ModelProvider,
    asr: AsrProvider,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return draft units, model label, and a human-readable extraction warning."""
    if mime_type.startswith("image/"):
        text = models.inspect_image(_image_for_vlm(path.read_bytes(), settings))
        return [{"kind": "image_interpretation", "extracted_text": text, "model_name": settings.vision_model}], settings.vision_model, None

    if mime_type == "application/pdf":
        import pymupdf as fitz

        units: list[dict[str, Any]] = []
        warnings: list[str] = []
        vlm_pages = 0
        vlm_success = 0
        with fitz.open(path) as document:
            if document.is_encrypted or document.page_count > settings.max_pdf_pages:
                raise ExtractionError("PDFのページ数または暗号化状態が上限外です")
            for page_index, page in enumerate(document):
                text = page.get_text("text", sort=True).strip()
                if text:
                    units.append({"kind": "pdf_text", "extracted_text": text, "page_number": page_index + 1, "model_name": "PyMuPDF"})
                # Even one vector path may carry a diagram/axis that plain text omits.
                has_visuals = bool(page.get_images(full=True)) or bool(page.get_drawings()) or not text
                if has_visuals and vlm_pages < settings.max_vlm_pdf_pages:
                    vlm_pages += 1
                    width, height = page.rect.width, page.rect.height
                    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                        raise ExtractionError("PDFページの寸法を確認できません")
                    # Bound the raster *before* allocation; resizing the PNG afterwards is too late.
                    scale = min(1.5, 1280 / width, 1280 / height)
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    try:
                        interpreted = models.inspect_image(_image_for_vlm(pixmap.tobytes("png"), settings))
                    except ModelError as exc:
                        if not text:
                            raise ExtractionError("画像のみのPDFページを解析できません。画像対応モデルを確認してください") from exc
                        warnings.append(f"{page_index + 1}ページの図表解析を省略しました")
                    else:
                        units.append({"kind": "image_interpretation", "extracted_text": interpreted, "page_number": page_index + 1, "model_name": settings.vision_model})
                        vlm_success += 1
                elif has_visuals:
                    warnings.append(f"{page_index + 1}ページの図表解析は上限のため省略しました")
        if not units:
            raise ExtractionError("PDFから文字・図表を抽出できませんでした")
        return units, "PyMuPDF + " + settings.vision_model if vlm_success else "PyMuPDF", " / ".join(warnings) or None

    if mime_type.startswith("audio/"):
        duration = _audio_duration(path)
        if not 0 < duration <= settings.max_audio_seconds:
            raise ExtractionError(f"音声は {settings.max_audio_seconds} 秒以下にしてください")
        segments = asr.transcribe(path)
        units = []
        for segment in segments:
            start, end = float(segment["start_sec"]), float(segment["end_sec"])
            if start < 0 or end <= start or end > duration + 1.0:
                raise ExtractionError("音声認識の時刻が元音声の範囲外です")
            units.append({
                "kind": "audio_transcript",
                "extracted_text": str(segment["text"]).strip(),
                "start_sec": start,
                "end_sec": end,
                "model_name": f"faster-whisper:{settings.asr_model}",
            })
        return units, f"faster-whisper:{settings.asr_model}", None
    raise ExtractionError("対応していない資料形式です")
