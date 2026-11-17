"""Exercise the safe paths of the review subject. The defective paths stay uncalled."""

from tests.conftest import ranges_overlap
from tests.review_subject import (
    allocate,
    clamp,
    covers,
    line_total,
    merge_ranges,
    refund_remaining,
    sum_quantities,
)


def test_sum_of_no_lines_is_zero():
    assert sum_quantities([]) == 0


def test_line_total_multiplies_a_decimal_quantity():
    assert line_total(5, "2") == 10


def test_allocate_fills_requests_that_fit_strictly_inside_stock():
    assert allocate(10, [4, 3]) == [4, 3]


def test_merge_ranges_joins_an_inclusive_touch():
    assert merge_ranges([(1, 3), (3, 5)]) == [(1, 5)]
    assert merge_ranges([(1, 2), (4, 6)]) == [(1, 2), (4, 6)]


def test_covers_an_interior_point():
    assert covers([(1, 5)], 3)
    assert covers([(1, 5)], 1)


def test_clamp_orders_reversed_bounds():
    assert clamp(0, 10, 1) == 1
    assert clamp(7, 1, 10) == 7
    assert clamp(11, 1, 10) == 10


def test_refund_remaining_stops_at_zero():
    assert refund_remaining(10, 4) == 6
    assert refund_remaining(10, 10) == 0
    try:
        refund_remaining(10, -1)
    except ValueError:
        return
    raise AssertionError("negative refund was accepted")


def test_ranges_overlap_is_inclusive():
    assert ranges_overlap((1, 3), (3, 5))
    assert not ranges_overlap((1, 2), (4, 6))
