from app.constants import WORKER_COUNT, WORKER_COUNT_MAX


def worker_target(sizes):
    """Decide the worker-pool size for a batch from its file sizes (bytes).
    Returns WORKER_COUNT_MAX when the batch has enough small files to make
    extra channels pay off, else the WORKER_COUNT default. Sizes below zero
    are unknown and never count as small."""
    small = sum(1 for s in sizes if 0 <= s < 1024 * 1024)
    return WORKER_COUNT_MAX if small >= 8 else WORKER_COUNT

