"""Tests for JarvisLogger client."""

import os
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from jarvis_log_client import init, init_node, JarvisLogger
from jarvis_log_client.client import (
    _app_credentials,
    _node_credentials,
    _get_auth_headers,
    _get_log_endpoint,
)
import jarvis_log_client.client as client_module


def _reset_auth_state():
    """Reset all auth state to defaults."""
    _app_credentials.clear()
    _node_credentials.clear()
    client_module._auth_mode = "app"


class TestInit:
    """Tests for init() function."""

    def setup_method(self):
        """Clear credentials before each test."""
        _reset_auth_state()

    def test_init_stores_credentials(self):
        """Test that init() stores credentials."""
        init(app_id="test-app", app_key="test-key")
        assert _app_credentials["app_id"] == "test-app"
        assert _app_credentials["app_key"] == "test-key"

    def test_init_overwrites_previous(self):
        """Test that init() overwrites previous credentials."""
        init(app_id="first", app_key="first-key")
        init(app_id="second", app_key="second-key")
        assert _app_credentials["app_id"] == "second"
        assert _app_credentials["app_key"] == "second-key"


class TestGetAuthHeaders:
    """Tests for _get_auth_headers() function."""

    def setup_method(self):
        """Clear credentials before each test."""
        _reset_auth_state()

    def test_returns_headers_from_init(self):
        """Test headers from init()."""
        init(app_id="my-app", app_key="my-key")
        headers = _get_auth_headers()
        assert headers == {
            "X-Jarvis-App-Id": "my-app",
            "X-Jarvis-App-Key": "my-key",
        }

    def test_returns_headers_from_env(self):
        """Test headers from environment variables."""
        with patch.dict(os.environ, {
            "JARVIS_APP_ID": "env-app",
            "JARVIS_APP_KEY": "env-key",
        }):
            headers = _get_auth_headers()
            assert headers == {
                "X-Jarvis-App-Id": "env-app",
                "X-Jarvis-App-Key": "env-key",
            }

    def test_init_takes_precedence_over_env(self):
        """Test that init() credentials take precedence over env vars."""
        init(app_id="init-app", app_key="init-key")
        with patch.dict(os.environ, {
            "JARVIS_APP_ID": "env-app",
            "JARVIS_APP_KEY": "env-key",
        }):
            headers = _get_auth_headers()
            assert headers["X-Jarvis-App-Id"] == "init-app"
            assert headers["X-Jarvis-App-Key"] == "init-key"

    def test_returns_empty_without_credentials(self):
        """Test that empty dict is returned without credentials."""
        with patch.dict(os.environ, {}, clear=True):
            # Ensure no env vars
            os.environ.pop("JARVIS_APP_ID", None)
            os.environ.pop("JARVIS_APP_KEY", None)
            headers = _get_auth_headers()
            assert headers == {}

    def test_returns_empty_with_partial_credentials(self):
        """Test that empty dict is returned with partial credentials."""
        init(app_id="my-app", app_key="")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("JARVIS_APP_KEY", None)
            headers = _get_auth_headers()
            assert headers == {}


