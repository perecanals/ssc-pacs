"""Response cleanup must run when a client disconnects before or during transfer."""

import asyncio

import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, StreamingResponse

from download_response import DownloadResponse


@pytest.mark.parametrize("kind", ["file", "stream"])
@pytest.mark.parametrize("failure", ["http.response.start", "http.response.body", "cancel", None])
def test_cleanup_on_response_completion_failure_and_cancellation(tmp_path, kind, failure):
    path = tmp_path / "download"
    path.write_bytes(b"test payload")
    handle = path.open("rb")
    response = FileResponse(path) if kind == "file" else StreamingResponse(iter(lambda: handle.read(4), b""))
    cleaned = []

    def cleanup():
        handle.close()
        path.unlink()
        cleaned.append(True)

    managed = DownloadResponse(response, cleanup)

    async def send(message):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if message["type"] == failure:
            raise BrokenPipeError("Client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    async def transfer():
        scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}
        await managed(scope, receive, send)

    if failure:
        with pytest.raises((BrokenPipeError, ClientDisconnect, asyncio.CancelledError)):
            asyncio.run(transfer())
    else:
        asyncio.run(transfer())
    assert cleaned == [True]
    assert handle.closed
    assert not path.exists()
