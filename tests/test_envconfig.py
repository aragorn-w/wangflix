"""`media_stack.envconfig`: shared, bounded env-var parsing."""

import pytest

from media_stack.envconfig import env_float, env_int


# --- env_float --------------------------------------------------------

@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-5", "abc",
                                 "1e308", "100.5"], ids=lambda v: f"reject-{v}")  # 1e308: finite but huge; 100.5: just over the limit
def test_env_float_rejects_non_finite_non_positive_unparseable_and_over_limit(bad):
    seen = []
    assert env_float({"X": bad}, "X", 7.0, 100.0, log=seen.append) == 7.0
    assert seen and "X" in seen[0]


@pytest.mark.parametrize("value, expected", [("12.5", 12.5), ("100.0", 100.0)],
                         ids=["valid-positive", "exactly-at-the-limit"])
def test_env_float_accepts_valid_values(value, expected):
    assert env_float({"X": value}, "X", 7.0, 100.0) == expected


def test_env_float_default_log_is_print(capsys):
    """The default `log=print` still gives a visible warning."""
    env_float({"X": "nan"}, "X", 7.0, 100.0)
    assert "X" in capsys.readouterr().out


# --- env_int ------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan", "65"], ids=lambda v: f"reject-{v}")  # 65: over the limit
def test_env_int_rejects_non_positive_unparseable_and_over_limit(bad):
    seen = []
    assert env_int({"X": bad}, "X", 6, 64, log=seen.append) == 6
    assert seen and "X" in seen[0]


@pytest.mark.parametrize("value, expected", [("3", 3), ("64", 64)], ids=["valid-positive", "exactly-at-the-limit"])
def test_env_int_accepts_valid_values(value, expected):
    assert env_int({"X": value}, "X", 6, 64) == expected


@pytest.mark.parametrize("parse, default, bound", [(env_float, 7.0, 100.0), (env_int, 6, 64)],
                         ids=["env_float", "env_int"])
def test_empty_or_missing_is_the_default_with_no_warning(parse, default, bound):
    seen = []
    assert parse({}, "X", default, bound, log=seen.append) == default
    assert parse({"X": ""}, "X", default, bound, log=seen.append) == default
    assert seen == []  # absence isn't a bad value
