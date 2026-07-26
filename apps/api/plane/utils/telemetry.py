# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import os
import atexit

# Third party imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.django import DjangoInstrumentor

# Global variable to track initialization
_TRACER_PROVIDER = None


def is_telemetry_configured():
    """Telemetry is opt-in: it runs only when the operator sets OTLP_ENDPOINT."""
    return bool(os.environ.get("OTLP_ENDPOINT", ""))


def init_tracer():
    """Initialize OpenTelemetry with proper shutdown handling"""
    global _TRACER_PROVIDER

    # Fail closed: no exporter, no instrumentation, unless an endpoint was
    # explicitly configured by the operator (Biplane ships none by default).
    if not is_telemetry_configured():
        return None

    # If already initialized, return existing provider
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER

    # Configure the tracer provider
    service_name = os.environ.get("SERVICE_NAME", "plane-ce-api")
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)

    # Set as global tracer provider
    trace.set_tracer_provider(tracer_provider)

    # Configure the OTLP exporter
    otel_endpoint = os.environ.get("OTLP_ENDPOINT")
    otlp_exporter = OTLPSpanExporter(endpoint=otel_endpoint)
    span_processor = BatchSpanProcessor(otlp_exporter)
    tracer_provider.add_span_processor(span_processor)

    # Initialize Django instrumentation
    DjangoInstrumentor().instrument()

    # Store provider globally
    _TRACER_PROVIDER = tracer_provider

    # Register shutdown handler
    atexit.register(shutdown_tracer)

    return tracer_provider


def shutdown_tracer():
    """Shutdown OpenTelemetry tracers and processors"""
    global _TRACER_PROVIDER

    if _TRACER_PROVIDER is not None:
        if hasattr(_TRACER_PROVIDER, "shutdown"):
            _TRACER_PROVIDER.shutdown()
        _TRACER_PROVIDER = None
