from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def dstr(d: Decimal) -> str:
    """Plain decimal string without storage padding or exponent: Decimal('60.4800000000') -> '60.48'."""
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    return format(d.normalize(), "f")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return dstr(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not serializable: {type(o)}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def jsonable(obj: Any) -> Any:
    return json.loads(canonical_json(obj))


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()
