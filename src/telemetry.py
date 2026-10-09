"""Optional Logfire telemetry for the pipeline scripts: spans, log events and system metrics.

Everything here is a no-op when the logfire package is not installed, and nothing is sent when
the project has no Logfire credentials (.logfire/ or LOGFIRE_TOKEN), so the scripts run the same
either way. To send data: `logfire auth`, then `logfire projects use <project>` in the repo root.
"""

import contextlib

try:
    import logfire
except ModuleNotFoundError:
    logfire = None


class _NoSpan:
    def set_attribute(self, key, value):
        pass


def configure(service_name, system_metrics=False):
    """Call once per process, before any span. Worker processes call it again for themselves."""
    if logfire is None:
        return
    # console=False: the scripts print their own progress.
    logfire.configure(service_name=service_name, send_to_logfire="if-token-present", console=False)
    if system_metrics:
        logfire.instrument_system_metrics()


def span(message, **attributes):
    """Context manager timing a block; `message` may reference attributes as {name}."""
    if logfire is None:
        return contextlib.nullcontext(_NoSpan())
    return logfire.span(message, **attributes)


def info(message, **attributes):
    if logfire is not None:
        logfire.info(message, **attributes)


def warn(message, **attributes):
    if logfire is not None:
        logfire.warn(message, **attributes)


def get_context():
    """The current trace context, to hand to another process."""
    return logfire.get_context() if logfire is not None else None


def attach_context(context):
    """Context manager making spans opened inside it children of another process's span."""
    if logfire is None or context is None:
        return contextlib.nullcontext()
    return logfire.attach_context(context)


def flush():
    """Send buffered spans now. Pool workers are terminated without running exit handlers."""
    if logfire is not None:
        logfire.force_flush(timeout_millis=30000)   # the first connection alone can take ~3 s
