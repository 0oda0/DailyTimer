import logging
import os

import uvicorn

from .web.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("DAILYTIMER_HOST", "127.0.0.1"),
        port=int(os.environ.get("DAILYTIMER_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
