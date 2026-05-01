import os
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import db
from src.quiz import Question, build_question


def _seed_db(conn, phrases):
    for phrase in phrases:
        db.add_idiom(conn, phrase, f"meaning of {phrase}", f"He had to {phrase} at the party.", None)


def make_conn(n_idioms=6):
    tmp = tempfile.mktemp(suffix=".db")
    db.init(tmp)
    with db.connect(tmp) as conn:
        phrases = [f"idiom number {i}" for i in range(n_idioms)]
        _seed_db(conn, phrases)
    return tmp


def test_question_has_4_options():
    tmp = make_conn()
    with db.connect(tmp) as conn:
        row = conn.execute("SELECT * FROM idioms LIMIT 1").fetchone()
        q = build_question(conn, row)
    assert len(q.options) == 4


def test_correct_option_is_present():
    tmp = make_conn()
    with db.connect(tmp) as conn:
        row = conn.execute("SELECT * FROM idioms LIMIT 1").fetchone()
        q = build_question(conn, row)
    assert q.options[q.correct_index] == row["phrase"]


def test_options_are_unique():
    tmp = make_conn()
    with db.connect(tmp) as conn:
        row = conn.execute("SELECT * FROM idioms LIMIT 1").fetchone()
        q = build_question(conn, row)
    assert len(set(q.options)) == 4


def test_stem_contains_blank():
    tmp = make_conn()
    with db.connect(tmp) as conn:
        row = conn.execute("SELECT * FROM idioms LIMIT 1").fetchone()
        q = build_question(conn, row)
    assert "___" in q.stem
