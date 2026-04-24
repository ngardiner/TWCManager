# OpenTelemetry Status Output
# Exports traces, metrics, and logs via OpenTelemetry

import logging
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("OTELStatus", "Status")


class OTELStatus:
    """
    OpenTelemetry Status module for observability.
    
    Exports traces, metrics, and structured logs to OpenTelemetry collectors.
    Enables live debugging and performance monitoring across all modules.
    """

    def __init__(self, master):
        self.__config = master.config
        self.__master = master

        try:
            self.__config_otel = self.__config["status"]["OTEL"]
        except KeyError:
            self.__config_otel = {}

        self.enabled = self.__config_otel.get("enabled", False)
        self.exporter_type = self.__config_otel.get("exporter", "otlp").lower()
        self.otlp_endpoint = self.__config_otel.get("otlpEndpoint", "http://localhost:4317")
        self.jaeger_endpoint = self.__config_otel.get("jaegerEndpoint", "http://localhost:14268/api/traces")
        self.service_name = self.__config_otel.get("serviceName", "twcmanager")
        self.service_version = self.__config_otel.get("serviceVersion", "1.4.0")

        # Unload if disabled or misconfigured
        if not self.enabled:
            self.__master.releaseModule("lib.TWCManager.Status", "OTELStatus")
            return

        self._init_otel()

    def _init_otel(self):
        """Initialize OpenTelemetry SDK and exporters."""
        try:
            from opentelemetry import trace, metrics, logs
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.logs import LoggerProvider
            from opentelemetry.sdk.logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.grpc.log_exporter import OTLPLogExporter
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        except ImportError as e:
            logger.error(
                "OTELStatus: OpenTelemetry packages not installed - OTEL support unavailable (%s)",
                str(e),
            )
            self.__master.releaseModule("lib.TWCManager.Status", "OTELStatus")
            return

        # Create resource
        resource = Resource.create({
            "service.name": self.service_name,
            "service.version": self.service_version,
        })

        # Initialize tracer
        if self.exporter_type == "jaeger":
            jaeger_exporter = JaegerExporter(
                agent_host_name="localhost",
                agent_port=6831,
            )
            trace_provider = TracerProvider(resource=resource)
            trace_provider.add_span_processor(SimpleSpanProcessor(jaeger_exporter))
        else:  # otlp (default)
            otlp_exporter = OTLPSpanExporter(endpoint=self.otlp_endpoint)
            trace_provider = TracerProvider(resource=resource)
            trace_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

        trace.set_tracer_provider(trace_provider)
        self.tracer = trace.get_tracer(__name__)

        # Initialize metrics
        try:
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=self.otlp_endpoint)
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
            self.meter = metrics.get_meter(__name__)
        except Exception as e:
            logger.warning("OTELStatus: Failed to initialize metrics: %s", str(e))
            self.meter = None

        # Initialize logs
        try:
            log_exporter = OTLPLogExporter(endpoint=self.otlp_endpoint)
            log_provider = LoggerProvider(resource=resource)
            log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
            logs.set_logger_provider(log_provider)
        except Exception as e:
            logger.warning("OTELStatus: Failed to initialize logs: %s", str(e))

        logger.info(
            "OTELStatus initialized: exporter=%s, endpoint=%s",
            self.exporter_type,
            self.otlp_endpoint if self.exporter_type == "otlp" else self.jaeger_endpoint,
        )

    def setStatus(self, twcid, key_underscore, key_camelcase, value, unit):
        """
        Publish status as OTEL metric.
        Converts TWC/vehicle state into metrics for observability.
        """
        if not self.enabled or not self.meter:
            return

        try:
            # Create metric name from TWC ID and key
            metric_name = f"twc.{twcid}.{key_underscore}".lower()

            # Record as gauge (current value)
            if isinstance(value, (int, float)):
                gauge = self.meter.create_gauge(
                    metric_name,
                    description=f"{key_camelcase} for {twcid}",
                    unit=unit or "",
                )
                gauge.record(value)
        except Exception as e:
            logger.debug("OTELStatus: Failed to record metric %s: %s", key_underscore, str(e))

    def record_span(self, name, attributes=None, status=None):
        """
        Record a span for distributed tracing.
        Used by modules to trace operations.
        """
        if not self.enabled or not self.tracer:
            return None

        try:
            span = self.tracer.start_span(name)
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            if status:
                span.set_attribute("status", status)
            return span
        except Exception as e:
            logger.debug("OTELStatus: Failed to create span %s: %s", name, str(e))
            return None

    def record_metric(self, name, value, attributes=None):
        """
        Record a metric value.
        Used by modules to track counters, gauges, histograms.
        """
        if not self.enabled or not self.meter:
            return

        try:
            # Simple gauge recording
            gauge = self.meter.create_gauge(name, description=f"Metric: {name}")
            gauge.record(value, attributes or {})
        except Exception as e:
            logger.debug("OTELStatus: Failed to record metric %s: %s", name, str(e))
