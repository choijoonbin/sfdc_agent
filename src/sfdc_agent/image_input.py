"""One immutable, source-bound PNG input, independent of model/provider transport.

Image bytes are never prompt-template arguments or trace values. A verified
binding describes the source, not the resolution eventually chosen by a model.
"""

import base64
from dataclasses import dataclass, field
import hashlib
import math
import struct
from typing import Literal
import zlib

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
_OPERATIONS = frozenset({"screen.generate", "screen.repair"})
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schemaVersion: StrictInt = Field(ge=1, le=1)
    mediaType: Literal["image/png"]
    fileKey: str = Field(pattern=r"^[A-Za-z0-9]{1,160}$")
    nodeId: str = Field(pattern=r"^\d+:\d+$", max_length=40)
    sourceVersion: str = Field(pattern=r"^[A-Za-z0-9_.:/+\-]{1,240}$")
    analysisSourceDigest: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: StrictInt = Field(gt=0, le=MAX_IMAGE_BYTES)
    width: StrictInt = Field(gt=0, le=MAX_IMAGE_DIMENSION)
    height: StrictInt = Field(gt=0, le=MAX_IMAGE_DIMENSION)
    scale: StrictInt = Field(ge=1, le=1)
    revisionVerified: StrictBool
    scaleVerified: StrictBool


@dataclass(frozen=True)
class ImageInput:
    """Created only after PNG validation and source/evidence/attempt binding."""

    content: bytes = field(repr=False)
    binding: ImageBinding

    def receipt(self):
        return self.binding.model_dump()


def _rows(width, height, bits_per_pixel, interlace):
    passes = ((0, 0, 1, 1),) if not interlace else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    for x, y, dx, dy in passes:
        columns, lines = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if columns and lines:
            size = (columns * bits_per_pixel + 7) // 8 + 1
            yield from (size for _ in range(lines))


def inspect_png(content: bytes) -> tuple[int, int]:
    """Validate framing, CRCs and bounded decoded PNG rows without retaining pixels."""
    if not isinstance(content, bytes) or not 45 <= len(content) <= MAX_IMAGE_BYTES or content[:8] != _SIGNATURE:
        raise ValueError("Invalid reference PNG.")
    cursor, dimensions, compressed, palette, image_ended, data_closed = 8, None, [], False, False, False
    while cursor < len(content):
        if cursor + 12 > len(content):
            raise ValueError("Invalid reference PNG chunks.")
        size, kind = struct.unpack_from(">I4s", content, cursor)
        end = cursor + size + 12
        if end > len(content) or zlib.crc32(content[cursor + 4:end - 4]) != struct.unpack_from(">I", content, end - 4)[0]:
            raise ValueError("Invalid reference PNG checksum.")
        data = content[cursor + 8:end - 4]
        if dimensions is None and kind != b"IHDR":
            raise ValueError("Missing reference PNG header.")
        if kind == b"IHDR":
            if dimensions is not None or size != 13:
                raise ValueError("Invalid reference PNG header.")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Reference PNG dimensions exceed the supported size.")
            if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError("Unsupported reference PNG encoding.")
            dimensions = width, height, depth, color, interlace
        elif kind == b"PLTE":
            if palette or compressed or not size or size % 3 or size > 768:
                raise ValueError("Invalid reference PNG palette.")
            palette = True
        elif kind == b"IDAT":
            if data_closed:
                raise ValueError("Noncontiguous reference PNG data.")
            compressed.append(data)
        elif kind == b"IEND":
            if size or not compressed or end != len(content):
                raise ValueError("Invalid reference PNG ending.")
            image_ended = True
        elif kind == b"acTL" or not kind[0] & 32:
            raise ValueError("Unsupported reference PNG chunk.")
        if compressed and kind not in (b"IDAT", b"IEND"):
            data_closed = True
        cursor = end
    if not image_ended:
        raise ValueError("Truncated reference PNG.")
    width, height, depth, color, interlace = dimensions
    if color == 3 and not palette:
        raise ValueError("Missing reference PNG palette.")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    rows = iter(_rows(width, height, channels * depth, interlace))
    remaining, at_filter, total = next(rows), True, 0
    expected = sum(_rows(width, height, channels * depth, interlace))
    decoder = zlib.decompressobj()
    try:
        for chunk in compressed:
            pending = chunk
            while pending:
                decoded = decoder.decompress(pending, 64 * 1024)
                pending = decoder.unconsumed_tail
                total += len(decoded)
                if total > expected or decoder.unused_data:
                    raise ValueError("Reference PNG decoded data exceeds its dimensions.")
                offset = 0
                while offset < len(decoded):
                    if at_filter and decoded[offset] > 4:
                        raise ValueError("Invalid reference PNG row filter.")
                    take = min(remaining, len(decoded) - offset)
                    offset, remaining, at_filter = offset + take, remaining - take, False
                    if not remaining:
                        remaining, at_filter = next(rows, 0), True
        if not decoder.eof or total != expected or remaining:
            raise ValueError("Truncated reference PNG pixel data.")
    except zlib.error:
        raise ValueError("Invalid reference PNG pixel data.") from None
    return width, height


