#!/bin/sh
set -e

# Files the app makes (the database, its WAL, the lab's records) are readable by this account only:
# the database holds the broker and Telegram secrets (encrypted when the key is mounted).
umask 077

# First start on an empty volume: seed the universe from the image's copy.
if [ ! -f "$UNIVERSE_PATH" ]; then
    echo "entrypoint: seeding $UNIVERSE_PATH from image default"
    cp /app/data/universe.json "$UNIVERSE_PATH"
fi

exec "$@"
