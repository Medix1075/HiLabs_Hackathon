"""Serve the UI backed by pipeline/out and the real ptd_run command.

    python run_ui.py
    python run_ui.py --port 8000 --no-browser
    python run_ui.py --pipeline-dir /path/to/pipeline
"""
from __future__ import annotations

import argparse
import http.server
import sys
import webbrowser
from pathlib import Path

from ptd_ui import PipelineData, PipelineJob, make_handler

ROOT_DIR = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--pipeline-dir', type=Path, default=ROOT_DIR / 'pipeline')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    root = args.pipeline_dir.resolve()
    if not (root / 'ptd_run/__main__.py').is_file():
        parser.error(f'Pipeline runner not found in {root}')
    data = PipelineData(root)
    job = PipelineJob(data)
    for port in range(args.port, min(args.port + 100, 65536)):
        try:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', port), make_handler(data, job, ROOT_DIR / 'ui'))
            break
        except OSError:
            if port == min(args.port + 99, 65535):
                raise
    url = f'http://127.0.0.1:{port}'
    print(f'Centene PTD Intelligence Platform\nUI: {url}\nPipeline: {root}\nOutputs: {data.out}', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping UI and any active pipeline run.', flush=True)
    finally:
        server.server_close()
        job.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