class TestJarvisLogger:
    """Tests for JarvisLogger class."""

    def setup_method(self):
        """Clear credentials and instance cache before each test."""
        _reset_auth_state()
        JarvisLogger._instances.clear()

    def test_logger_creation(self):
        """Test basic logger creation."""
        logger = JarvisLogger(service="test-service")
        assert logger.service == "test-service"
        assert logger.server_url == "http://localhost:7702"
        logger.shutdown()

    def test_logger_custom_url(self):
        """Test logger with custom server URL."""
        logger = JarvisLogger(service="test", server_url="http://custom:9000")
        assert logger.server_url == "http://custom:9000"
        logger.shutdown()

    def test_logger_url_from_env(self):
        """Test logger reads URL from environment."""
        with patch.dict(os.environ, {"JARVIS_LOGS_URL": "http://env-server:8080"}):
            logger = JarvisLogger(service="test")
            assert logger.server_url == "http://env-server:8080"
            logger.shutdown()

    def test_log_methods_exist(self):
        """Test that all log level methods exist."""
        logger = JarvisLogger(service="test")
        assert hasattr(logger, "debug")
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")
        assert hasattr(logger, "critical")
        logger.shutdown()

    def test_log_queues_entry(self):
        """Test that logging queues an entry."""
        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",  # Don't spam console
        )

        logger.info("Test message", key="value")

        # Check queue has entry
        assert not logger._queue.empty()
        entry = logger._queue.get_nowait()
        assert entry["service"] == "test"
        assert entry["level"] == "INFO"
        assert entry["message"] == "Test message"
        assert entry["context"] == {"key": "value"}
        logger.shutdown()

    def test_log_respects_remote_level(self):
        """Test that logs below remote_level are not queued."""
        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",
            remote_level="WARNING",  # Only WARNING and above
        )

        logger.info("Should not queue")  # INFO < WARNING
        assert logger._queue.empty()

        logger.warning("Should queue")
        assert not logger._queue.empty()
        logger.shutdown()

    def test_many_instances_share_one_flush_thread(self):
        """The whole point of the refactor: N JarvisLogger instances must
        result in exactly ONE flush thread, not N.

        Pre-fix on jarvis-dev, 24 ``_flush_loop`` threads accumulated after
        a handful of Pantry installs (each install re-imports custom_commands
        modules whose module-level ``logger = JarvisLogger(...)`` lines each
        spawned a daemon thread). The class-level worker collapses that to 1.

        We count Python-level threads named "JarvisLogger-flush" — that's
        the worker's name. Any other count means we regressed.
        """
        # Make a bunch of distinct-service loggers + some duplicates.
        loggers = [JarvisLogger(service=f"test.svc.{i}") for i in range(20)]
        loggers += [JarvisLogger(service="test.svc.5") for _ in range(5)]

        # Give the global thread a moment to be ensured.
        time.sleep(0.1)

        flush_threads = [
            t for t in threading.enumerate()
            if t.name == "JarvisLogger-flush"
        ]
        assert len(flush_threads) == 1, (
            f"expected exactly 1 JarvisLogger-flush thread, got "
            f"{len(flush_threads)}: {[t.name for t in flush_threads]}"
        )

        for lg in loggers:
            lg.shutdown()

    def test_shutdown_drains_queue_and_global_thread_keeps_running(self):
        """Shutdown is per-instance and drains its queue; the class-level
        flush worker stays alive for any other still-live loggers.

        Per-instance threads were removed because Pantry-install re-imports
        and other code paths legitimately re-instantiate JarvisLogger,
        which accumulated daemon threads (~24 on jarvis-dev). See client.py
        comments above the class-level ``_global_thread`` field.
        """
        logger = JarvisLogger(service="test")
        # The class-level thread must be running after at least one instance.
        assert JarvisLogger._global_thread is not None
        assert JarvisLogger._global_thread.is_alive()

        logger.warning("queued")
        assert not logger._queue.empty()
        logger.shutdown()
        # Shutdown drains this instance's queue (best-effort).
        # The class thread keeps running for any other JarvisLogger.
        assert JarvisLogger._global_thread.is_alive()

    def test_context_is_optional(self):
        """Test logging without context."""
        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",
        )

        logger.info("No context")
        entry = logger._queue.get_nowait()
        assert entry["context"] is None
        logger.shutdown()

    def test_console_logger_does_not_propagate(self):
        """Records must not propagate to ancestor or root handlers.

        Regression: when JarvisLogger(service="cmd.spotify.keepalive") emits,
        records used to also fire on JarvisLogger(service="cmd.spotify")'s
        handler (ancestor) AND on the root handler that ``alembic.ini``'s
        ``[handler_console]`` installs during migrations. Result: every line
        appeared 2-3× in journalctl. Setting propagate=False on the per-service
        console logger is the fix.
        """
        import io
        import logging

        root = logging.getLogger()
        root_buf = io.StringIO()
        root_handler = logging.StreamHandler(root_buf)
        root_handler.setLevel(logging.DEBUG)
        prior_root_level = root.level
        root.addHandler(root_handler)
        root.setLevel(logging.DEBUG)
        try:
            logger = JarvisLogger(
                service="test-no-propagate",
                console_level="CRITICAL",
            )
            assert logger._console_logger.propagate is False
            logger.info("must not reach root")
            assert "must not reach root" not in root_buf.getvalue()
            logger.shutdown()
        finally:
            root.removeHandler(root_handler)
            root.setLevel(prior_root_level)

    def test_underlying_logger_has_propagate_false(self):
        """PIN: the stdlib Logger that JarvisLogger wraps must have
        ``propagate`` set to False.

        This is the documented triple-emission fix (client.py: the per-service
        ``logging.getLogger("jarvis.<service>")`` sets ``propagate = False``).
        We assert it both on the instance attribute the logger exposes and on
        the same logger fetched independently via ``logging.getLogger`` to prove
        the flag lives on the shared, named stdlib logger — not on some private
        wrapper. Reverting the fix (dropping the ``propagate = False`` line, the
        stdlib default being True) makes both assertions fail.

        Runs fully offline: console_level=CRITICAL suppresses console output and
        nothing here touches the logs server.
        """
        import logging

        logger = JarvisLogger(
            service="test-propagate-flag",
            console_level="CRITICAL",
        )
        try:
            # The attribute JarvisLogger exposes for its underlying logger.
            assert logger._console_logger.propagate is False
            # Independently fetched by name — same singleton stdlib Logger.
            named = logging.getLogger("jarvis.test-propagate-flag")
            assert named is logger._console_logger
            assert named.propagate is False
        finally:
            logger.shutdown()

    def test_single_emit_not_duplicated_to_ancestor_logger(self):
        """PIN: emitting one record must reach exactly one handler, not also
        the ancestor JarvisLogger's handler.

        This reproduces propagation path #1 from the client.py comment: a
        child service name (``jarvis.cmd.spotify.keepalive``) used to emit once
        on its own StreamHandler AND again on the ancestor
        (``jarvis.cmd.spotify``) handler, producing duplicate journal lines.
        With ``propagate = False`` the ancestor never sees the child's record.

        We install a counting handler on the ancestor logger and assert it
        captures zero records when the child emits. Reverting the fix
        (propagate defaults to True) makes the ancestor see the record and this
        assertion fails. No logs server involved — pure stdlib handlers.
        """
        import logging

        ancestor = logging.getLogger("jarvis.cmd.spotify")
        records: list[logging.LogRecord] = []

        class _CountingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        counting = _CountingHandler()
        counting.setLevel(logging.DEBUG)
        prior_level = ancestor.level
        ancestor.addHandler(counting)
        ancestor.setLevel(logging.DEBUG)
        try:
            # console_level=INFO so the child's own handler WOULD emit — the
            # point is that the ancestor still must not see it.
            child = JarvisLogger(
                service="cmd.spotify.keepalive",
                console_level="INFO",
            )
            child.info("emit once")
            assert child._console_logger.propagate is False
            assert len(records) == 0, (
                f"ancestor logger saw {len(records)} record(s); expected 0 — "
                "child record propagated up (triple-emission regression)"
            )
            child.shutdown()
        finally:
            ancestor.removeHandler(counting)
            ancestor.setLevel(prior_level)


