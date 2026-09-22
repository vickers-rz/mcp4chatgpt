"""Lightweight async stdio MCP client for downstream MCP servers.

Implements the small MCP client surface currently needed by this gateway
(initialize, notifications/initialized, tools/list, tools/call) over JSON-RPC
2.0 stdio. The repository currently has no official ``mcp`` SDK dependency;
this transport therefore remains deliberately narrow rather than attempting to
reimplement the full MCP client protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_MCP_PROTOCOL_VERSION = "2025-06-18"
# asyncio StreamReader defaults to ~64 KiB per line. MCP stdio transports use
# one JSON-RPC object per line and real tools (network/snapshot/script output)
# can legitimately exceed that, so use a bounded but substantially larger cap.
_MAX_STDIO_LINE_BYTES = 16 * 1024 * 1024


class StdioMCPClientError(Exception):
    """Base error for stdio MCP client operations."""


class StdioMCPClient:
    """Async stdio MCP client that communicates with a child process.

    The client runs its own asyncio event loop on a background daemon thread.
    Public methods are thread-safe and block the calling thread.
    """

    def __init__(
        self,
        *,
        downstream_id: str,
        command: str,
        args: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        startup_timeout: float = 30.0,
        call_timeout: float = 60.0,
    ) -> None:
        self._downstream_id = downstream_id
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env = env
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout

        self._process: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None

        self._request_id_counter = 0
        # Accessed only on the client's private event-loop thread; public calls
        # enter through run_coroutine_threadsafe.
        self._pending: dict[str | int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._tools: list[dict[str, Any]] = []
        self._pid: int | None = None
        self._started_at: float | None = None
        self._closed = False
        self._transport_error: str | None = None

    # ── Properties ──────────────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def transport_error(self) -> str | None:
        return self._transport_error

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._loop is not None
            and not self._loop.is_closed()
            and self._thread is not None
            and self._thread.is_alive()
            and self._transport_error is None
            and not self._closed
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Start the downstream process and complete MCP handshake.

        Blocks until the process is initialized and tools are discovered,
        or raises StdioMCPClientError on failure.
        """
        if self._thread is not None:
            raise StdioMCPClientError("Client already started")

        self._ready.clear()
        self._start_error = None
        self._transport_error = None
        self._closed = False

        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"downstream-{self._downstream_id}",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(timeout=self._startup_timeout + 2):
            self._start_error = StdioMCPClientError("Downstream startup wait timed out")
            self._cleanup_sync()
            raise StdioMCPClientError(
                f"Downstream {self._downstream_id!r} failed to start "
                f"within {self._startup_timeout}s"
            )

        if self._start_error is not None:
            err = self._start_error
            self._cleanup_sync()
            raise StdioMCPClientError(
                f"Downstream {self._downstream_id!r} startup failed: {err}"
            ) from err

    def stop(self, timeout: float = 10.0) -> None:
        """Gracefully stop the downstream process."""
        if self._loop is not None and not self._loop.is_closed():
            fut = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
            try:
                fut.result(timeout=timeout)
            except Exception as exc:
                log.warning(
                    "downstream %s: graceful stop did not complete: %s",
                    self._downstream_id, exc,
                )
        else:
            self._closed = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self._cleanup_sync()
                if self._thread is not None and self._thread.is_alive():
                    raise StdioMCPClientError(
                        f"Downstream {self._downstream_id!r} shutdown timed out"
                    )
            else:
                self._thread = None

    def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Call a tool on the downstream MCP server. Thread-safe."""
        if not self.is_running:
            raise StdioMCPClientError(
                f"Downstream {self._downstream_id!r} is not running"
            )
        t = self._call_timeout if timeout is None else timeout
        fut = asyncio.run_coroutine_threadsafe(
            self._async_call_tool(tool_name, arguments or {}, t),
            self._loop,
        )
        try:
            return fut.result(timeout=max(t + 0.25, 1.0))
        except Exception as exc:
            fut.cancel()
            raise StdioMCPClientError(
                f"Tool call {tool_name!r} on {self._downstream_id!r} failed: {exc}"
            ) from exc

    # ── Event loop thread ──────────────────────────────────────────

    def _run_loop(self) -> None:
        """Background thread: run the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_start())
        except asyncio.CancelledError:
            # A cancelled reader is the normal stop path after readiness. During
            # startup it is an error and the child must be torn down.
            if not self._ready.is_set():
                self._start_error = StdioMCPClientError("Downstream startup cancelled")
                self._closed = True
                if self._process is not None and self._process.returncode is None:
                    try:
                        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            self._process.kill()
                        except Exception:
                            pass
        except Exception as exc:
            self._start_error = exc
            # Startup can fail after the child has already been spawned (for
            # example initialize/tools-list errors).  Kill that process group
            # here; the public start() cleanup runs from another thread and may
            # otherwise arrive after this loop has already closed.
            self._closed = True
            if self._process is not None and self._process.returncode is None:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                # Reaping is completed below once run_until_complete unwinds.
        finally:
            # Ensure a failed/unusable child is reaped before closing the loop.
            try:
                if self._process is not None and self._closed:
                    try:
                        self._loop.run_until_complete(
                            asyncio.wait_for(self._process.wait(), timeout=2.0)
                        )
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            try:
                                self._process.kill()
                            except Exception:
                                pass
                        self._loop.run_until_complete(self._process.wait())
            except Exception:
                pass
            # Ensure auxiliary tasks (notably stderr draining) do not survive
            # event-loop teardown and produce "Task was destroyed" warnings.
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            # Publish startup failure only after child cleanup/reaping is done.
            if self._start_error is not None and not self._ready.is_set():
                self._ready.set()
            self._loop.close()
            self._loop = None

    async def _async_start(self) -> None:
        """Spawn process, handshake, discover tools, then run reader."""
        # Build environment
        child_env = os.environ.copy()
        if self._env:
            child_env.update(self._env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_STDIO_LINE_BYTES,
                cwd=self._cwd or None,
                env=child_env,
                start_new_session=True,  # prevent orphans
            )
        except FileNotFoundError as exc:
            raise StdioMCPClientError(
                f"Command not found: {self._command}"
            ) from exc
        except Exception as exc:
            raise StdioMCPClientError(
                f"Failed to spawn {self._command}: {exc}"
            ) from exc

        self._pid = self._process.pid
        self._started_at = time.time()
        log.info(
            "downstream %s: spawned pid=%d cmd=%s",
            self._downstream_id, self._pid, self._command,
        )

        # Start stdout/stderr readers before the first request.  JSON-RPC
        # requests complete only when the stdout reader dispatches responses,
        # so initialization would deadlock if the reader started afterwards.
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # MCP initialize handshake
        try:
            init_result = await asyncio.wait_for(
                self._send_request("initialize", {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "mcp4chatgpt-downstream",
                        "version": "0.3.0",
                    },
                }),
                timeout=self._startup_timeout,
            )
        except asyncio.TimeoutError:
            raise StdioMCPClientError(
                f"Downstream {self._downstream_id!r} initialize timed out"
            )

        negotiated_version = init_result.get("protocolVersion")
        if not isinstance(negotiated_version, str) or not negotiated_version:
            raise StdioMCPClientError("initialize response missing protocolVersion")
        self._server_info = init_result.get("serverInfo", {})
        self._server_capabilities = init_result.get("capabilities", {})

        # Send initialized notification. MCP notifications omit params when
        # there is no payload; this matches current SDK/server behaviour.
        await self._send_notification("notifications/initialized", None)

        log.info(
            "downstream %s: initialized server=%s",
            self._downstream_id,
            self._server_info.get("name", "unknown"),
        )

        # Discover tools
        try:
            tools_result = await asyncio.wait_for(
                self._send_request("tools/list", {}),
                timeout=self._startup_timeout,
            )
        except asyncio.TimeoutError:
            raise StdioMCPClientError(
                f"Downstream {self._downstream_id!r} tools/list timed out"
            )

        self._tools = tools_result.get("tools", [])
        log.info(
            "downstream %s: discovered %d tools",
            self._downstream_id, len(self._tools),
        )

        # Signal ready to the calling thread, then keep this loop alive for the
        # lifetime of the reader task.  Public tool calls are scheduled onto
        # this same event loop from other threads.
        self._ready.set()
        assert self._reader_task is not None
        await self._reader_task

    def _mark_transport_failed(self, message: str) -> None:
        """Mark an otherwise-live child unusable and begin group teardown."""
        if self._closed:
            return
        self._transport_error = str(message)[:1000]
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.terminate()
                except Exception:
                    pass

    async def _reader_loop(self) -> None:
        """Read stdout lines and dispatch JSON-RPC responses."""
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while not self._closed and self._process.returncode is None:
                try:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if not line:
                    if not self._closed:
                        self._mark_transport_failed("downstream stdout closed unexpectedly")
                    break  # EOF

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    log.debug(
                        "downstream %s: non-JSON line: %s",
                        self._downstream_id, line_str[:200],
                    )
                    continue

                self._dispatch_message(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._mark_transport_failed(f"reader error: {exc}")
            log.warning(
                "downstream %s: reader error: %s",
                self._downstream_id, exc,
            )
        finally:
            # Fail all pending futures
            for req_id, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(
                        StdioMCPClientError("Downstream process ended")
                    )
            self._pending.clear()

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Route a JSON-RPC message to the appropriate pending future."""
        if "id" not in msg:
            if "method" in msg:
                log.debug(
                    "downstream %s: server notification method=%s",
                    self._downstream_id, msg.get("method"),
                )
            return
        if "method" in msg:
            # This Phase-1 transport intentionally supports downstream tools,
            # not server-initiated sampling/elicitation/roots requests. Return a
            # protocol error instead of leaving the downstream request hanging.
            asyncio.create_task(self._send_server_error(
                msg["id"], -32601, f"Unsupported server request: {msg.get('method')}"
            ))
            return

        req_id = msg["id"]
        fut = self._pending.pop(req_id, None)
        if fut is None:
            log.debug(
                "downstream %s: unexpected response id=%s",
                self._downstream_id, req_id,
            )
            return

        if "error" in msg:
            err = msg["error"]
            fut.set_exception(StdioMCPClientError(
                f"JSON-RPC error {err.get('code', '?')}: "
                f"{err.get('message', 'unknown')}"
            ))
        else:
            fut.set_result(msg.get("result", {}))

    async def _drain_stderr(self) -> None:
        """Read and log stderr from the child process."""
        assert self._process is not None
        assert self._process.stderr is not None
        try:
            while self._process.returncode is None:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.debug(
                        "downstream %s stderr: %s",
                        self._downstream_id, text[:500],
                    )
        except Exception:
            pass

    # ── JSON-RPC helpers ───────────────────────────────────────────

    async def _send_request(
        self, method: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response."""
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._loop is not None

        self._request_id_counter += 1
        req_id = self._request_id_counter

        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }

        fut: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[req_id] = fut

        payload = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception as exc:
            self._pending.pop(req_id, None)
            raise StdioMCPClientError(
                f"Failed to write to downstream stdin: {exc}"
            ) from exc

        try:
            return await fut
        finally:
            # _dispatch_message normally removes completed requests. This also
            # clears timed-out/cancelled requests so late responses are ignored
            # instead of leaking pending futures.
            self._pending.pop(req_id, None)

    async def _send_server_error(
        self, request_id: str | int, code: int, message: str,
    ) -> None:
        """Reply to an unsupported server-initiated JSON-RPC request."""
        if self._process is None or self._process.stdin is None:
            return
        payload = json.dumps({
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception:
            pass

    async def _send_notification(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        assert self._process is not None
        assert self._process.stdin is not None

        msg: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        payload = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception:
            pass  # notifications are fire-and-forget

    async def _async_call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float,
    ) -> dict[str, Any]:
        """Forward a tools/call request to the downstream server."""
        return await asyncio.wait_for(
            self._send_request("tools/call", {
                "name": name,
                "arguments": arguments,
            }),
            timeout=timeout,
        )

    # ── Shutdown ───────────────────────────────────────────────────

    async def _async_stop(self) -> None:
        """Gracefully stop the downstream process."""
        self._closed = True

        if self._process is None or self._process.returncode is not None:
            if self._reader_task is not None and not self._reader_task.done():
                self._reader_task.cancel()
            if self._stderr_task is not None and not self._stderr_task.done():
                self._stderr_task.cancel()
            return

        pid = self._process.pid
        log.info("downstream %s: stopping pid=%d", self._downstream_id, pid)

        # Close stdin first. Well-behaved stdio MCP servers treat EOF as a
        # graceful shutdown signal; giving them a brief window to exit lets
        # managed child processes (notably persistent Chrome profiles) flush
        # state before we terminate the whole process group.
        try:
            if self._process.stdin and not self._process.stdin.is_closing():
                self._process.stdin.close()
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            # The server did not honor EOF promptly. Fall back to the bounded
            # process-group termination path so orphan prevention is preserved.
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    return

            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=3.5,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "downstream %s: pid=%d did not exit, sending SIGKILL",
                    self._downstream_id, pid,
                )
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(
                        self._process.wait(), timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    pass

        if self._reader_task is not None and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()
        if self._stderr_task is not None and not self._stderr_task.done():
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()

        log.info(
            "downstream %s: stopped pid=%d rc=%s",
            self._downstream_id, pid,
            self._process.returncode,
        )

    def _cleanup_sync(self) -> None:
        """Synchronous cleanup fallback for failed starts."""
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except Exception:
                    pass
        if (
            self._loop is not None
            and not self._loop.is_closed()
            and self._reader_task is not None
            and not self._reader_task.done()
        ):
            self._loop.call_soon_threadsafe(self._reader_task.cancel)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if not thread.is_alive():
                self._thread = None
