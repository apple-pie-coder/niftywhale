#!/bin/sh
set -e

# First start on an empty volume: seed the universe from the image's copy.
if [ ! -f "$UNIVERSE_PATH" ]; then
    echo "entrypoint: seeding $UNIVERSE_PATH from image default"
    cp /app/data/universe.json "$UNIVERSE_PATH"
fi

exec "$@"
