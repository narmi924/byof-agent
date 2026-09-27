"""Independent persistent factory clock, using real time only to pace business ticks."""

import argparse
import time

from packages.persistence import connect
from packages.settings import Settings
from services.factory_sim.service import run_due_tick


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    engine = connect(Settings().factory_database_url.get_secret_value())
    try:
        while True:
            did_work = run_due_tick(engine)
            if args.once:
                return
            if not did_work:
                time.sleep(0.1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
