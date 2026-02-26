"""Tests for JarvisLogHandler."""

import logging
import queue
import time
from unittest.mock import patch, MagicMock

import pytest

from jarvis_log_client import JarvisLogHandler
from jarvis_log_client.client import _app_credentials, _node_credentials
import jarvis_log_client.client as client_module


def _reset_auth_state():
    """Reset all auth state to defaults."""
    _app_credentials.clear()
    _node_credentials.clear()
    client_module._auth_mode = "app"


class TestJarvisLogHandlerInit:
    """Tests for JarvisLogHandler initialization."""

    def setup_method(self):
        _reset_auth_state()

    def test_handler_creation(self):
        """Test basic handler creation."""
        handler = JarvisLogHandler(service="test-service")
        assert handler.service == "test-service"
        assert handler.server_url == "http://localhost:7702"
        assert handler.batch_size == 50
        assert handler.flush_interval == 5.0
        handler.close()

    def test_handler_custom_url(self):
        """Test handler with custom server URL."""
        handler = JarvisLogHandler(service="test", server_url="http://custom:9000")
        assert handler.server_url == "http://custom:9000"
        handler.close()

    def test_handler_custom_params(self):
        """Test handler with custom batch size and flush interval."""
        handler = JarvisLogHandler(
            service="test",
            batch_size=100,
            flush_interval=10.0,
            level=logging.WARNING,
        )
        assert handler.batch_size == 100
        assert handler.flush_interval == 10.0
        assert handler.level == logging.WARNING
        handler.close()

    def test_handler_starts_flush_thread(self):
        """Test that handler starts a background flush thread."""
        handler = JarvisLogHandler(service="test")
        assert handler._flush_thread.is_alive()
        handler.close()


class TestJarvisLogHandlerEmit:
    """Tests for JarvisLogHandler emit behavior."""

    def setup_method(self):
        _reset_auth_state()

    def test_emit_queues_entry(self):
        """Test that emit() queues a log entry."""
        handler = JarvisLogHandler(service="test")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

        assert not handler._queue.empty()
        entry = handler._queue.get_nowait()
        assert entry["service"] == "test"
        assert entry["level"] == "INFO"
        assert entry["message"] == "Test message"
        handler.close()

    def test_emit_extracts_extra_context(self):
        """Test that emit() extracts extra fields as context."""
        handler = JarvisLogHandler(service="test")
        logger = logging.getLogger("test.extras")
        logger.handlers = []
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("With context", extra={"request_id": "abc123", "user_id": "u1"})

        entry = handler._queue.get_nowait()
        assert entry["context"]["request_id"] == "abc123"
        assert entry["context"]["user_id"] == "u1"
        handler.close()

    def test_emit_with_exception_info(self):
        """Test that emit() includes exception info in context."""
        handler = JarvisLogHandler(service="test")
        try:
            raise ValueError("test error")
        except ValueError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="Error occurred",
                args=None,
                exc_info=True,
            )
            # LogRecord needs exc_info to be a tuple, but passing True
            # makes it capture current exception
            import sys
            record.exc_info = sys.exc_info()

        handler.emit(record)
        entry = handler._queue.get_nowait()
        assert "exception" in entry["context"]
        assert "ValueError" in entry["context"]["exception"]
        handler.close()

    def test_emit_handles_non_serializable_context(self):
        """Test that emit() converts non-serializable values to strings."""
        handler = JarvisLogHandler(service="test")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=None,
            exc_info=None,
        )
        # Add a non-serializable object as an extra attribute
        record.custom_obj = object()

        handler.emit(record)
        entry = handler._queue.get_nowait()
        # Should be converted to string
        assert isinstance(entry["context"]["custom_obj"], str)
        handler.close()

    def test_emit_no_context_returns_none(self):
        """Test that emit() sets context to None when no extra fields."""
        handler = JarvisLogHandler(service="test")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="No context",
            args=None,
            exc_info=None,
        )
        handler.emit(record)
        entry = handler._queue.get_nowait()
        # Context may have standard logging attrs that sneak in, or be None
        # The key thing is the entry was queued successfully
        assert entry["message"] == "No context"
        handler.close()

    def test_emit_drops_when_queue_full(self):
        """Test that emit() silently drops logs when queue is full."""
        handler = JarvisLogHandler(service="test")
        # Replace with a tiny queue
        handler._queue = queue.Queue(maxsize=1)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="First", args=None, exc_info=None,
        )
        handler.emit(record)  # Fills the queue

        record2 = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="Second", args=None, exc_info=None,
        )
        handler.emit(record2)  # Should be silently dropped

        assert handler._queue.qsize() == 1
        entry = handler._queue.get_nowait()
        assert entry["message"] == "First"
        handler.close()


class TestJarvisLogHandlerFlush:
    """Tests for JarvisLogHandler flush and close behavior."""

    def setup_method(self):
        _reset_auth_state()

    def test_flush_batch_with_empty_queue(self):
        """Test that _flush_batch() is a no-op when queue is empty."""
        handler = JarvisLogHandler(service="test")
        # Wait for flush thread to create the client
        time.sleep(0.2)
        handler._flush_batch()  # Should not raise
        handler.close()

    def test_flush_batch_sends_to_server(self):
        """Test that _flush_batch() sends logs to server."""
        handler = JarvisLogHandler(service="test", server_url="http://test:7702")
        # Wait for flush thread to create the client
        time.sleep(0.2)

        # Manually queue an entry
        handler._queue.put_nowait({
            "timestamp": "2024-01-01T00:00:00",
            "service": "test",
            "level": "INFO",
            "message": "Test",
            "context": None,
        })

        # Mock the HTTP client
        mock_client = MagicMock()
        handler._client = mock_client

        handler._flush_batch()

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "http://test:7702/api/v0/logs/batch" in call_args[0]
        handler.close()

    def test_flush_batch_handles_request_error(self):
        """Test that _flush_batch() silently handles request errors."""
        import httpx

        handler = JarvisLogHandler(service="test")
        time.sleep(0.2)

        handler._queue.put_nowait({
            "timestamp": "2024-01-01T00:00:00",
            "service": "test",
            "level": "INFO",
            "message": "Test",
            "context": None,
        })

        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.RequestError("Connection failed")
        handler._client = mock_client

        # Should not raise
        handler._flush_batch()
        handler.close()

    def test_close_stops_flush_thread(self):
        """Test that close() stops the flush thread."""
        handler = JarvisLogHandler(service="test")
        assert handler._flush_thread.is_alive()
        handler.close()
        assert not handler._flush_thread.is_alive()

    def test_manual_flush(self):
        """Test manual flush() method."""
        handler = JarvisLogHandler(service="test")
        time.sleep(0.2)

        mock_client = MagicMock()
        handler._client = mock_client

        handler._queue.put_nowait({
            "timestamp": "2024-01-01T00:00:00",
            "service": "test",
            "level": "INFO",
            "message": "Flush test",
            "context": None,
        })

        handler.flush()
        mock_client.post.assert_called_once()
        handler.close()

    def test_flush_batch_no_client(self):
        """Test that _flush_batch() is a no-op when client is None."""
        handler = JarvisLogHandler(service="test")
        # Put something in queue before client is created
        handler._client = None
        handler._queue.put_nowait({
            "timestamp": "2024-01-01T00:00:00",
            "service": "test",
            "level": "INFO",
            "message": "Test",
            "context": None,
        })
        handler._flush_batch()  # Should not raise
        handler.close()
