"""Build the two requested benchmark subsets from existing project inputs."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()

    def read(name):
        return json.loads((args.inputs / name).read_text(encoding='utf-8-sig'))

    full = read('yakutia_real_full.json')
    small = read('yakutia_real_small.json')
    restricted = dict(full, **{field: small[field] for field in ('tugs', 'barges', 'compatible_pairs')})
    diagnostic = dict(restricted, cargo=sorted(full['cargo'], key=lambda cargo: cargo['volume_t'])[:17])
    args.output.mkdir(parents=True, exist_ok=True)
    for label, scenario in [('a', diagnostic), ('c', restricted)]:
        with (args.output / f'{label}.json').open('x', encoding='utf-8') as stream:
            json.dump(scenario, stream, ensure_ascii=False)


if __name__ == '__main__':
    main()
