#!/bin/sh
cd "$(dirname "$0")"
python3 -m rza_calc rza_calc/examples/oilfield_gtes.json report | less -R
