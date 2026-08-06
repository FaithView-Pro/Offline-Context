"""Milestone 4 WS+REST smoke test. Run while server.py is up on 127.0.0.1:8000."""
import json
import urllib.request
import urllib.error
import time
from websockets.sync.client import connect

BASE = "http://127.0.0.1:8000"


def post(path, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=8)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    # Verify the frontend HTML is served
    html = urllib.request.urlopen(BASE + "/", timeout=8).read().decode()
    assert "Live transcript" in html and "Recent detections" in html \
        and "Live display" in html and "Queue" in html, "frontend panels missing"
    print("GET / -> HTML served (4 panels present)")

    # open a WS and collect events in a separate thread while we fire REST calls
    ws = connect("ws://127.0.0.1:8000/ws")
    received = []

    import threading
    def reader():
        try:
            while True:
                received.append(ws.recv())
        except Exception:
            pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()

    st, r = post("/queue/add", {"reference": "Romans 8:28", "translation": "NKJV",
                                "text": "And we know that all things work together for good to those who love God.",
                                "confidence": 0.89, "confidence_band": "review queue"})
    item_id = r.get("id")
    assert st == 200 and item_id, (st, r)
    print("queue/add ->", item_id)

    st, r2 = post("/queue/add", {"reference": "Ezekiel 36:27", "translation": "AMP",
                                 "text": "I will put My Spirit within you..."})
    print("queue/add 2 ->", r2.get("id"))

    st, r = post("/present", {"reference": "Romans 8:28", "translation": "NKJV"})
    print("present ->", st, r.get("reference"), r.get("translation"))
    assert st == 200 and r.get("reference") == "Romans 8:28", (st, r)

    st, r = post("/queue/" + item_id, method="DELETE")
    print("delete ->", st, r)
    assert r.get("removed") is True, r

    st, r = post("/queue/clear", method="POST")
    print("clear ->", st, r)

    st, r = post("/display/clear", method="POST")
    print("display/clear ->", st, r)

    time.sleep(1.0)  # let broadcasts flush
    ws.close()

    print("\n=== WS EVENTS RECEIVED (in order) ===")
    seen_types = []
    for raw in received:
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        seen_types.append(ev.get("type"))
        t = ev.get("type")
        if t == "queue_update":
            print(f"  queue_update queue_len={len(ev.get('queue', []))} ids={[q.get('id','') for q in ev.get('queue', [])][:3]}")
        elif t == "display_update":
            if ev.get("clear"):
                print("  display_update CLEAR")
            else:
                print(f"  display_update {ev.get('reference')} ({ev.get('translation')}) :: {(ev.get('text') or '')[:50]}")
        else:
            print(f"  {t}: {raw[:80]}")

    # correctness assertions
    assert "queue_update" in seen_types, "expected queue_update broadcasts"
    assert any("display_update" == s for s in seen_types), "expected display_update broadcast"
    print("\nALL M4 SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()