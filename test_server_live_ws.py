"""WS probe: connect ASAP, print every message inline with flush, read ~55s."""
import json, time
from websockets.sync.client import connect

ws = connect("ws://127.0.0.1:8000/ws")
print("connected", flush=True)
end = time.time() + 55
n = 0
try:
    while time.time() < end:
        try:
            ws.settimeout(1.0)
            raw = ws.recv()
        except Exception:
            continue
        n += 1
        try:
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "transcript_update":
                print(f"  [t] {ev.get('text','')[:55]!r}", flush=True)
            elif t == "detection":
                print(f"  [DETECT] {ev.get('confidence_percent')}% {ev.get('reference')} ({ev.get('translation')})", flush=True)
            else:
                print(f"  [{t}] {raw[:90]}", flush=True)
        except Exception:
            print(f"  [raw] {raw[:90]}", flush=True)
        if n >= 12:
            break
finally:
    ws.close()
print(f"total messages: {n}", flush=True)