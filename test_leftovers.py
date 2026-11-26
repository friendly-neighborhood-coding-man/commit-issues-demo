from leftovers import divide_leftovers


def test_divide_leftovers():
    assert divide_leftovers(["Joy", "Anger"]) == ["pie", "pie"]
    assert divide_leftovers([]) == []


if __name__ == "__main__":
    test_divide_leftovers()
