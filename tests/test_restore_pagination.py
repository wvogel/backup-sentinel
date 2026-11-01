"""Pure-function tests for the restore runs-log pagination helpers."""

from app.web.routes_restore import _RUNS_PER_PAGE_DEFAULT, _clamp_per_page, _page_window


def test_clamp_per_page_accepts_allowed_values():
    assert _clamp_per_page("50", _RUNS_PER_PAGE_DEFAULT) == 50
    assert _clamp_per_page("200", _RUNS_PER_PAGE_DEFAULT) == 200


def test_clamp_per_page_rejects_junk_and_out_of_set():
    assert _clamp_per_page(None, 25) == 25
    assert _clamp_per_page("", 25) == 25
    assert _clamp_per_page("abc", 25) == 25
    assert _clamp_per_page("17", 25) == 25  # not an offered size
    assert _clamp_per_page("999999", 25) == 25


def test_page_window_single_page():
    assert _page_window(1, 1) == [1]


def test_page_window_no_gaps_when_small():
    assert _page_window(3, 5) == [1, 2, 3, 4, 5]


def test_page_window_gaps_both_sides():
    # current 10 of 20 → first, …, 8 9 10 11 12, …, last
    assert _page_window(10, 20) == [1, None, 8, 9, 10, 11, 12, None, 20]


def test_page_window_gap_only_on_the_right_near_start():
    assert _page_window(2, 20) == [1, 2, 3, 4, None, 20]


def test_page_window_gap_only_on_the_left_near_end():
    assert _page_window(19, 20) == [1, None, 17, 18, 19, 20]
