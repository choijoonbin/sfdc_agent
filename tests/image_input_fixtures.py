"""Small, synthetic source/image fixtures; no credentials or project data."""

import base64
import hashlib
import struct
import zlib

from sfdc_agent.image_input import validate_image_input


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width=2, height=2, *, pixels=None):
    rows = pixels if pixels is not None else (b"\0" + b"\xff\xff\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def image_payload(*, source_version="123456789", revision=True, scale_verified=True, content=None):
    content = png() if content is None else content
    source = {"fileKey": "FixtureFile", "nodeId": "12:34", "version": source_version,
              "revisionVerified": revision}
    fields = {"fileKey": source["fileKey"], "nodeId": source["nodeId"],
              "name": "nextBssImageFixture", "version": "1.9", "tokenDeclarations": "",
              "typographyGroups": [], "componentVariants": "", "layoutDimensions": {"width": 2, "height": 2},
              "sourceContext": {"fixture": True}, "reviewedAnalysis": {
                  "source": source, "sourceDigest": "a" * 64, "dimensions": {"width": 2, "height": 2}}}
    binding = {"schemaVersion": 1, "mediaType": "image/png", "fileKey": source["fileKey"],
               "nodeId": source["nodeId"], "sourceVersion": source_version, "analysisSourceDigest": "a" * 64,
               "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "width": 2, "height": 2,
               "scale": 1, "revisionVerified": revision, "scaleVerified": scale_verified}
    return {"operation": "screen.generate", "fields": fields,
            "png": base64.b64encode(content).decode(), "imageBinding": binding}


def validated_image(**options):
    value = image_payload(**options)
    return validate_image_input(value["png"], value["imageBinding"], operation=value["operation"], fields=value["fields"])


def event(kind, **values):
    return {"type": "sessionUpdate", "data": {"sessionId": "fixture-session",
             "update": {"sessionUpdate": kind, **values}}}


def terminal(**values):
    return {"type": "runFinished", "data": {"sessionId": "fixture-session", "status": "success",
             "stopReason": "end_turn", "finalText": "{}", "finalTextTruncated": False, **values}}


def stream(*events):
    return b"\n".join(__import__("json").dumps(value).encode() for value in events) + b"\n"
