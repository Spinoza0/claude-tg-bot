"""Entry point: python -m claude_tg_bot (or python3 claude_tg_bot/__main__.py)."""

import asyncio

from .client import main

if __name__ == "__main__":
    asyncio.run(main())