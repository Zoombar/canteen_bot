from src.reports import OrderLine, allocate_pairs_for_order


def test_pairs_basic() -> None:
    lines = [
        OrderLine(1, "Гречка", "garnish", 2),
        OrderLine(2, "Котлета", "main", 1),
    ]
    p, rem = allocate_pairs_for_order(lines)
    assert p == 1
    assert rem[1] == 1  # one garnish left
    assert rem[2] == 0


def test_pairs_only_other() -> None:
    lines = [OrderLine(1, "Салат", "other", 3)]
    p, rem = allocate_pairs_for_order(lines)
    assert p == 0
    assert rem[1] == 3
