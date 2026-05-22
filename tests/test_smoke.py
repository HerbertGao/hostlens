"""Smoke tests: verify import + async runtime baseline."""

import asyncio


def test_hostlens_importable() -> None:
    import hostlens

    assert hostlens.__version__ == "0.1.0"


async def test_async_smoke() -> None:
    await asyncio.sleep(0)
    assert True
