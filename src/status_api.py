"""Optional HTTP API that serves the contents of status.json at ``GET /status``."""

import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from status_store import read_status


def create_app(status_file: Path) -> FastAPI:
    """Create the FastAPI application.

    Args:
        status_file: Path to the status file to serve.

    Returns:
        The configured FastAPI app.
    """
    app = FastAPI(title="WAN Failover Check")

    @app.get("/status")
    def get_status() -> JSONResponse:
        """Return the current WAN status, or HTTP 503 if it is unavailable."""
        try:
            return JSONResponse(content=read_status(status_file))
        except (RuntimeError, TypeError) as e:
            return JSONResponse(status_code=503, content={"error": str(e)})

    return app


class StatusAPIServer:
    """Runs the status API under uvicorn in a managed thread and stops it cleanly."""

    def __init__(self, status_file: Path, host: str, port: int) -> None:
        """Create the server.

        Args:
            status_file: Path to the status file to serve.
            host: Bind address.
            port: TCP port.
        """
        self.status_file = status_file
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        """The status endpoint URL."""
        return f"http://{self.host}:{self.port}/status"

    def serve(self, stop_event: threading.Event) -> None:
        """Serve requests until stop_event is set. Blocks.

        Args:
            stop_event: Set to request shutdown.

        Raises:
            RuntimeError: If uvicorn exits before a shutdown was requested (for example the port is
                already in use), so the ThreadManager treats it as a crash and can restart it.
        """
        config = uvicorn.Config(create_app(self.status_file), host=self.host, port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        with self._lock:
            self._server = server

        def _watch_for_stop() -> None:
            stop_event.wait()
            server.should_exit = True

        threading.Thread(target=_watch_for_stop, name="status api stop watcher", daemon=True).start()

        try:
            # Not on the main thread, so uvicorn leaves signal handling to main().
            server.run()
        except SystemExit as e:
            # uvicorn calls sys.exit(1) when it cannot bind.
            msg = f"Status API on {self.host}:{self.port} failed to start."
            raise RuntimeError(msg) from e
        finally:
            with self._lock:
                self._server = None

        if not stop_event.is_set():
            msg = "Status API stopped unexpectedly."
            raise RuntimeError(msg)

    def stop(self) -> None:
        """Ask uvicorn to shut down. Called by ThreadManager.stop_all()."""
        with self._lock:
            if self._server is not None:
                self._server.should_exit = True
