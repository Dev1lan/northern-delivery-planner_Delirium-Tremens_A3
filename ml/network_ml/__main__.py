"""Print a network forecast from JSON inputs."""

import argparse
import json
from pathlib import Path

from .model import DEFAULT_ARTIFACT, forecast


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--tariffs", type=Path)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    raw = json.loads(args.scenario.read_text(encoding="utf-8-sig"))
    tariffs = json.loads(args.tariffs.read_text(encoding="utf-8-sig")) if args.tariffs else None
    answer = forecast(raw, tariffs=tariffs, artifact_path=args.artifact, enabled=not args.disable)
    print(json.dumps(answer, indent=2, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
