from tools.run_coremark_jit import valid

CRCS = """seedcrc          : 0xe9f5
[0]crclist       : 0xe714
[0]crcmatrix     : 0x1fd7
[0]crcstate      : 0x8e3a
"""
SLOW = "ERROR! Must execute for at least 10 secs for a valid result!\n"


def test_validated() -> None:
    assert valid(CRCS + "Correct operation validated. See README.md\n")


def test_only_the_ten_seconds_rule() -> None:
    assert valid(CRCS + SLOW + "Errors detected\n")


def test_crc_mismatch() -> None:
    assert not valid(CRCS.replace("0x8e3a", "0x1234") + SLOW + "Errors detected\n")
    assert not valid(CRCS + SLOW + "[0]ERROR! list crc 0x0000 - should be 0xe714\n")


def test_no_crc_lines() -> None:
    assert not valid(SLOW + "Errors detected\n")
