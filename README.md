# bot

A game bot that speaks JSON lines on stdin/stdout. The image is built from the `Dockerfile` and runs `bot.py`.

| File | What it is |
| --- | --- |
| `bot.py` | Decisions |
| `nav.py` | Map model: walk grid, line of sight, paths |
| `wire.py` | stdin/stdout loop |
