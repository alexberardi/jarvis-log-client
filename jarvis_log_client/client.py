import atexit
import logging
import os
import queue
import threading
from datetime import datetime
from typing import Any

import httpx

_DEFAULT_LOGS_URL = "http://localhost:8006"


def _get_logs_url() -> str:
    """
    Get the jarvis-logs URL.

    Priority:
    1. jarvis-config-client (if initialized)
    2. JARVIS_LOGS_URL env var
    3. Default: http://localhost:8006
    """
    # Try config client first (if available and initialized)
    try:
        from jarvis_config_client import get_service_url
        url = get_service_url("jarvis-logs")
        if url:
            return url
    except (ImportError, RuntimeError):
        # ImportError: jarvis-config-client not installed
        # RuntimeError: config client not initialized
        pass

    # Fall back to env var or default
    return os.getenv("JARVIS_LOGS_URL", _DEFAULT_LOGS_URL)


# Module-level credentials cache
_app_credentials: dict[str, str] = {}
_node_credentials: dict[str, str] = {}
_auth_mode: str = "app"  # "app" or "node"


def init(app_id: str, app_key: str) -> None:
    """
    Initialize jarvis-log-client with app-to-app credentials.

    Call this once at application startup before creating any JarvisLogger instances.

    Args:
        app_id: Your service's app ID registered with jarvis-auth
        app_key: Your service's app key from jarvis-auth

    Usage:
        from jarvis_log_client import init
        init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))
    """
    global _auth_mode
    _app_credentials["app_id"] = app_id
    _app_credentials["app_key"] = app_key
    _auth_mode = "app"


def init_node(node_id: str, node_key: str) -> None:
    """
    Initialize jarvis-log-client with node credentials.

    Call this once at application startup before creating any JarvisLogger instances.
    Use this for nodes (e.g., Pi Zero devices) that authenticate via jarvis-auth's
    centralized node authentication.

    Args:
        node_id: The node's ID registered with jarvis-auth
        node_key: The node's secret key from jarvis-auth

    Usage:
        from jarvis_log_client import init_node
        init_node(node_id="kitchen-pi", node_key=os.getenv("JARVIS_NODE_KEY"))
    """
    global _auth_mode
    _node_credentials["node_id"] = node_id
    _node_credentials["node_key"] = node_key
    _auth_mode = "node"


def _get_auth_headers() -> dict[str, str]:
    """Get authentication headers based on auth mode."""
    if _auth_mode == "node":
        node_id = _node_credentials.get("node_id") or os.getenv("JARVIS_NODE_ID")
        node_key = _node_credentials.get("node_key") or os.getenv("JARVIS_NODE_KEY")
        if node_id and node_key:
            return {
                "X-Node-Id": node_id,
                "X-Node-Key": node_key,
            }
    else:
        app_id = _app_credentials.get("app_id") or os.getenv("JARVIS_APP_ID")
        app_key = _app_credentials.get("app_key") or os.getenv("JARVIS_APP_KEY")
        if app_id and app_key:
            return {
                "X-Jarvis-App-Id": app_id,
                "X-Jarvis-App-Key": app_key,
            }
    return {}


def _get_log_endpoint() -> str:
    """Get the log endpoint path based on auth mode."""
    if _auth_mode == "node":
        return "/api/v0/node/logs/batch"
    return "/api/v0/logs/batch"


class JarvisLogger:
    """
    Centralized logger for jarvis microservices.

    Sends logs to jarvis-logs server with async batching.
    Falls back to console logging if server is unavailable.

    Usage:
        from jarvis_log_client import init, JarvisLogger

        # Initialize credentials once at startup
        init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))

        # Create logger
        logger = JarvisLogger(service="my-service")
        logger.info("Application started")
        logger.error("Something went wrong", error=str(e), request_id="abc123")
    """

    def __init__(
        self,
        service: str,
        server_url: str | None = None,
        console_level: str = "WARNING",
        remote_level: str = "DEBUG",
        batch_size: int = 50,
        flush_interval: float = 5.0,
    ):
        self.service = service
        self.server_url = server_url or _get_logs_url()
        self.console_level = getattr(logging, console_level.upper(), logging.WARNING)
        self.remote_level = getattr(logging, remote_level.upper(), logging.DEBUG)
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        # Log queue for async batching
        self._queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._flush_thread: threading.Thread | None = None

        # HTTP client (created lazily in flush thread)
        self._client: httpx.Client | None = None

        # Console logger for fallback
        self._console_logger = logging.getLogger(f"jarvis.{service}")
        self._console_logger.setLevel(logging.DEBUG)
        if not self._console_logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(self.console_level)
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            self._console_logger.addHandler(handler)

        # Start background flush thread
        self._start_flush_thread()

        # Register shutdown hook
        atexit.register(self.shutdown)

    def _start_flush_thread(self) -> None:
        """Start the background thread for flushing logs."""
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        """Background loop that flushes logs periodically."""
        self._client = httpx.Client(timeout=10.0)

        while not self._shutdown.is_set():
            try:
                self._flush_batch()
            except Exception as e:
                self._console_logger.warning(f"Log flush error: {e}")

            # Wait for flush interval or shutdown
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

        if not batch:
            return

        # Build headers with auth credentials
        headers = {"Content-Type": "application/json"}
        headers.update(_get_auth_headers())

        try:
            response = self._client.post(
                f"{self.server_url}{_get_log_endpoint()}",
                json={"logs": batch},
                headers=headers,
            )
            if response.status_code not in (204, 200):
                self._fallback_to_console(batch)
        except httpx.RequestError:
            self._fallback_to_console(batch)

    def _fallback_to_console(self, batch: list[dict]) -> None:
        """Log entries to console when server is unavailable."""
        for entry in batch:
            level = entry.get("level", "INFO")
            message = entry.get("message", "")
            context = entry.get("context")
            if context:
                message = f"{message} | {context}"
            log_level = getattr(logging, level, logging.INFO)
            self._console_logger.log(log_level, message)

    def _log(self, level: str, message: str, **context: Any) -> None:
        """Internal method to queue a log entry."""
        level_value = getattr(logging, level, logging.INFO)

        # Always log to console if level is high enough
        if level_value >= self.console_level:
            console_msg = message
            if context:
                console_msg = f"{message} | {context}"
            self._console_logger.log(level_value, console_msg)

        # Queue for remote if level is high enough
        if level_value >= self.remote_level:
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "service": self.service,
                "level": level,
                "message": message,
                "context": context if context else None,
            }
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                pass  # Drop log if queue is full

    def debug(self, message: str, **context: Any) -> None:
        """Log a debug message."""
        self._log("DEBUG", message, **context)

    def info(self, message: str, **context: Any) -> None:
        """Log an info message."""
        self._log("INFO", message, **context)

    def warning(self, message: str, **context: Any) -> None:
        """Log a warning message."""
        self._log("WARNING", message, **context)

    def error(self, message: str, **context: Any) -> None:
        """Log an error message."""
        self._log("ERROR", message, **context)

    def critical(self, message: str, **context: Any) -> None:
        """Log a critical message."""
        self._log("CRITICAL", message, **context)

    def shutdown(self) -> None:
        """Gracefully shutdown the logger."""
        self._shutdown.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

    def flush(self) -> None:
        """Manually trigger a flush of pending logs."""
        self._flush_batch()
