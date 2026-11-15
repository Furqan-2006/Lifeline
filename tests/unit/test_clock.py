import asyncio

import pytest

from lifeline.clock import FakeClock, SystemClock


async def test_fake_clock_starts_at_given_time():
    assert FakeClock(start=5.5).now() == 5.5


async def test_sleepers_wake_in_due_order_and_never_early():
    clock = FakeClock()
    woke: list[tuple[str, float]] = []

    async def sleeper(name: str, delay: float) -> None:
        await clock.sleep(delay)
        woke.append((name, clock.now()))

    tasks = [
        asyncio.create_task(sleeper("c", 3.0)),
        asyncio.create_task(sleeper("a", 1.0)),
        asyncio.create_task(sleeper("b", 2.0)),
    ]
    await asyncio.sleep(0)

    await clock.advance(0.5)
    assert woke == []

    await clock.advance(1.0)  # now at 1.5
    assert woke == [] or woke[0][0] == "a"
    assert all(t >= 1.0 for _, t in woke)

    await clock.advance(10.0)
    assert [name for name, _ in woke] == ["a", "b", "c"]
    assert [t for _, t in woke] == [1.0, 2.0, 3.0]
    await asyncio.gather(*tasks)


async def test_sleeper_does_not_wake_before_due_time():
    clock = FakeClock()
    done = False

    async def sleeper() -> None:
        nonlocal done
        await clock.sleep(2.0)
        done = True

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    await clock.advance(1.999)
    assert not done
    await clock.advance(0.001)
    await task
    assert done


async def test_ties_wake_in_creation_order():
    clock = FakeClock()
    order: list[int] = []

    async def sleeper(i: int) -> None:
        await clock.sleep(1.0)
        order.append(i)

    tasks = [asyncio.create_task(sleeper(i)) for i in range(5)]
    await asyncio.sleep(0)
    await clock.advance(1.0)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3, 4]


async def test_loop_sleeping_task_advances_across_one_advance_call():
    clock = FakeClock()
    ticks: list[float] = []

    async def ticker() -> None:
        for _ in range(4):
            await clock.sleep(1.0)
            ticks.append(clock.now())

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    await clock.advance(10.0)
    await task
    assert ticks == [1.0, 2.0, 3.0, 4.0]
    assert clock.now() == 10.0


async def test_sleep_scheduled_by_woken_task_is_honoured_within_same_advance():
    clock = FakeClock()
    seen: list[float] = []

    async def chain() -> None:
        await clock.sleep(1.0)
        seen.append(clock.now())
        await clock.sleep(1.0)
        seen.append(clock.now())

    task = asyncio.create_task(chain())
    await asyncio.sleep(0)
    await clock.advance(5.0)
    await task
    assert seen == [1.0, 2.0]


async def test_advance_rejects_negative_dt():
    with pytest.raises(ValueError):
        await FakeClock().advance(-1.0)


async def test_sleep_rejects_negative_duration():
    with pytest.raises(ValueError):
        await FakeClock().sleep(-0.1)


async def test_zero_sleep_yields_without_advancing_time():
    clock = FakeClock(start=3.0)
    await clock.sleep(0)
    assert clock.now() == 3.0


async def test_advance_zero_is_allowed_and_does_not_move_time():
    clock = FakeClock(start=1.0)
    await clock.advance(0.0)
    assert clock.now() == 1.0


def test_system_clock_is_monotonic_and_sleeps():
    clock = SystemClock()
    first = clock.now()
    second = clock.now()
    assert second >= first
    asyncio.run(clock.sleep(0))