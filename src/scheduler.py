def sm2(ease: float, interval: int, repetitions: int, quality: int) -> tuple[float, int, int]:
    """Standard SM-2. quality: 0-5; <3 = forgot."""
    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * ease)
        repetitions += 1
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return ease, interval, repetitions
