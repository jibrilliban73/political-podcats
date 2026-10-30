"""Paced Spotify requests that respect Retry-After without repeating lost writes."""
import json
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_last_request = 0.0
MIN_INTERVAL = 1.0
MAX_RETRY_WAIT = 180


def retry_delay(value):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        try:
            return max(1, int(parsedate_to_datetime(value).timestamp() - time.time()) + 1)
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 30


def send_json(request, label, error_type):
    global _last_request
    for attempt in range(4):
        pause = MIN_INTERVAL - (time.monotonic() - _last_request)
        if pause > 0:
            time.sleep(pause)
        _last_request = time.monotonic()
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if exc.code == 429:
                wait = retry_delay(exc.headers.get("Retry-After"))
                if attempt == 3 or wait > MAX_RETRY_WAIT:
                    raise error_type(
                        f"Spotify rate/quota limit during {label}; Retry-After={wait}s. "
                        "Stopped safely; the next scheduled run will retry."
                    ) from None
                print(f"Spotify rate limit during {label}; waiting {wait}s before retry.", flush=True)
                time.sleep(wait)
                continue
            if exc.code >= 500 and request.get_method() == "GET" and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            hint = " Check authorization and playlist permissions." if exc.code in (401, 403) else ""
            raise error_type(f"Spotify HTTP {exc.code} during {label}.{hint}") from None
        except (URLError, OSError, TimeoutError):
            raise error_type(
                f"Network error during {label}; stopped without blindly repeating a write."
            ) from None
        except (ValueError, UnicodeDecodeError):
            raise error_type(f"Invalid Spotify JSON during {label}.") from None
    raise error_type("Spotify retries exhausted.")
