"""v0.6.2 — image attachment handler.

Pasted screenshots arrive from the VSCode webview as base64-encoded PNG/JPEG
data. This module:
  1. Decodes the base64 payload.
  2. Downsamples with Pillow to a MAX dimension of 1024 (preserves aspect ratio).
     Locked safeguard from the Gemini debate (v0.6.1 symmetric peer review
     already 4× the per-turn token burn; raw screenshots would push us over
     the 429 cliff).
  3. Saves the result under <project_dir>/_collab/images/<turn_id>-<idx>.png.
  4. Returns the saved paths for inclusion in CothinkState.attached_images.

Read by Discovery + Planning, which wire the paths into Claude's multimodal
prompt (Anthropic image content blocks) and Gemini's `Part.from_bytes`.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image


_IMAGES_REL = "_collab/images"
MAX_DIMENSION = 1024  # px — Gemini debate safeguard
SUPPORTED_FORMATS = ("PNG", "JPEG", "WEBP")


def _images_dir(project_dir: str) -> Path:
    return Path(project_dir) / _IMAGES_REL


def decode_and_save(
    project_dir: str,
    turn_id: str,
    images: list[dict[str, str]],
) -> list[str]:
    """Decode base64 image payloads, downsample, save to disk.

    Args:
        project_dir: absolute path to the project root.
        turn_id: stable id for this turn (server-generated, e.g. "t<uuid12>").
        images: list of {"filename": str, "data_base64": str} dicts.
                `data_base64` may have or omit the `data:image/...;base64,` prefix.

    Returns:
        List of saved file paths (absolute). Empty if `images` is empty.
        Skips entries that fail to decode rather than aborting the turn.
    """
    if not images:
        return []
    out_dir = _images_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for idx, item in enumerate(images):
        raw_b64 = item.get("data_base64") or ""
        # Strip the data-URL prefix if the webview included it.
        if "," in raw_b64 and raw_b64.lstrip().startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(raw_b64, validate=False)
            img = Image.open(io.BytesIO(raw_bytes))
            img.load()
        except Exception:
            # Bad payload — skip this image, log via the caller (server).
            continue

        # Convert palette / 'P' / 'RGBA-with-mask' to RGB if needed for JPEG safety,
        # but we always save as PNG below, so just normalize RGBA→RGB if no alpha.
        if img.mode == "P":
            img = img.convert("RGBA")

        w, h = img.size
        if max(w, h) > MAX_DIMENSION:
            scale = MAX_DIMENSION / float(max(w, h))
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        # Save as PNG for losslessness + universal SDK support.
        out_path = out_dir / f"{turn_id}-{idx}.png"
        img.save(out_path, format="PNG", optimize=True)
        saved.append(str(out_path))

    return saved


def load_image_bytes(path: str) -> tuple[bytes, str]:
    """Read an image file from disk; return (bytes, mime_type).

    Used by Discovery / Planning to feed the LLM SDKs.
    """
    p = Path(path)
    data = p.read_bytes()
    suffix = p.suffix.lower().lstrip(".")
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(suffix, "image/png")
    return data, mime
