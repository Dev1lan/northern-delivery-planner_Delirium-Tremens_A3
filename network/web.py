"""Serve the local Yakutia demonstration on loopback."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from experiments.run_yakutia import bundle_results, calculate, read
from network.analysis import checked, compare_results, explain_voyage, port_at, port_load, positions_at

ASSETS = Path(__file__).with_name('web_assets')
STATIC = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/style.css': ('style.css', 'text/css')}


class Handler(BaseHTTPRequestHandler):
    def send(self, payload, mime, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def json(self, payload, status=HTTPStatus.OK):
        self.send(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                  'application/json', status)

    def do_POST(self):
        if self.path != '/api/plan':
            self.json({'error': 'Unknown endpoint'}, HTTPStatus.NOT_FOUND)
            return
        # One assignment publishes only a complete set of audited results.
        bundle = calculate()
        self.server.bundle = bundle
        self.json(bundle)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in STATIC:
            name, mime = STATIC[url.path]
            self.send((ASSETS / name).read_bytes(), mime)
            return
        if url.path not in {'/api/demo', '/api/state', '/api/port-load', '/api/explain',
                            '/api/improvements', '/api/compare'}:
            self.json({'error': 'Unknown endpoint'}, HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(url.query)
        bundle = self.server.bundle
        plans = bundle['plans']
        try:
            if url.path == '/api/demo':
                response = bundle
            elif url.path == '/api/improvements':
                response = bundle['improvements']
            elif url.path == '/api/compare':
                response = compare_results(plans[query['from'][0]], plans[query['to'][0]])
            else:
                plan = plans[query.get('variant', ['C'])[0]]
                if url.path == '/api/state':
                    moment = query['moment'][0]
                    response = {'vessels': positions_at(plan, moment),
                                'ports': {port: port_at(plan, port, moment) for port in plan['scenario']['ports']}}
                elif url.path == '/api/port-load':
                    response = port_load(plan, query['port'][0], query['start'][0], query['end'][0])
                else:
                    response = explain_voyage(plan, query['voyage'][0])
        except (ValueError, KeyError, TypeError) as exc:
            self.json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.json(response)


def make_server(port, bundle):
    for result in bundle_results(bundle):
        checked(result)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.bundle = bundle
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bundle', type=Path, help='Previously generated and audited bundle.json')
    args = parser.parse_args(argv)
    bundle = read(args.bundle) if args.bundle else calculate()
    with make_server(args.port, bundle) as server:
        print(f'http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
