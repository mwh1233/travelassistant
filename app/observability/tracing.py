"""Lightweight tracing helpers for planner nodes and tools."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from app.utils.logger import app_logger


@contextmanager
def trace_operation(name: str, **metadata: object) -> Iterator[None]:
    """Log a compact duration trace around an operation."""

    started = time.perf_counter()
    app_logger.info(f"trace_start name={name} metadata={metadata}")
    try:
        yield
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        app_logger.exception(f"trace_error name={name} elapsed_ms={elapsed_ms} error={exc}")
        raise
    else:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        app_logger.info(f"trace_end name={name} elapsed_ms={elapsed_ms}")

