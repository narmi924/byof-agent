import argparse
import json
import sys

from packages.agent.router import OfflineDemoModel, RouteError, RouterAgent
from packages.providers.gateway import Gateway, GatewayError


def main():
    parser = argparse.ArgumentParser(description="Minimal production-planning router")
    parser.add_argument("request")
    parser.add_argument("--mode", choices=("mock", "gateway"), default="mock")
    args = parser.parse_args()
    try:
        model = OfflineDemoModel() if args.mode == "mock" else Gateway()
        output = RouterAgent(model).run(args.request)
        output["model_mode"] = args.mode
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (RouteError, GatewayError) as exc:
        print(
            json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