def _dimension_pair(dimensions, binding):
    if not isinstance(dimensions, dict):
        raise ValueError("Missing source image dimensions.")
    for key in ("width", "height"):
        value = dimensions.get(key)
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not 0 < value <= MAX_IMAGE_DIMENSION or not math.isfinite(value)):
            raise ValueError("Invalid source image dimensions.")
        if math.ceil(value) != getattr(binding, key):
            raise ValueError("The reference PNG dimensions differ from the source.")


def validate_image_input(png, binding, *, operation, fields, context=None) -> ImageInput | None:
    """Fail before a model call on absent, altered, or cross-source image evidence."""
    if png is None and binding is None:
        return None
    if png is None or binding is None or operation not in _OPERATIONS or not isinstance(fields, dict):
        raise ValueError("Only source-bound Screen generation and repair accept PNG input.")
    binding = binding if isinstance(binding, ImageBinding) else ImageBinding.model_validate(binding)
    if not isinstance(png, str) or len(png) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("The reference PNG exceeds the supported size.")
    content = base64.b64decode(png, validate=True)
    if base64.b64encode(content).decode() != png or len(content) != binding.bytes or hashlib.sha256(content).hexdigest() != binding.sha256:
        raise ValueError("The reference PNG digest does not match.")
    if inspect_png(content) != (binding.width, binding.height):
        raise ValueError("The reference PNG dimensions do not match.")
    evidence = fields.get("evidence") if operation == "screen.repair" else fields
    if not isinstance(evidence, dict):
        raise ValueError("Missing original image evidence.")
    reviewed = evidence.get("reviewedAnalysis")
    source = reviewed.get("source") if isinstance(reviewed, dict) else None
    if not isinstance(source, dict) or reviewed.get("sourceDigest") != binding.analysisSourceDigest:
        raise ValueError("The reference image does not match the saved analysis.")
    for key in ("fileKey", "nodeId"):
        if source.get(key) != getattr(binding, key) or evidence.get(key) != getattr(binding, key):
            raise ValueError("The reference image belongs to a different source.")
    version = source.get("version")
    snapshot = source.get("snapshotId")
    if not version and isinstance(snapshot, str) and snapshot:
        version = "mcp-snapshot:" + snapshot
    if version != binding.sourceVersion:
        raise ValueError("The reference image source version differs from the saved analysis.")
    if binding.revisionVerified and (source.get("revisionVerified") is not True or version.startswith(("local-", "mcp-snapshot:"))):
        raise ValueError("The source does not prove the reference image's remote revision.")
    _dimension_pair(evidence.get("layoutDimensions"), binding)
    _dimension_pair(reviewed.get("dimensions"), binding)
    if context is not None:
        if getattr(context, "purpose", None) != "screen":
            raise ValueError("The reference image belongs to a different execution purpose.")
        for name, expected in (("file_key", binding.fileKey), ("node_id", binding.nodeId)):
            actual = getattr(context, name, None)
            if actual is not None and actual != expected:
                raise ValueError("The reference image belongs to a different managed execution.")
    return ImageInput(content, binding)
