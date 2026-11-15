import pytest

from lifeline.rng import Rng


def draws(rng: Rng, n: int = 20) -> list[float]:
    return [rng.random() for _ in range(n)]


def test_same_seed_gives_same_sequence():
    assert draws(Rng(7)) == draws(Rng(7))


def test_different_seeds_give_different_sequences():
    assert draws(Rng(7)) != draws(Rng(8))


def test_random_is_in_unit_interval():
    rng = Rng(1)
    assert all(0.0 <= x < 1.0 for x in draws(rng, 1000))


def test_uniform_and_randint_stay_in_bounds():
    rng = Rng(2)
    for _ in range(500):
        assert 1.5 <= rng.uniform(1.5, 2.5) <= 2.5
        assert 3 <= rng.randint(3, 7) <= 7


def test_choice_and_shuffle_are_deterministic():
    a, b = Rng(9), Rng(9)
    items = list(range(10))
    assert a.choice(items) == b.choice(items)
    xs, ys = list(range(10)), list(range(10))
    a.shuffle(xs)
    b.shuffle(ys)
    assert xs == ys
    assert sorted(xs) == list(range(10))


def test_different_child_names_give_different_streams():
    root = Rng(42)
    assert draws(root.child("n01")) != draws(root.child("n02"))


def test_same_child_name_gives_same_stream():
    assert draws(Rng(42).child("n01")) == draws(Rng(42).child("n01"))


def test_child_output_is_unaffected_by_parent_draws():
    fresh = Rng(42)
    expected = draws(fresh.child("n01"))

    used = Rng(42)
    draws(used, 1000)
    used.shuffle(list(range(50)))
    assert draws(used.child("n01")) == expected


def test_child_is_unaffected_by_creation_order_of_siblings():
    root_a = Rng(5)
    a1 = draws(root_a.child("x"))
    _ = root_a.child("y")

    root_b = Rng(5)
    _ = root_b.child("y")
    b1 = draws(root_b.child("x"))
    assert a1 == b1


def test_child_of_child_is_deterministic():
    assert draws(Rng(3).child("a").child("b")) == draws(Rng(3).child("a").child("b"))


def test_invalid_seed_rejected():
    with pytest.raises(ValueError):
        Rng(-1)
    with pytest.raises(ValueError):
        Rng(2**64)
    with pytest.raises(ValueError):
        Rng(1.5)  # type: ignore[arg-type]