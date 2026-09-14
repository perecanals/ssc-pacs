"""Release download resources even if sending the response fails."""

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response


class DownloadResponse(Response):
    def __init__(self, response: Response, cleanup):
        self.response = response
        self.cleanup = cleanup
        self.status_code = response.status_code
        self.media_type = response.media_type
        self.raw_headers = response.raw_headers
        self.background = response.background

    async def __call__(self, scope, receive, send):
        self.response.background = self.background
        try:
            await self.response(scope, receive, send)
        finally:
            # A disconnected client may cancel the response task. Cleanup must
            # still close files, remove temporary data and release download slots.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.cleanup)
