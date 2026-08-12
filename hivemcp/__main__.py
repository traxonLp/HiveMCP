"""Entry point: ``python -m hivemcp`` / ``hivemcp``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "hivemcp.app:app",
        host=os.getenv("HIVE_HOST", "0.0.0.0"),  # noqa: S104 - container listens on all
        port=int(os.getenv("HIVE_PORT", "8080")),
        log_config=None,  # create_app() owns logging configuration
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
