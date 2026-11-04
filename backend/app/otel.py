"""OpenTelemetry setup. The exporter stays off unless configured."""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from app.config import Settings


def setup_telemetry(app: FastAPI, settings: Settings) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
    if settings.otel_exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp" and settings.otel_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
        )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
