"""The word list and association table for Word Link, written for this repo.

Each topic is a clue a bot can give, and its words are what that clue points at. No word is
in two topics, and no topic is a board word, so a topic is always a legal clue.
"""

from __future__ import annotations

TOPICS: dict[str, tuple[str, ...]] = {
    "ocean": ("whale", "ship", "anchor", "coral", "tide", "pearl", "shark", "lighthouse", "wave"),
    "kitchen": ("oven", "spoon", "kettle", "fork", "pan", "recipe", "salt", "butter", "apron"),
    "space": ("rocket", "moon", "comet", "planet", "star", "orbit", "telescope", "meteor"),
    "farm": ("tractor", "barn", "cow", "wheat", "hay", "sheep", "pig", "rooster", "plough"),
    "music": ("piano", "violin", "drum", "song", "choir", "guitar", "trumpet", "melody", "flute"),
    "city": ("taxi", "subway", "bridge", "traffic", "pavement", "office", "museum", "market"),
    "forest": ("oak", "moss", "fox", "owl", "mushroom", "deer", "fern", "pine", "squirrel"),
    "sport": ("goal", "referee", "racket", "stadium", "trophy", "medal", "whistle", "marathon"),
    "school": ("teacher", "chalk", "desk", "exam", "pencil", "library", "homework", "ruler"),
    "weather": ("storm", "thunder", "rain", "snow", "fog", "wind", "frost", "rainbow", "cloud"),
    "medicine": ("doctor", "nurse", "bandage", "pill", "hospital", "needle", "fever", "surgeon"),
    "castle": ("knight", "dragon", "king", "moat", "sword", "crown", "shield", "throne"),
    "travel": ("visa", "suitcase", "ticket", "airport", "hotel", "map", "compass", "luggage"),
    "tools": ("hammer", "saw", "nail", "drill", "wrench", "screw", "ladder", "shovel", "pliers"),
}

WORDS: tuple[str, ...] = tuple(word for words in TOPICS.values() for word in words)
