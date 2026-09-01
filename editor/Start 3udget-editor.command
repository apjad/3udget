#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")"
open "http://localhost:8422" &
python3 server.py
