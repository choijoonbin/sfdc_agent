"""Shared JSON syntax checks; callers retain their shape, size and error policies."""

from collections.abc import Callable
import json
from typing import NoReturn, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list['JsonValue'] | dict[str, 'JsonValue']
ErrorFactory: TypeAlias = Callable[[], ValueError]


class StrictJSONDecoder(json.JSONDecoder):
    """Reject duplicate keys and named non-finite constants, including in raw_decode."""

    def __init__(self, *, error_factory: ErrorFactory | None = None) -> None:
        def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {}
            for key, value in pairs:
                if key in result:
                    raise error_factory() if error_factory else ValueError('Repeated JSON key.')
                result[key] = value
            return result

        def reject_constant(value: str) -> NoReturn:
            raise error_factory() if error_factory else ValueError('Non-finite JSON number.')

        super().__init__(object_pairs_hook=unique, parse_constant=reject_constant)


def loads(value: str | bytes | bytearray, *, error_factory: ErrorFactory | None = None) -> JsonValue:
    """Keep json.loads byte-encoding detection and syntax errors unchanged."""
    return json.loads(value, cls=StrictJSONDecoder, error_factory=error_factory)


def decode_json(body: bytes | bytearray) -> JsonValue:
    """Decode a managed message as strict UTF-8, without byte-encoding detection."""
    return loads(body.decode())


def encode_json(value: JsonValue) -> bytes:
    """Encode the existing compact UTF-8 managed wire format; never canonicalize sources."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