class TestJarvisLoggerFlush:
    """Tests for JarvisLogger flush behavior."""

    def setup_method(self):
        """Clear credentials and instance cache before each test."""
        _reset_auth_state()
        JarvisLogger._instances.clear()

    def test_failed_batch_logs_single_warning_not_per_entry(self):
        """When the remote post fails, the client must NOT re-emit every
        queued entry to console. The originals already went to console at
        emit time via active handlers. Re-emitting on failure produced
        apparent-duplicate log lines that masked real duplicates and made
        journal analysis unreliable. Verify only one summary warning is
        emitted, regardless of batch size."""
        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",
        )
        logger.shutdown()
        logger._shutdown.clear()

        client = MagicMock()
        rejected = MagicMock()
        rejected.status_code = 401
        client.post.return_value = rejected
        logger._client = client

        warnings: list[str] = []
        original_warning = logger._console_logger.warning

        def capture_warning(msg, *args, **kwargs):
            warnings.append(msg % args if args else msg)
            return original_warning(msg, *args, **kwargs)

        logger._console_logger.warning = capture_warning

        for i in range(5):
            logger.info(f"entry {i}")

        logger._flush_batch()

        assert len(warnings) == 1, (
            f"expected exactly one summary warning, got {len(warnings)}: {warnings}"
        )
        assert "entries_dropped=5" in warnings[0]
        assert "401" in warnings[0]

    def test_flush_batch_serializes_non_native_types(self):
        """A context value that stdlib json can't serialize must not crash
        the batch — it should be coerced (via .item() / .tolist() / str()).
        Regression: numpy.float32 in `Wake word score` killed the flush
        thread on 2026-04-25."""

        class FakeFloat32:
            def __init__(self, v: float) -> None:
                self._v = v

            def item(self) -> float:
                return self._v

        captured: dict = {}

        def fake_post(*_args, content=None, **_kwargs):
            captured["body"] = content
            response = MagicMock()
            response.status_code = 204
            return response

        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",
        )
        logger.shutdown()  # we'll drive the flush manually

        client = MagicMock()
        client.post.side_effect = fake_post
        logger._client = client
        logger._shutdown.clear()

        logger.info("score", score=FakeFloat32(0.963))

        logger._flush_batch()

        assert "body" in captured, "post() was not called"
        assert b"0.963" in captured["body"]

    def test_persistent_auth_failures_disable_remote_logging(self):
        """After AUTH_GIVEUP_THRESHOLD consecutive 401/403 responses the
        client should mark itself disabled, drain its queue, log one error,
        and stop calling .post() on subsequent flushes.

        Why: kitchen Pi was retrying every 5 min for days against an old
        log endpoint that consistently returned 403. The retries did
        nothing useful and kept the queue + thread busy."""
        logger = JarvisLogger(service="test", console_level="CRITICAL")
        logger.shutdown()
        logger._shutdown.clear()

        client = MagicMock()
        rejected = MagicMock()
        rejected.status_code = 403
        client.post.return_value = rejected
        logger._client = client

        # Push past the giveup threshold.
        for _ in range(logger._auth_giveup_threshold):
            logger.info("doomed entry")
            logger._flush_batch()

        assert logger.is_remote_disabled, "remote logging should be disabled"
        status = logger.get_remote_status()
        assert status["disabled"] is True
        assert "403" in (status["disabled_reason"] or "")

        # Subsequent log calls must NOT enqueue, and flush must NOT post.
        client.post.reset_mock()
        for _ in range(20):
            logger.info("dropped entry")
        logger._flush_batch()
        assert client.post.call_count == 0
        assert logger.get_remote_status()["queue_depth"] == 0

    def test_transient_failures_do_not_disable_remote_logging(self):
        """Network errors (RequestError) and 5xx responses must NOT trip the
        auth-giveup path — they're transient and should keep retrying with
        backoff. Only 401/403 indicates an unrecoverable auth misconfig."""
        logger = JarvisLogger(service="test", console_level="CRITICAL")
        logger.shutdown()
        logger._shutdown.clear()

        client = MagicMock()
        rejected = MagicMock()
        rejected.status_code = 503
        client.post.return_value = rejected
        logger._client = client

        for _ in range(logger._auth_giveup_threshold + 5):
            logger.info("entry")
            logger._flush_batch()

        assert not logger.is_remote_disabled, (
            "5xx should not disable remote logging"
        )
        assert logger._consecutive_auth_failures == 0

    def test_flush_loop_survives_serializer_crash(self):
        """If _flush_batch raises (e.g. broken httpx mock), the daemon loop
        keeps running so the next batch still gets a chance to be sent."""
        logger = JarvisLogger(
            service="test",
            console_level="CRITICAL",
            flush_interval=0.05,
        )

        boom_count = {"n": 0}

        def boom():
            boom_count["n"] += 1
            raise RuntimeError("simulated serializer failure")

        logger._flush_batch = boom

        time.sleep(2.5)  # global worker tick is 1 s

        assert JarvisLogger._global_thread is not None
        assert JarvisLogger._global_thread.is_alive(), (
            "global flush worker died on RuntimeError — it must catch and "
            "continue so one bad logger doesn't kill the whole pipeline"
        )
        assert boom_count["n"] >= 2, (
            "global worker did not retry after first crash"
        )

        logger.shutdown()


