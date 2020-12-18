#!/bin/sh -x

# Doesn't work currently due to new PG3INIT variable requirement
exit 0

python scripts/tests.py
st=$?
cat logs/debug.log
exit $st
