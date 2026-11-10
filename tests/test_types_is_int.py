from arena.types import is_int


def test_is_int_rejects_bools_and_non_ints() -> None:
    assert is_int(0) and is_int(-3)
    assert not is_int(True) and not is_int(False)
    assert not is_int(1.0) and not is_int("1") and not is_int(None)
