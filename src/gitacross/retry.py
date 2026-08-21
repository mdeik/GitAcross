import logging
import time

logger = logging.getLogger(__name__)


def retry(fn, max_attempts=3, backoff_seconds=2, exceptions=(Exception,)):
    """Run fn with exponential backoff. Re-raises the last exception on final failure."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except exceptions as e:
            if attempt == max_attempts - 1:
                raise
            delay = backoff_seconds * (2**attempt)
            logger.warning(
                "Attempt %d/%d failed: %s. Retrying in %.1fs...",
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            time.sleep(delay)
