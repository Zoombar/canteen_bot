from src.menu_parse import _parse_line, classify_dish


def test_parse_line_simple() -> None:
    assert _parse_line("Borsch 120") == ("Borsch", 120.0)
    assert _parse_line("Cutlet Kiev-style 150.50") == ("Cutlet Kiev-style", 150.5)
    assert _parse_line("Борщ 50-00") == ("Борщ", 50.0)
    assert _parse_line("Плов 100-5") == ("Плов", 100.5)


def test_classify_garnish_main() -> None:
    assert classify_dish("Гречка с маслом") in ("garnish", "main", "other")
    assert classify_dish("Котлета куриная") == "main"
