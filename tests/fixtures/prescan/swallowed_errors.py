"""Fixture for prescan tests — swallowed errors, long functions, TODO markers, commented code."""


def process():
    try:
        do_something()  # noqa: F821 — intentional undefined for fixture
    except:  # noqa: E722 -- bare except for test
        pass


def careful_handler():
    try:
        do_something()  # noqa: F821 — intentional undefined for fixture
    except ValueError:
        pass  # P-ERR: swallowed error (specific exception)


def long_function():
    # This function has >50 lines to trigger P-LEN
    x = 1
    x += 1
    x += 2
    x += 3
    x += 4
    x += 5
    x += 6
    x += 7
    x += 8
    x += 9
    x += 10
    x += 11
    x += 12
    x += 13
    x += 14
    x += 15
    x += 16
    x += 17
    x += 18
    x += 19
    x += 20
    x += 21
    x += 22
    x += 23
    x += 24
    x += 25
    x += 26
    x += 27
    x += 28
    x += 29
    x += 30
    x += 31
    x += 32
    x += 33
    x += 34
    x += 35
    x += 36
    x += 37
    x += 38
    x += 39
    x += 40
    x += 41
    x += 42
    x += 43
    x += 44
    x += 45
    x += 46
    x += 47
    x += 48
    x += 49
    x += 50
    return x


def short_function():
    return 42


# TODO: fix this later
# FIXME: this is broken
# XXX: hack alert

# def old_function():
#     return None

# for item in collection:
#     process(item)


def unused_helper():
    """This function is never called in this file."""
    return "unused"


def caller():
    """This calls short_function."""
    return short_function()
