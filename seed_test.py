"""Seed 10 idioms directly into the DB for testing."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from src import config, db

IDIOMS = [
    ("break the ice", "to do or say something to relieve tension in an awkward social situation"),
    ("bite the bullet", "to endure a painful or difficult situation with courage"),
    ("spill the beans", "to reveal secret information accidentally or indiscreetly"),
    ("hit the sack", "to go to bed"),
    ("under the weather", "feeling ill or unwell"),
    ("cost an arm and a leg", "to be very expensive"),
    ("bite off more than you can chew", "to take on more responsibility than you can handle"),
    ("the best of both worlds", "to enjoy two different opportunities at the same time"),
    ("once in a blue moon", "something that happens very rarely"),
    ("steal someone's thunder", "to upstage someone by doing or saying what they had planned"),
]

db.init(config.DB_PATH)
with db.connect(config.DB_PATH) as conn:
    for phrase, meaning in IDIOMS:
        result = db.add_idiom(conn, phrase, meaning, None, "test_seed")
        status = "added" if result else "exists"
        print(f"  {status}: {phrase}")

print(f"\nDone. Run: python -m src.main fill-examples")
