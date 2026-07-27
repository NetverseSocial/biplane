# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Biplane regression: telemetry is opt-in and fails closed.

Without an operator-configured OTLP_ENDPOINT there must be no exporter
construction, no Django instrumentation, and no span delivery — and new
instances must default to telemetry disabled.
"""

import pytest
from unittest.mock import patch

from plane.license.models import Instance
from plane.utils import telemetry
from plane.license.bgtasks.tracer import instance_traces


@pytest.mark.unit
class TestTelemetryOptIn:
    def test_instance_model_defaults_telemetry_off(self):
        field = Instance._meta.get_field("is_telemetry_enabled")
        assert field.default is False

    def test_is_telemetry_configured_false_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
        assert telemetry.is_telemetry_configured() is False

    def test_init_tracer_returns_none_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
        with (
            patch.object(telemetry, "OTLPSpanExporter") as exporter,
            patch.object(telemetry, "DjangoInstrumentor") as instrumentor,
        ):
            assert telemetry.init_tracer() is None
            exporter.assert_not_called()
            instrumentor.assert_not_called()

    def test_instance_traces_noop_without_endpoint(self, monkeypatch):
        # Must return before any tracer init, exporter build, or DB access.
        monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
        with (
            patch("plane.license.bgtasks.tracer.init_tracer") as init,
            patch("plane.license.bgtasks.tracer.Instance") as instance_model,
        ):
            instance_traces()
            init.assert_not_called()
            instance_model.objects.first.assert_not_called()

    def test_init_tracer_builds_exporter_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("OTLP_ENDPOINT", "https://collector.example.com")
        monkeypatch.setattr(telemetry, "_TRACER_PROVIDER", None)
        with (
            patch.object(telemetry, "OTLPSpanExporter") as exporter,
            patch.object(telemetry, "DjangoInstrumentor"),
            patch.object(telemetry, "trace"),
            patch.object(telemetry.atexit, "register"),
        ):
            provider = telemetry.init_tracer()
            assert provider is not None
            exporter.assert_called_once_with(endpoint="https://collector.example.com")
        monkeypatch.setattr(telemetry, "_TRACER_PROVIDER", None)
