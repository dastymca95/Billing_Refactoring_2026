"""Fail-closed network boundary for private, local-only experiments.

Normal application behavior is unchanged unless ``INNER_VIEW_LOCAL_INFERENCE_ONLY``
is explicitly enabled.  In that mode only loopback HTTP endpoints are accepted;
configured remote providers and DNS hostnames are rejected before request
construction or socket dispatch.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse


LOCAL_ONLY_ENV = "INNER_VIEW_LOCAL_INFERENCE_ONLY"
LOCAL_PROVIDER_NAMES = frozenset({"local", "local_ollama", "ollama"})
_NETWORK_GUARD_LOCK = threading.RLock()


class LocalInferenceNetworkBlocked(RuntimeError):
    """Raised before transport when local-only execution rejects an endpoint."""


@dataclass(frozen=True)
class LocalEndpoint:
    provider: str
    base_url: str
    hostname: str
    port: int | None


def local_inference_only(environment: dict[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return str(env.get(LOCAL_ONLY_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def validate_loopback_endpoint(*, provider: str, base_url: str) -> LocalEndpoint:
    normalized_provider = str(provider or "").strip().lower()
    raw_url = str(base_url or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocalInferenceNetworkBlocked("local_endpoint_invalid")
    hostname = parsed.hostname.strip().lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        # Hostnames, including ``localhost``, would require name resolution.
        # The experiment contract permits no DNS path, so require a literal
        # loopback address such as 127.0.0.1 or ::1.
        loopback = False
    if not loopback:
        raise LocalInferenceNetworkBlocked("remote_endpoint_blocked")
    if normalized_provider not in LOCAL_PROVIDER_NAMES:
        raise LocalInferenceNetworkBlocked("remote_provider_blocked")
    return LocalEndpoint(
        provider=normalized_provider,
        base_url=raw_url,
        hostname=hostname,
        port=parsed.port,
    )


def assert_dispatch_allowed(*, provider: str, url: str, stage: str = "") -> None:
    """Reject non-loopback dispatches before secrets or private payloads are used."""

    if not local_inference_only():
        return
    try:
        validate_loopback_endpoint(provider=provider, base_url=url)
    except LocalInferenceNetworkBlocked as exc:
        # Import lazily so this guard remains usable before application settings.
        try:
            from . import ai_runtime_trace

            ai_runtime_trace.record_blocked_network_attempt(
                provider=str(provider or "unknown"),
                stage=str(stage or "unknown"),
                failure_code=str(exc),
            )
        except Exception:
            # The security boundary must not depend on telemetry availability.
            pass
        raise


def _literal_loopback_host(value: object) -> bool:
    host = str(value or "").strip().strip("[]")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@contextmanager
def local_network_isolation() -> Iterator[None]:
    """Process-wide socket gate used only by the isolated experiment runner.

    It blocks DNS hostnames and non-loopback IPs even when a dependency bypasses
    the normal provider adapter. The runner is a dedicated process, so this
    temporary global patch cannot affect the interactive application server.
    """

    if not local_inference_only():
        raise LocalInferenceNetworkBlocked("local_only_mode_required")
    with _NETWORK_GUARD_LOCK:
        original_create_connection = socket.create_connection
        original_connect = socket.socket.connect

        def guarded_create_connection(address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) and address else ""
            if not _literal_loopback_host(host):
                raise LocalInferenceNetworkBlocked("socket_remote_connect_blocked")
            return original_create_connection(address, *args, **kwargs)

        def guarded_connect(instance, address):
            host = address[0] if isinstance(address, tuple) and address else ""
            if not _literal_loopback_host(host):
                raise LocalInferenceNetworkBlocked("socket_remote_connect_blocked")
            return original_connect(instance, address)

        socket.create_connection = guarded_create_connection
        socket.socket.connect = guarded_connect
        try:
            yield
        finally:
            socket.create_connection = original_create_connection
            socket.socket.connect = original_connect


__all__ = [
    "LOCAL_ONLY_ENV",
    "LOCAL_PROVIDER_NAMES",
    "LocalEndpoint",
    "LocalInferenceNetworkBlocked",
    "assert_dispatch_allowed",
    "local_inference_only",
    "local_network_isolation",
    "validate_loopback_endpoint",
]
