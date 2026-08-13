#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python web_app.py "$@"