import json
import logging
import queue
import threading
from datetime import datetime
from typing import Any

import httpx

from jarvis_log_client.client import _get_auth_headers, _get_log_endpoint, _get_logs_url


class JarvisLogHandler(logging.Handler):
    """
    Python logging handler that sends logs to jarvis-logs server.

    Integrates with Python's standard logging module for easy migration.

    Usage:
        import logging
        from jarvis_log_client import JarvisLogHandler

        logger = logging.getLogger("my-app")
        handler = JarvisLogHandler(service="my-service")
        logger.addHandler(handler)

        logger.info("This goes to jarvis-logs", extra={"request_id": "abc123"})
    """

    def __init__(
        self,
        service: str,
        server_url: str | None = None,
        batch_size: int = 50,
        flush_interval: float = 5.0,
        level: int = logging.DEBUG,
    ):
        super().__init__(level=level)
        self.service = service
        self.server_url = server_url or _get_logs_url()
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        # Log queue for async batching
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._shutdown = threading.Event()

        # HTTP client (created in flush thread)
        self._client: httpx.Client | None = None

        # Start background flush thread
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record."""
        try:
            # Extract context from extra fields
            context: dict[str, Any] = {}
            for key, value in record.__dict__.items():
                if key not in {
                    "name",
                    "msg",
                    "args",
                    "created",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "exc_info",
                    "exc_text",
                    "thread",
                    "threadName",
                    "message",
                    "taskName",
                }:
                    # Ensure value is JSON serializable
                    try:
                        json.dumps(value)
                        context[key] = value
                    except (TypeError, ValueError):
                        context[key] = str(value)

            # Add exception info if present
            if record.exc_info:
                context["exception"] = self.formatException(record.exc_info)

            entry = {
                "timestamp": datetime.utcfromtimestamp(record.created).isoformat(),
                "service": self.service,
                "level": record.levelname,
                "message": record.getMessage(),
                "context": context if context else None,
            }

            self._queue.put_nowait(entry)
        except queue.Full:
            pass  # Drop log if queue is full
        except Exception:
            self.handleError(record)

    def _flush_loop(self) -> None:
        """Background loop that flushes logs periodically."""
        self._client = httpx.Client(timeout=10.0)

        while not self._shutdown.is_set():
            try:
                self._flush_batch()
            except Exception:
                pass  # Silently ignore flush errors

            self._shutdown.wait(timeout=self.flush_interval)

        # Final flush on shutdown
        self._flush_batch()
        if self._client:
            self._client.close()

    def _flush_batch(self) -> None:
        """Flush pending logs to server."""
        batch = []
        while len(batch) < self.batch_size:
            try:
                entry = self._queue.get_nowait()
                batch.append(entry)
            except queue.Empty:
                break

        if not batch or not self._client:
            return

        # Build headers with auth credentials
        headers = {"Content-Type": "application/json"}
        headers.update(_get_auth_headers())

        try:
            self._client.post(
                f"{self.server_url}{_get_log_endpoint()}",
                json={"logs": batch},
                headers=headers,
            )
        except httpx.RequestError:
            pass  # Silently drop on network error

    def close(self) -> None:
        """Close the handler and flush remaining logs."""
        self._shutdown.set()
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        super().close()

    def flush(self) -> None:
        """Manually flush pending logs."""
        self._flush_batch()
