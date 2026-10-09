"""Bounded, explicit projections and type-preserving immutable identities."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from types import MappingProxyType, MemberDescriptorType
from typing import Any

from .errors import DefinitionError, InputError

Path = tuple[str, ...]
MISSING = object()
MAX_BYTES = 4 * 1024 * 1024


def path(value: Any, *, shorthand: bool = False) -> Path:
    if shorthand and isinstance(value, str):
        value = (value,)
    if not isinstance(value, tuple) or not value or any(not isinstance(k, str) or not k for k in value):
        raise DefinitionError("a path must be a nonempty tuple of nonempty strings")
    if len(value) > 16:
        raise DefinitionError("a path cannot exceed 16 levels")
    return value


def identity_key(value: Any, *, revision: bool = False) -> Any:
    if revision and value is None:
        return None
    if type(value) not in (str, int):
        raise InputError("record keys and revisions must be strings or integers, excluding bool")
    return value


def read(item: Any, keys: Path, *, optional: bool = False) -> Any:
    for key in keys:
        if isinstance(item, Mapping):
            if key not in item:
                if optional:
                    return MISSING
                raise InputError("a required projected field is missing")
            item = item[key]
        elif is_dataclass(item) and not isinstance(item, type):
            if key not in {f.name for f in fields(item)}:
                if optional:
                    return MISSING
                raise InputError("a required dataclass field is missing")
            descriptor = inspect.getattr_static(type(item), key, MISSING)
            if descriptor is not MISSING and hasattr(descriptor, "__get__"):
                if not isinstance(descriptor, MemberDescriptorType):
                    raise InputError("descriptor properties are not supported projections")
                item = descriptor.__get__(item, type(item))
            else:
                storage = object.__getattribute__(item, "__dict__")
                if key not in storage:
                    raise InputError("dataclass field has no stored value")
                item = storage[key]
        else:
            raise InputError("paths traverse mappings or stored dataclass fields only")
    return item


def freeze(
    value: Any, *, _active: set[int] | None = None, _count: list[int] | None = None, _bytes: list[int] | None = None, _depth: int = 0
) -> Any:
    active = set() if _active is None else _active
    count = [0] if _count is None else _count
    used = [0] if _bytes is None else _bytes

    def charge(amount):
        used[0] += amount
        if used[0] > MAX_BYTES:
            raise InputError("snapshot exceeds byte limit")

    count[0] += 1
    if count[0] > 10_000 or _depth > 16:
        raise InputError("snapshot exceeds member or nesting limit")
    if value is None or type(value) in (bool, int):
        try:
            charge(len(canonical(tagged(value))))
        except ValueError:
            raise InputError("integer exceeds serialization limits") from None
        return value
    if type(value) is str:
        if len(value) + used[0] > MAX_BYTES:
            raise InputError("snapshot exceeds byte limit")
        charge(len(canonical(tagged(value))))
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InputError("snapshot numbers must be finite")
        charge(len(canonical(tagged(value))))
        return value
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise InputError("timestamps must be timezone-aware")
        # User-defined tzinfo can be mutable; retain the supplied wall time and fixed offset.
        value = value.replace(tzinfo=timezone(offset))
        charge(len(canonical(tagged(value))))
        return value
    if not isinstance(value, (Mapping, list, tuple)):
        raise InputError("unsupported snapshot value; project explicit stored fields")
    if id(value) in active:
        raise InputError("snapshot contains a cycle")
    active.add(id(value))
    try:
        if isinstance(value, Mapping):
            charge(10 + max(0, len(value) - 1))
            if any(type(k) is not str for k in value):
                raise InputError("snapshot mapping keys must be strings")
            out = {}
            for key, item in value.items():
                if len(key) + used[0] > MAX_BYTES:
                    raise InputError("snapshot exceeds byte limit")
                charge(len(canonical(key)) + 3)
                out[key] = freeze(item, _active=active, _count=count, _bytes=used, _depth=_depth + 1)
            return MappingProxyType(out)
        charge(12 + max(0, len(value) - 1))
        return tuple(freeze(v, _active=active, _count=count, _bytes=used, _depth=_depth + 1) for v in value)
    finally:
        active.remove(id(value))


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def tagged(value: Any) -> Any:
    if isinstance(value, Mapping):
        return ["map", [[k, tagged(value[k])] for k in sorted(value)]]
    if isinstance(value, (tuple, list)):
        return ["array", [tagged(v) for v in value]]
    if isinstance(value, datetime):
        return ["datetime", utc(value)]
    return [type(value).__name__, value]


def canonical(value: Any) -> bytes:
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(tagged(value))).hexdigest()


def group_identity(value: Any) -> str:
    # IEEE signed zeros compare equal; preserve their sign everywhere except partition keys.
    return digest(0.0 if type(value) is float and value == 0 else value)


def project(item: Any, required: set[Path], optional: set[Path] | None = None) -> Mapping[str, Any]:
    if not isinstance(item, Mapping) and not (is_dataclass(item) and not isinstance(item, type)):
        raise InputError("input records must be mappings or dataclass instances")
    result: dict[str, Any] = {}
    # Parents first; a selected parent already preserves the selected child's full structure.
    selected: list[Path] = []
    for keys in sorted(required | (optional or set()), key=lambda p: (len(p), p)):
        value = read(item, keys, optional=keys not in required)
        if value is MISSING:
            continue
        if any(keys[: len(parent)] == parent for parent in selected):
            continue
        target = result
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
        selected.append(keys)
    frozen = freeze(result)
    if len(canonical(tagged(frozen))) > MAX_BYTES:
        raise InputError("snapshot exceeds byte limit")
    return frozen


def aliases(data: Mapping, projection: Mapping[str, Path]) -> Mapping[str, Any]:
    return MappingProxyType({name: read(data, p) for name, p in projection.items()})
