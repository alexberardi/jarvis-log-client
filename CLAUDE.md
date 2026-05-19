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
    logs_url="http://localhost:7702"  # optional
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
| `JARVIS_LOGS_URL` | http://localhost:7702 | Fallback logs server URL |
| `JARVIS_AUTH_APP_ID` | - | App ID for authentication (preferred) |
| `JARVIS_AUTH_APP_KEY` | - | App key for authentication (preferred) |
| `JARVIS_APP_ID` | - | App ID fallback (legacy) |
| `JARVIS_APP_KEY` | - | App key fallback (legacy) |

## Service Discovery

If `jarvis-config-client` is installed and initialized by the host service, the logs URL is automatically fetched from `jarvis-config-service`. Otherwise falls back to `JARVIS_LOGS_URL` env var.

Priority:
1. jarvis-config-client (if initialized)
2. `JARVIS_LOGS_URL` env var
3. Default: `http://localhost:7702`

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

## Invariants & gotchas

1. **Push failures fall back to console silently.** A misconfigured `JARVIS_APP_KEY`, an unreachable jarvis-logs URL, or a 401 from auth all result in **console-only logging** — the host service stays up, but your logs don't reach Loki. If you can't find a service's logs in Grafana, check the host's stdout for "Failed to push log" first. This is by design (logging should never crash the host) but it's the #1 reason logs go missing.
2. **`init()` must be called before creating a `JarvisLogger`.** Calling the logger without init falls back to stdlib logging silently — same silent-fallback principle.
3. **Logs are batched and async.** A short-lived script that exits before the batch flushes will lose pending logs. Call `shutdown()` to flush before exit (or use the context manager pattern).
4. **The `service` label on each log is what shows up in Loki as `{service="..."}`.** Keep it stable per process — changing it across deploys creates split label streams.
5. **App-credential env vars have legacy + canonical names.** Canonical: `JARVIS_AUTH_APP_ID`/`JARVIS_AUTH_APP_KEY`. Legacy fallback: `JARVIS_APP_ID`/`JARVIS_APP_KEY`. New services should set the canonical names. Existing services may still use the legacy ones.
6. **Service discovery is best-effort.** If jarvis-config-client isn't installed or initialized, this library falls back to `JARVIS_LOGS_URL` env var, then to the default. Don't depend on dynamic URL changes — restart the host to pick up new URLs.

## Used by

Every Python service in the stack: jarvis-auth, jarvis-command-center, jarvis-llm-proxy-api, jarvis-notifications, jarvis-settings-server, jarvis-tts, jarvis-whisper-api, and any others (via the standard `_setup_remote_logging()` pattern in `main.py`).

The standard pattern attaches `JarvisLogHandler` to uvicorn's loggers so request logs propagate to Loki as well as the custom logger calls.

## Stability

`0.2.0`. Stable. Public API (`init`, `JarvisLogger`, `JarvisLogHandler`, `shutdown`) is the contract. Internal batching strategy may change.
