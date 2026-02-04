#!/bin/bash
# Build wrapper script to set correct CFLAGS for pgnats
export CFLAGS="-std=gnu11"
exec cargo "$@"
