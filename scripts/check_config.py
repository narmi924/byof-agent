"""Print configuration presence only; no credentials, network requests or messages."""

import json

from pydantic import ValidationError

from packages.settings import Settings


def main() -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        print(json.dumps({"invalid_fields": [list(e["loc"]) for e in exc.errors()]}))
        return 1
    print(json.dumps({"configured": settings.presence(), "live_tests_run": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
