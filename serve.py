"""Run the hosted app behind a hosting provider's HTTPS reverse proxy."""
import os

import uvicorn


if __name__ == "__main__":
    os.environ.setdefault("PAPERWORK_MODE", "hosted")
    uvicorn.run(
        "app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")),
        workers=1, proxy_headers=True, forwarded_allow_ips="*",
        limit_concurrency=4, access_log=False,
    )
