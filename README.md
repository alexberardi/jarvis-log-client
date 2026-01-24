# jarvis-log-client

Centralized logging client for jarvis microservices. Sends logs to `jarvis-logs` server with async batching and automatic fallback to console.

## Installation

```bash
# From the jarvis root directory
pip install -e jarvis-log-client/

# Or add to your service's requirements.txt
-e ../jarvis-log-client
```

## Authentication Setup

jarvis-logs requires app-to-app authentication. Initialize credentials once at application startup:

```python
import os
from jarvis_log_client import init

# Initialize credentials (call once at startup)
init(
    app_id="my-service",
    app_key=os.getenv("JARVIS_APP_KEY")
)
```

Or set environment variables:
```bash
JARVIS_APP_ID=my-service
JARVIS_APP_KEY=your-app-key-from-jarvis-auth
```

## Usage

### Option 1: JarvisLogger (Recommended)

Simple, structured logging with automatic batching:

```python
import os
from jarvis_log_client import init, JarvisLogger

# Initialize credentials once at startup
init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))

# Create logger
logger = JarvisLogger(
    service="my-service",
    console_level="WARNING",  # What goes to stdout
    remote_level="DEBUG",     # What goes to server
)

# Usage - same as standard logging
logger.info("Application started")
logger.debug("Processing request", request_id="abc123", user_id="user456")
logger.error("Database connection failed", error=str(e), retry_count=3)

# Structured context is automatically included
logger.info("Order completed", order_id="ord_123", total=99.99, items=["item1", "item2"])
```

### Option 2: JarvisLogHandler (Standard Logging Integration)

Integrates with Python's standard `logging` module:

```python
import logging
import os
from jarvis_log_client import init, JarvisLogHandler

# Initialize credentials once at startup
init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))

# Set up standard logging
logger = logging.getLogger("my-app")
logger.setLevel(logging.DEBUG)

# Add jarvis handler
jarvis_handler = JarvisLogHandler(service="my-service")
logger.addHandler(jarvis_handler)

# Also add console handler for local output
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
logger.addHandler(console_handler)

# Use standard logging syntax
logger.info("This goes to jarvis-logs")
logger.error("Error occurred", extra={"request_id": "abc123"})
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_APP_ID` | (none) | App ID for authentication |
| `JARVIS_APP_KEY` | (none) | App key for authentication |
| `JARVIS_LOGS_URL` | `http://localhost:8006` | jarvis-logs server URL |

## Features

- **App-to-app auth**: Secure authentication via jarvis-auth
- **Async batching**: Logs are queued and sent in batches to reduce network overhead
- **Automatic fallback**: Falls back to console if server is unavailable
- **Structured context**: Include arbitrary key-value pairs with each log
- **Thread-safe**: Safe to use from multiple threads
- **Graceful shutdown**: Flushes pending logs on application exit

## Migrating Existing Services

### Step 1: Register your service with jarvis-auth

```bash
# Create app client (run once)
curl -X POST http://localhost:8007/admin/app-clients \
  -H "X-Jarvis-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"app_id": "my-service", "name": "My Service"}'
```

Save the returned `key` - you'll need it for `JARVIS_APP_KEY`.

### Step 2: Add to your service

```python
# In your main.py or __init__.py
import os
from jarvis_log_client import init

init(app_id="my-service", app_key=os.getenv("JARVIS_APP_KEY"))
```

### Step 3: Replace print()/logging

```python
# Before
print(f"Processing request {request_id}")

# After
logger.info("Processing request", request_id=request_id)
```
