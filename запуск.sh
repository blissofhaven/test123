#!/bin/sh
cd "$(dirname "$0")"
python3 -m rza_calc rza_calc/examples/ps_promyshlennaya.json report | less -R
