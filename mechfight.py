"""Minimal mechfight/1 runtime: stdin/stdout loop, newest-observation rule, debug drawing, logging.

    import mechfight as mf
    class Bot:
        def __init__(self, hello): ...
        def act(self, obs): return {"tick": obs["tick"], ...}
    mf.run(Bot)
"""
import json
import queue
import sys
import threading
import traceback

_CONTINUOUS = ("move", "aim", "boost")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class Draw:
    def __init__(self):
        self.caps = {}
        self.clear()

    def clear(self):
        self.d = {}

    def _add(self, key, cap, default, item):
        lst = self.d.setdefault(key, [])
        if len(lst) < int(self.caps.get(cap, default)):
            lst.append(item)

    def line(self, a, b, color=None):
        self._add("lines", "max_lines", 64, {"a": list(a), "b": list(b), **({"color": color} if color else {})})

    def point(self, at, color=None, size=None):
        it = {"at": list(at)}
        if color:
            it["color"] = color
        if size:
            it["size"] = max(1, min(12, size))
        self._add("points", "max_points", 64, it)

    def text(self, text, at=None, color=None):
        it = {"text": str(text)[: int(self.caps.get("max_text_chars", 80))]}
        if at is not None:
            it["at"] = list(at)
        if color:
            it["color"] = color
        self._add("text", "max_text", 16, it)

    def label(self, text):
        self.d["label"] = str(text)[: int(self.caps.get("max_text_chars", 80))]

    def take(self):
        d, self.d = self.d, {}
        if not d:
            return None
        if len(json.dumps(d)) > int(self.caps.get("max_bytes", 8192)):
            d = {"label": d["label"]} if "label" in d else None
        return d


draw = Draw()


def _send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reader(q):
    for line in sys.stdin:
        q.put(line)
    q.put(None)


def run(make_bot):
    q = queue.Queue()
    threading.Thread(target=_reader, args=(q,), daemon=True).start()
    bot = None
    last = {}
    errors = 0
    while True:
        batch = [q.get()]
        while True:  # drain: only the newest observation is answered
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for raw in batch:
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                draw.caps = msg.get("config", {}).get("debug", {}) or {}
                try:
                    bot = make_bot(msg)
                except Exception:
                    log(traceback.format_exc())
                name = getattr(bot, "name", None) or "bot"
                _send({"type": "ready", "name": str(name)[:32]})
            elif kind == "obs":
                latest = msg
            elif kind == "end":
                return
        if latest is None:
            continue
        action = {}
        draw.clear()
        if bot is not None:
            try:
                action = dict(bot.act(latest) or {})
                last = {k: action[k] for k in _CONTINUOUS if k in action}
            except Exception:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log(f"act() failed at tick {latest.get('tick')} ({errors}):\n{traceback.format_exc()}")
                action = dict(last)
        action["tick"] = latest["tick"]
        d = draw.take()
        if d and "debug" not in action:
            action["debug"] = d
        try:
            _send(action)
        except (TypeError, ValueError):
            log(f"unserializable action at tick {latest.get('tick')}:\n{traceback.format_exc()}")
            _send({"tick": latest["tick"], **last})
