# jarvis-log-client

Python library for sending structured logs to jarvis-logs server. Async batching with automatic console fallback.

## Quick Reference

```bash
# Install
pip install -e .

# Test
poetry run pytest
```

## Usage

```python
from jarvis_log_client import init, JarvisLogger

# Initialize once at startup
init(
    app_id="my-service",
    app_key=os.getenv("JARVIS_APP_KEY"),
    logs_url="http://localhost:8006"  # optional
)

# Create logger
logger = JarvisLogger(
    service="my-service",
    console_level="WARNING",
    remote_level="DEBUG"
)

# Log with context
logger.info("User logged in", user_id="123", request_id="abc")
logger.error("Failed to connect", error=str(e))

# Shutdown (flushes remaining logs)
from jarvis_log_client import shutdown
shutdown()
```

## Architecture

```
jarvis_log_client/
├── __init__.py    # Public API: init, JarvisLogger, JarvisLogHandler
├── client.py      # Core batching and HTTP sending
└── auth.py        # App-to-app authentication
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_LOGS_URL` | http://localhost:8006 | Logs server URL |
| `JARVIS_APP_ID` | - | App ID for authentication |
| `JARVIS_APP_KEY` | - | App key for authentication |

## Features

- **Async batching**: Logs buffered and sent in batches
- **Console fallback**: Falls back to console if server unavailable
- **Structured context**: Arbitrary key-value pairs attached to logs
- **Thread-safe**: Safe for multi-threaded applications
- **Graceful shutdown**: Flushes remaining logs on exit

## Integration with stdlib logging

```python
import logging
from jarvis_log_client import JarvisLogHandler

handler = JarvisLogHandler(service="my-service")
logging.getLogger().addHandler(handler)
```

## Version

0.2.0