class TestInitNode:
    """Tests for init_node() function."""

    def setup_method(self):
        """Clear credentials before each test."""
        _reset_auth_state()

    def test_init_node_stores_credentials(self):
        """Test that init_node() stores node credentials."""
        init_node(node_id="test-node", node_key="test-key")
        assert _node_credentials["node_id"] == "test-node"
        assert _node_credentials["node_key"] == "test-key"

    def test_init_node_sets_auth_mode(self):
        """Test that init_node() sets auth mode to 'node'."""
        init_node(node_id="test-node", node_key="test-key")
        assert client_module._auth_mode == "node"

    def test_init_after_init_node_switches_back(self):
        """Test that init() switches back to app mode."""
        init_node(node_id="test-node", node_key="test-key")
        assert client_module._auth_mode == "node"
        init(app_id="test-app", app_key="test-app-key")
        assert client_module._auth_mode == "app"


class TestNodeAuthHeaders:
    """Tests for node authentication headers."""

    def setup_method(self):
        """Clear credentials before each test."""
        _reset_auth_state()

    def test_returns_node_headers_from_init_node(self):
        """Test headers from init_node()."""
        init_node(node_id="my-node", node_key="my-node-key")
        headers = _get_auth_headers()
        assert headers == {
            "X-Node-Id": "my-node",
            "X-Node-Key": "my-node-key",
        }

    def test_returns_node_headers_from_env(self):
        """Test node headers from environment variables."""
        client_module._auth_mode = "node"
        with patch.dict(os.environ, {
            "JARVIS_NODE_ID": "env-node",
            "JARVIS_NODE_KEY": "env-node-key",
        }):
            headers = _get_auth_headers()
            assert headers == {
                "X-Node-Id": "env-node",
                "X-Node-Key": "env-node-key",
            }

    def test_init_node_takes_precedence_over_env(self):
        """Test that init_node() credentials take precedence over env vars."""
        init_node(node_id="init-node", node_key="init-node-key")
        with patch.dict(os.environ, {
            "JARVIS_NODE_ID": "env-node",
            "JARVIS_NODE_KEY": "env-node-key",
        }):
            headers = _get_auth_headers()
            assert headers["X-Node-Id"] == "init-node"
            assert headers["X-Node-Key"] == "init-node-key"


