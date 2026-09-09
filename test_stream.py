import asyncio, json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8787/ws/stats") as ws:
        m = await ws.recv()
        msg = json.loads(m[6:])
        print("STATS ok: CPU %.0f%%, RAM %.0f%%, GPUs=%d, LLM=%s" % (
            msg["cpu"]["pct"], msg["ram"]["pct"], len(msg["gpus"]), msg["llm"]["online"]))
    async with websockets.connect("ws://127.0.0.1:8787/ws/logs") as ws:
        m = await ws.recv()
        print("LOGS ok:", m[4:90])

asyncio.run(main())
