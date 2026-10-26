#!/usr/bin/env python3
import sys
import uuid
import httpx
from datetime import datetime, timezone

def main():
    service_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    print("=" * 60)
    print("  QuickDrop Rider Chat Simulator (WhatsApp Webhook)")
    print(f"  Target Service: {service_url}")
    print("=" * 60)

    rider_id = input("\nEnter Rider ID (e.g., R003, R008, R011, R027) [default: R003]: ").strip().upper() or "R003"
    print(f"\nConnected as Rider: {rider_id}")
    print("Type your message below in Hindi, English, or Hinglish.")
    print("Type 'exit' or press Ctrl+C to quit.\n" + "-" * 60)

    client = httpx.Client(base_url=service_url, timeout=15.0)

    # Check health
    try:
        h = client.get("/health")
        if h.status_code != 200:
            print(f"[Warning] Service health check returned status {h.status_code}")
    except Exception as e:
        print(f"[Error] Could not connect to {service_url}: {e}")
        return

    while True:
        try:
            text = input(f"\n[{rider_id}] You: ").strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit", "q"):
                print("Exiting chat simulator. Bye!")
                break

            msg_payload = {
                "message_id": f"manual-{uuid.uuid4().hex[:10]}",
                "rider_id": rider_id,
                "text": text,
                "received_at": "2026-09-22T09:05:00+05:30",
            }

            resp = client.post("/messages", json=msg_payload)
            if resp.status_code == 200:
                reply = resp.json().get("reply", "")
                print(f"[Agent]: {reply}")
            else:
                print(f"[Error {resp.status_code}]: {resp.text}")

        except KeyboardInterrupt:
            print("\nExiting chat simulator. Bye!")
            break
        except Exception as e:
            print(f"[Request Error]: {e}")

if __name__ == "__main__":
    main()
