"""Fixture for prescan tests — stubs, implemented functions, secrets."""


def implemented():
    x = compute()
    y = transform(x)
    z = validate(y)
    return z + 1


def stub_function():
    pass


def todo_stub():
    raise NotImplementedError


def another_stub():
    raise NotImplementedError("not yet done")


def compute():
    return 42


def transform(val):
    return val * 2


def validate(val):
    if val > 0:
        return val
    return 0
