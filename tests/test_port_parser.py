"""
parse_port is the one strict port parser used everywhere a port is
interpreted: blank (None or an empty/whitespace-only string) means the
default SFTP port, 22. Anything else must be a plain whole number from 1 to
65535 with no surrounding whitespace, sign, or decimal point, or it is
refused via InvalidPort - it must never silently fall back to 22 for a
non-blank value.
"""
import pytest

from simple_sftp_client import InvalidPort, parse_port


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_means_default_port(value):
    assert parse_port(value) == 22


@pytest.mark.parametrize("value,expected", [
    (22, 22),
    ("22", 22),
    (1, 1),
    ("1", 1),
    (65535, 65535),
    ("65535", 65535),
    (2222, 2222),
    ("2222", 2222),
])
def test_valid_ports_accepted(value, expected):
    assert parse_port(value) == expected


@pytest.mark.parametrize("value", [
    0, "0",
    65536, "65536",
    -1, "-1",
    "abc",
    "22 ",      # trailing whitespace: refused, not treated as blank or 22
    " 22",      # leading whitespace
    "1.5", 1.5,
])
def test_invalid_nonblank_ports_are_refused(value):
    with pytest.raises(InvalidPort):
        parse_port(value)


@pytest.mark.parametrize("value", [
    "²",       # superscript two: isdigit() true, int() rejects
    "⁴",       # superscript four
    "٠١",  # Arabic-Indic digits
])
def test_nonascii_digits_refused_not_leaked(value):
    # str.isdigit() accepts Unicode digits that int() cannot parse, which
    # would leak a raw ValueError past every InvalidPort guard. They must be
    # refused as InvalidPort like any other non-numeric text.
    with pytest.raises(InvalidPort):
        parse_port(value)


@pytest.mark.parametrize("value", [True, False])
def test_bool_is_never_treated_as_a_port_number(value):
    # bool is a subclass of int in Python; without an explicit check, True/
    # False would silently parse as port 1/0.
    with pytest.raises(InvalidPort):
        parse_port(value)


@pytest.mark.parametrize("value", [[], {}, object()])
def test_unsupported_types_are_refused(value):
    with pytest.raises(InvalidPort):
        parse_port(value)
