"""Intex PureSpa LAN control — protocol, async client, supervisor."""

from .client import IntexSpaClient, SpaUnreachable
from .protocol import (
    COMMANDS,
    TEMP_MAX_C,
    TEMP_MIN_C,
    TOGGLE_FIELDS,
    SpaProtocolError,
    decode_status,
)

__all__ = [
    "IntexSpaClient",
    "SpaUnreachable",
    "COMMANDS",
    "TOGGLE_FIELDS",
    "TEMP_MIN_C",
    "TEMP_MAX_C",
    "SpaProtocolError",
    "decode_status",
]
