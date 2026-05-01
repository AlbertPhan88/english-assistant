import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scheduler import sm2


def test_forgot_resets():
    ease, interval, reps = sm2(2.5, 10, 3, quality=2)
    assert reps == 0
    assert interval == 1


def test_correct_grows_interval():
    ease, interval, reps = sm2(2.5, 0, 0, quality=5)
    assert interval == 1 and reps == 1

    ease, interval, reps = sm2(ease, interval, reps, quality=5)
    assert interval == 6 and reps == 2

    ease, interval, reps = sm2(ease, interval, reps, quality=5)
    assert interval > 6 and reps == 3


def test_ease_floor():
    ease = 1.3
    for _ in range(10):
        ease, _, _ = sm2(ease, 1, 1, quality=0)
    assert ease >= 1.3


def test_correct_improves_ease():
    ease0 = 1.5
    ease, _, _ = sm2(ease0, 1, 1, quality=5)
    assert ease > ease0