class TestGetLogEndpoint:
    """Tests for _get_log_endpoint() function."""

    def setup_method(self):
        """Clear credentials before each test."""
        _reset_auth_state()

    def test_returns_app_endpoint_by_default(self):
        """Test that app endpoint is returned by default."""
        endpoint = _get_log_endpoint()
        assert endpoint == "/api/v0/logs/batch"

    def test_returns_app_endpoint_after_init(self):
        """Test that app endpoint is returned after init()."""
        init(app_id="test-app", app_key="test-key")
        endpoint = _get_log_endpoint()
        assert endpoint == "/api/v0/logs/batch"

    def test_returns_node_endpoint_after_init_node(self):
        """Test that node endpoint is returned after init_node()."""
        init_node(node_id="test-node", node_key="test-key")
        endpoint = _get_log_endpoint()
        assert endpoint == "/api/v0/node/logs/batch"

    def test_switching_auth_mode_changes_endpoint(self):
        """Test that switching auth mode changes the endpoint."""
        # Start with app mode
        init(app_id="app", app_key="key")
        assert _get_log_endpoint() == "/api/v0/logs/batch"

        # Switch to node mode
        init_node(node_id="node", node_key="key")
        assert _get_log_endpoint() == "/api/v0/node/logs/batch"

        # Switch back to app mode
        init(app_id="app2", app_key="key2")
        assert _get_log_endpoint() == "/api/v0/logs/batch"
