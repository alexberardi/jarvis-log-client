import atexit
import json
import logging
import os
import queue
import threading
from datetime import datetime
from typing import Any

import httpx


def _json_safe_default(obj: Any) -> Any:
    """JSON encoder fallback for log payloads.

    Why: log context often carries values that aren't JSON-native — most
    commonly numpy scalars (e.g. ``oww.predict()`` returns float32). A
    single such value would raise TypeError out of ``json.dumps`` and,
    pre-fix, killed the flush daemon thread permanently — silencing all
    remote logging until the process restarted.
    """
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:
            pass
    return str(obj)

_DEFAULT_LOGS_URL = "http://localhost:7702"


def _get_logs_url() -> str:
    """
    Get the jarvis-logs URL.

    Priority:
    1. jarvis-config-client (if initialized)
    2. JARVIS_LOGS_URL env var
    3. Default: http://localhost:7702
    """
    # Try config client first (if available and initialized)
    try:
        from jarvis_config_client import get_service_url
        url = get_service_url("logs")
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

    Instances are cached by service name so that multiple modules
    creating ``JarvisLogger(service="jarvis-node")`` share a single
    queue and flush thread (important on resource-constrained devices).

    Usage:
        from jarvis_log_client import init, JarvisLogger

        # Initialize credentials once at startup
        init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))

        # Create logger
        logger = JarvisLogger(service="my-service")
        logger.info("Application started")
        logger.error("Something went wrong", error=str(e), request_id="abc123")
    """

    _instances: dict[str, "JarvisLogger"] = {}
    _instances_lock = threading.Lock()

    # Class-level (process-wide) flush worker. One thread regardless of how
    # many JarvisLogger instances exist. Earlier design started a daemon
    # ``_flush_loop`` per instance; under code paths that legitimately
    # re-instantiate JarvisLogger (Pantry install re-imports custom_commands
    # modules, mobile-driven settings snapshot triggers ``refresh_now()``,
    # etc.) thread count grew unbounded. On a Pi Zero 2W, observed 24 idle
    # flush threads contending for the GIL after a handful of installs,
    # adding multi-second latency to the wake loop.
    #
    # The per-service singleton above already prevents same-service re-init
    # in normal operation, but it's not sufficient: every UNIQUE service
    # string still spawned its own thread, and any future regression that
    # bypassed the singleton (e.g. a different class instance from a stale
    # sys.modules path) would silently leak again. Collapsing to one shared
    # thread bounds this at one no matter what.
    _global_thread: "threading.Thread | None" = None
    _global_thread_lock = threading.Lock()
    _global_shutdown = threading.Event()

    def __new__(
        cls,
        service: str,
        server_url: str | None = None,
        console_level: str = "INFO",
        remote_level: str = "DEBUG",
        batch_size: int = 50,
        flush_interval: float = 5.0,
    ) -> "JarvisLogger":
        # Cache on service name — the vast majority of callers use
        # identical defaults so the first instance wins.
        with cls._instances_lock:
            if service in cls._instances:
                return cls._instances[service]
            instance = super().__new__(cls)
            cls._instances[service] = instance
            return instance

    def __init__(
        self,
        service: str,
        server_url: str | None = None,
        console_level: str = "INFO",
        remote_level: str = "DEBUG",
        batch_size: int = 50,
        flush_interval: float = 5.0,
    ):
        # Skip re-init if this is a cached instance
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.service = service
        self._explicit_url: str | None = server_url  # None = resolve lazily
        self.server_url = server_url or _get_logs_url()
        self._url_resolved_from_config: bool = False
        self.console_level = getattr(logging, console_level.upper(), logging.WARNING)
        self.remote_level = getattr(logging, remote_level.upper(), logging.DEBUG)
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        # Log queue for async batching (bounded to prevent OOM on
        # resource-constrained devices when the log server is unreachable)
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._shutdown = threading.Event()

        # HTTP client created lazily in the shared flush worker on first
        # batch from this instance — different instances can target
        # different ``server_url``s so each owns its own client.
        self._client: httpx.Client | None = None

        # Per-instance backoff window. The shared global flush worker
        # checks this before calling ``_flush_batch`` so a slow server
        # (5 min cap) doesn't starve other instances' flushes.
        self._next_retry_at_monotonic: float = 0.0

        # Backoff state: when the server is unreachable, space out retries
        # instead of spamming every flush_interval (5s)
        self._consecutive_failures: int = 0
        self._max_backoff: float = 300.0  # cap at 5 minutes

        # Auth-failure giveup: 401/403 means our credentials are wrong, not
        # that the network is flaky. Retrying every 5 minutes for days burns
        # battery, churns log entries through the queue, and never recovers
        # without a redeploy. After AUTH_GIVEUP_THRESHOLD consecutive 401/403
        # responses we disable remote logging until the next process restart.
        self._consecutive_auth_failures: int = 0
        self._auth_giveup_threshold: int = 10
        self._remote_disabled: bool = False
        self._remote_disabled_reason: str | None = None

        # Console logger for fallback
        self._console_logger = logging.getLogger(f"jarvis.{service}")
        self._console_logger.setLevel(logging.DEBUG)
        # propagate=False is load-bearing: without it every emit duplicates.
        # Two propagation paths used to fire:
        #   1) Ancestor JarvisLoggers — e.g. JarvisLogger(service="cmd.spotify")
        #      installs a StreamHandler on logger "jarvis.cmd.spotify" with the
        #      same formatter, so a sibling like "jarvis.cmd.spotify.keepalive"
        #      emits ONCE on its own handler and AGAIN on the ancestor's,
        #      producing identical [INFO] lines in journalctl.
        #   2) The root logger — main.py runs alembic migrations on startup, and
        #      ``alembic.ini``'s ``[handler_console]`` formatter
        #      (``%(levelname)-5.5s [%(name)s] %(message)s``) is installed on
        #      root and STAYS installed for the rest of the process. Every
        #      JarvisLogger emit propagated up and emitted a third line.
        # The remote pipeline is independent of stdlib propagation, so we just
        # turn propagation off and keep one handler per logger.
        self._console_logger.propagate = False
        if not self._console_logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(self.console_level)
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            self._console_logger.addHandler(handler)

        # Ensure the class-level flush worker is running. Idempotent — only
        # the first instance actually spawns the thread.
        type(self)._ensure_global_thread()

        # Register shutdown hook
        atexit.register(self.shutdown)

    @classmethod
    def _ensure_global_thread(cls) -> None:
        """Start the single class-level flush worker on first call.

        Idempotent and thread-safe. Subsequent calls are cheap no-ops once
        the thread is running. The thread is a daemon so it doesn't block
        process exit; ``atexit`` triggers a final flush on each instance.
        """
        with cls._global_thread_lock:
            if cls._global_thread is not None and cls._global_thread.is_alive():
                return
            cls._global_shutdown.clear()
            cls._global_thread = threading.Thread(
                target=cls._global_flush_loop,
                daemon=True,
                name="JarvisLogger-flush",
            )
            cls._global_thread.start()

    @classmethod
    def _global_flush_loop(cls) -> None:
        """Single process-wide flush worker — drains every instance's queue.

        Sleeps a fixed 1 s between rounds (much shorter than any single
        instance's ``flush_interval`` so per-instance backoff windows
        remain accurate). Each ``_flush_batch_with_backoff`` call is a
        fast no-op when the instance has nothing to send or is in its
        backoff window, so iterating ~30 instances takes microseconds.
        """
        import time as _time

        while not cls._global_shutdown.is_set():
            # Snapshot instances under the lock so re-entrancy from a
            # logger.info() call during iteration can't mutate underfoot.
            with cls._instances_lock:
                instances = list(cls._instances.values())

            now = _time.monotonic()
            for inst in instances:
                if now < inst._next_retry_at_monotonic:
                    continue
                try:
                    inst._flush_batch()
                except (httpx.HTTPError, OSError) as e:
                    inst._console_logger.warning(
                        f"Log flush error ({type(e).__name__}): {e}"
                    )
                except Exception as e:
                    # Anything else (serialize bug, etc.) must NOT take the
                    # worker down — that would silently disable remote
                    # logging for the rest of the process.
                    inst._console_logger.error(
                        f"Log flush worker caught unexpected error, "
                        f"continuing: {type(e).__name__}: {e}"
                    )

                # After the call, update the per-instance backoff window
                # based on whatever ``_flush_batch`` left in
                # ``_consecutive_failures``.
                if inst._consecutive_failures > 0:
                    backoff = min(
                        inst.flush_interval * (2 ** inst._consecutive_failures),
                        inst._max_backoff,
                    )
                    inst._next_retry_at_monotonic = _time.monotonic() + backoff
                else:
                    inst._next_retry_at_monotonic = 0.0

            # 1 s tick. Doesn't matter that it's shorter than the default
            # 5 s flush_interval — the backoff check above means each
            # instance still only sends every flush_interval seconds when
            # healthy (because _flush_batch drains at most batch_size and
            # is a no-op when the queue is empty).
            cls._global_shutdown.wait(timeout=1.0)

        # Final flush across all instances on shutdown.
        with cls._instances_lock:
            instances = list(cls._instances.values())
        for inst in instances:
            try:
                inst._flush_batch()
            except Exception as e:
                inst._console_logger.warning(
                    f"Final log flush failed: {type(e).__name__}: {e}"
                )
            if inst._client is not None:
                try:
                    inst._client.close()
                except Exception:
                    pass

    def _maybe_resolve_url(self) -> None:
        """Re-resolve the logs URL from config-client if not yet resolved.

        The logger is often created before the config-client is initialized
        (especially on Pi nodes), so the URL falls back to localhost:7702.
        On the first connection failure we retry config-client resolution
        in case it has since been initialized with the correct host URL.
        """
        if self._explicit_url or self._url_resolved_from_config:
            return
        try:
            from jarvis_config_client import get_service_url
            url = get_service_url("logs")
            if url and url != self.server_url:
                self._console_logger.info(
                    "Log client URL updated from config-service: %s", url
                )
                self.server_url = url
                self._url_resolved_from_config = True
        except (ImportError, RuntimeError):
            pass

    def _flush_batch(self) -> None:
        """Flush pending logs to server."""
        # Once we've given up on auth, drain the queue so it doesn't pile up
        # forever and keep memory bounded.
        if self._remote_disabled:
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            return

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

        # Serialize ourselves so we can plug in the safe default and
        # so a bad value drops the batch instead of raising past httpx.
        try:
            body = json.dumps(
                {"logs": batch}, default=_json_safe_default
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            self._console_logger.warning(
                f"Log batch dropped (serialize error): {type(e).__name__}: {e}"
            )
            return

        # Lazy-create the httpx client on first batch from this instance.
        # The shared global flush worker doesn't own a client (each instance
        # may have its own ``server_url``), so the instance creates it on
        # demand.
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)

        try:
            response = self._client.post(
                f"{self.server_url}{_get_log_endpoint()}",
                content=body,
                headers=headers,
            )
            if response.status_code in (204, 200):
                if self._consecutive_failures > 0:
                    self._console_logger.info(
                        "Log server reconnected after %d failures",
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0
                self._consecutive_auth_failures = 0
            else:
                self._consecutive_failures += 1
                if response.status_code in (401, 403):
                    self._consecutive_auth_failures += 1
                    if self._consecutive_auth_failures >= self._auth_giveup_threshold:
                        self._disable_remote(
                            f"HTTP {response.status_code} after "
                            f"{self._consecutive_auth_failures} attempts"
                        )
                        return
                else:
                    self._consecutive_auth_failures = 0
                self._report_batch_failure(
                    batch, f"HTTP {response.status_code}"
                )
        except httpx.RequestError as e:
            self._consecutive_failures += 1
            self._consecutive_auth_failures = 0  # network error, not auth
            self._report_batch_failure(
                batch, f"{type(e).__name__}: {e}"
            )
            # URL may have been wrong (localhost fallback before config-client init)
            self._maybe_resolve_url()

    def _disable_remote(self, reason: str) -> None:
        """Stop sending logs remotely after persistent auth failures.

        Drains the in-memory queue and emits a single console warning so
        the operator can spot it. Callers that want to surface this to a UI
        should poll ``is_remote_disabled`` / ``get_remote_status()``.
        """
        self._remote_disabled = True
        self._remote_disabled_reason = reason
        self._console_logger.error(
            "Remote logging disabled: %s. Logs will continue to console only "
            "until process restart.",
            reason,
        )
        # Drain queue immediately to free memory.
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    @property
    def is_remote_disabled(self) -> bool:
        """True if remote logging has been turned off due to auth failures."""
        return self._remote_disabled

    def get_remote_status(self) -> dict[str, Any]:
        """Snapshot of remote-logging health for UI surfacing."""
        return {
            "disabled": self._remote_disabled,
            "disabled_reason": self._remote_disabled_reason,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_auth_failures": self._consecutive_auth_failures,
            "queue_depth": self._queue.qsize(),
        }

    def _report_batch_failure(self, batch: list[dict], reason: str) -> None:
        """Note a failed batch send without re-emitting every entry.

        Why not replay each entry: the originals already went to console at
        emit time via the active handlers (JarvisLogger's StreamHandler for
        INFO+, propagation to root for everything). Re-emitting on failure
        produced apparent-duplicate log lines that masked real duplicate
        execution and made journal analysis unreliable. We accept losing
        the entries on the remote side (the post is going to fail again
        next time anyway until auth is fixed) and just record the count.
        """
        next_backoff = min(
            self.flush_interval * (2 ** self._consecutive_failures),
            self._max_backoff,
        )
        self._console_logger.warning(
            "Log batch send failed | reason=%s entries_dropped=%d next_retry=%.0fs",
            reason,
            len(batch),
            next_backoff,
        )

    def _log(self, level: str, message: str, **context: Any) -> None:
        """Internal method to queue a log entry."""
        level_value = getattr(logging, level, logging.INFO)

        # Always log to console if level is high enough
        if level_value >= self.console_level:
            console_msg = message
            if context:
                console_msg = f"{message} | {context}"
            self._console_logger.log(level_value, console_msg)

        # Queue for remote if level is high enough and remote isn't disabled
        if level_value >= self.remote_level and not self._remote_disabled:
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
        """Best-effort flush of this instance's queue on shutdown.

        The flush thread is class-level (one per process) so this method
        no longer joins a per-instance thread — it just drains whatever
        is queued here through ``_flush_batch``. The class-level worker
        keeps running for any other still-live instances.

        Process exit drains the global thread via the daemon flag plus
        ``atexit`` hooks set in ``__init__`` calling this method.
        """
        self._shutdown.set()
        try:
            self._flush_batch()
        except Exception:
            pass

    def flush(self) -> None:
        """Manually trigger a flush of pending logs."""
        self._flush_batch()
