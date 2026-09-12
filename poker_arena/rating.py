from __future__ import annotations

from openskill.models import PlackettLuce

MODEL = PlackettLuce()

Rating = tuple[float, float]


def rate(a: Rating, b: Rating, score: int) -> tuple[Rating, Rating]:
    """`score` is chips won by `a`: positive is a win for a, zero a draw."""
    ranks = [0, 0] if score == 0 else [0, 1] if score > 0 else [1, 0]
    [ra], [rb] = MODEL.rate(
        [[MODEL.rating(mu=a[0], sigma=a[1])], [MODEL.rating(mu=b[0], sigma=b[1])]],
        ranks=ranks,
    )
    return (ra.mu, ra.sigma), (rb.mu, rb.sigma)
