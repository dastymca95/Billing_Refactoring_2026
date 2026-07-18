"""Serialize Windows-native PDF/OCR work while remote AI calls stay parallel."""

from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable, TypeVar, cast


LOCAL_DOCUMENT_PREPROCESS_LOCK = threading.RLock()
F = TypeVar("F", bound=Callable[..., Any])


def serialized_local_document_operation(function: F) -> F:
    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        with LOCAL_DOCUMENT_PREPROCESS_LOCK:
            return function(*args, **kwargs)

    return cast(F, guarded)


__all__ = ["LOCAL_DOCUMENT_PREPROCESS_LOCK", "serialized_local_document_operation"]
