README section 12 (API reference) is generated from these files.

  spec.py          every endpoint: group, description, parameters, example request
  capture_get.py   GET answers from the running app (read-only), with an API token in NW_TOKEN:
                   NW_TOKEN=... python3 capture_get.py <this dir> <dir>/out/get.json   (revoke the token afterwards)
  capture_post.py  POST answers from a throwaway copy, run in a container with --network none, a copy of the code
                   WITHOUT .env, a DB snapshot with every secret removed, and every Dhan/Telegram variable set empty
                   (a real Dhan login from a copy would replace the live app's token)
  build.py         trims the answers, replaces personal values, rewrites section 12:  python3 build.py <dir> ../../README.md
