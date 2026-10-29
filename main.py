#!/usr/bin/env python3
"""sysdash — Cyberpunk System-Dashboard.

FastAPI-Server (127.0.0.1:8787) mit einem WebSocket-Stream:
  - "stats" : CPU/RAM/Swap/Top-Prozesse + beide GPUs + LLM-Slots (~2 Hz)
  - "log"   : docker logs -f llama-cpp, zeilenweise, max 500 Zeilen
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

PORT = int(os.environ.get("SYSDASH_PORT", "8787"))
STATIC = Path(__file__).parent / "static"
ENERGY_FILE = Path(__file__).parent / "energy.json"
SHELLEY_FILE = Path(__file__).parent / "shelly.json"
SHELLEY_URL = os.environ.get("SYSDASH_SHELLY", "http://192.168.178.21")
JOURNAL_LOGS = ["journalctl", "-u", "llama-cpp", "-f", "--no-pager", "-n", "200"]
SLOTS_URL = "http://127.0.0.1:9070/slots"
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=51.10&longitude=7.35"
    "&hourly=temperature_2m,precipitation_probability,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,weathercode"
    "&timezone=Europe%2FBerlin&forecast_days=7&forecast_hours=14"
)

app = FastAPI()


@app.on_event("startup")
async def startup():
    asyncio.create_task(energy_loop())
    asyncio.create_task(weather_loop())


# ---------------------------------------------------------------- collector
def _nvidia() -> list:
    """Eine Anfrage an nvidia-smi für beide GPUs."""
    fields = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed"
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        ).stdout.strip().splitlines()
        gpus = []
        for line in out:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 8:
                continue
            idx, name = int(parts[0]), parts[1]
            gpus.append({
                "idx": idx,
                "name": name,
                "util": float(parts[2]),
                "mem_used": float(parts[3]),
                "mem_total": float(parts[4]),
                "temp": float(parts[5]),
                "power": float(parts[6]),
                "fan": float(parts[7]),
            })
        return gpus
    except Exception:
        return []


def _slots() -> dict:
    """LLM-Telemetrie aus der llama.cpp-Server-API."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "2", SLOTS_URL],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        data = json.loads(out)
        slots = []
        if isinstance(data, dict):
            data = data.get("slots", [])
        for s in data:
            tp = s.get("t_predict") or 0
            nd = s.get("n_decoded") or 0
            slots.append({
                "id": s.get("id"),
                "processing": bool(s.get("is_processing")),
                "tps": (nd / tp) if tp > 0 else 0,
                "decoded": nd,
                "prompt_tokens": s.get("n_prompt_tokens", 0),
                "cache": s.get("n_prompt_tokens_cache", 0),
                "ctx": s.get("n_ctx", 0),
                "model": s.get("model", ""),
            })
        busy = any(s["processing"] for s in slots)
        return {"online": bool(slots), "busy": busy, "slots": slots}
    except Exception:
        return {"online": False, "busy": False, "slots": []}


def _top_procs(n: int = 6) -> list:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
    return procs[:n]


_prev_net = None


def _net() -> dict:
    """Netzwerk-Raten (Bytes/s) zwischen zwei Samples."""
    global _prev_net
    c = psutil.net_io_counters()
    now = time.time()
    if _prev_net is not None:
        dt = now - _prev_net[2]
        rx = max(0.0, (c.bytes_recv - _prev_net[0]) / dt) if dt > 0.05 else 0.0
        tx = max(0.0, (c.bytes_sent - _prev_net[1]) / dt) if dt > 0.05 else 0.0
    else:
        rx = tx = 0.0
    _prev_net = (c.bytes_recv, c.bytes_sent, now)
    return {"rx": rx, "tx": tx, "rx_total": c.bytes_recv, "tx_total": c.bytes_sent}


# ---------------------------------------------------------------- energie
class EnergyMeter:
    """Integriert die GPU-Leistung in kWh, persistiert in energy.json.
    Tage: {Datum: Wh}, gesamt: Wh, Session: Wh seit Server-Start."""

    def __init__(self, path: Path):
        self.path = path
        self.days = {}
        self.total_wh = 0.0
        self.session_wh = 0.0
        self._last_flush = 0.0
        self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            self.days = {k: float(v) for k, v in d.get("days", {}).items()}
            self.total_wh = float(d.get("total_wh", 0.0))
        except Exception:
            pass

    def _save(self):
        try:
            self.path.write_text(json.dumps(
                {"days": self.days, "total_wh": self.total_wh}))
        except Exception:
            pass

    def sample(self, watts: float, dt: float):
        if dt <= 0.05 or dt > 5:
            return
        wh = watts * dt / 3600.0
        self.session_wh += wh
        self.total_wh += wh
        today = date.today().isoformat()
        self.days[today] = self.days.get(today, 0.0) + wh
        if time.time() - self._last_flush > 60:
            self._last_flush = time.time()
            self._save()

    def flush(self):
        self._save()

    def report(self, watts: float) -> dict:
        return {
            "watts": watts,
            "today_wh": self.days.get(date.today().isoformat(), 0.0),
            "total_wh": self.total_wh,
            "session_wh": self.session_wh,
        }


_meter = EnergyMeter(ENERGY_FILE)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, lambda *_: (_meter.flush(), os._exit(0)))
    except Exception:
        pass


# ---------------------------------------------------------------- shellyem3
class ShellieMeter:
    """Liest die ShellyEM3 am Stromkasten (absoluter Zähler, Wh).

    today  = Deltazähler pro Tag (persistiert, übersteht Neustarts)
    total  = direkt vom Gerät (Gesamtwert seit Inbetriebnahme)
    """

    def __init__(self, path: Path):
        self.path = path
        self.days = {}
        self.last_total = None      # letzter gelesener Gesamtwert (Wh)
        self.online = False
        self._last_flush = 0.0
        self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            self.days = {k: float(v) for k, v in d.get("days", {}).items()}
            self.last_total = d.get("last_total")
            if self.last_total is not None:
                self.last_total = float(self.last_total)
        except Exception:
            pass

    def _save(self):
        try:
            self.path.write_text(json.dumps({
                "days": self.days,
                "last_total": self.last_total,
            }))
        except Exception:
            pass

    def sample(self):
        """Ein /status-Hit; ruft False, wenn offline."""
        try:
            out = subprocess.run(
                ["curl", "-s", "-m", "2", SHELLEY_URL + "/status"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            data = json.loads(out)
            emeters = data.get("emeters") or []
            total = sum(m.get("total", 0.0) for m in emeters)
            self.online = True
            if self.last_total is not None:
                delta = total - self.last_total
                if 0 <= delta <= 500:      # plausibel: max. ~500 Wh zwischen Polls
                    today = date.today().isoformat()
                    self.days[today] = self.days.get(today, 0.0) + delta
            self.last_total = total
            if time.time() - self._last_flush > 30:
                self._last_flush = time.time()
                self._save()
            return total
        except Exception:
            self.online = False
            return None

    def report(self, watts: float) -> dict:
        return {
            "online": self.online,
            "watts": watts,
            "today_wh": self.days.get(date.today().isoformat(), 0.0),
            "total_wh": self.last_total if self.last_total is not None else 0.0,
        }


_shelly = ShellieMeter(SHELLEY_FILE)


def _shelly_read() -> dict:
    """Ein /status-Hit: Tages-Delta aktualisieren + Live-Leistung liefern."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "2", SHELLEY_URL + "/status"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        em = json.loads(out).get("emeters") or []
        total = sum(m.get("total", 0.0) for m in em)
        watts = sum(m.get("power", 0.0) for m in em)
    except Exception:
        _shelly.online = False
        return _shelly.report(0.0)
    _shelly.online = True
    if _shelly.last_total is not None:
        delta = total - _shelly.last_total
        if 0 <= delta <= 500:      # plausibel: max. ~500 Wh zwischen Polls
            today = date.today().isoformat()
            _shelly.days[today] = _shelly.days.get(today, 0.0) + delta
    _shelly.last_total = total
    _shelly._save()
    rep = _shelly.report(watts)
    rep["phases"] = [m.get("power", 0.0) for m in em]
    return rep


def collect() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    cpu_pct = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    gpus = _nvidia()
    return {
        "ts": time.time(),
        "cpu": {"pct": cpu_pct, "cores": per_core, "count": len(per_core)},
        "ram": {"used": vm.used, "total": vm.total, "pct": vm.percent},
        "swap": {"used": sw.used, "total": sw.total, "pct": sw.percent},
        "load": list(os.getloadavg()),
        "net": _net(),
        "procs": _top_procs(),
        "gpus": gpus,
        "energy": _meter.report(sum(g["power"] for g in gpus)),
        "shelly": _shelly_last,
        "llm": _slots(),
        "weather": _weather,
    }


# ---------------------------------------------------------------- wetter
_weather = {"online": False, "updated": None, "hours": []}


def _weather_refresh():
    """Einmal laden; setzt _weather, liefert True bei Erfolg."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "5", WEATHER_URL],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        j = json.loads(out)
        h = j["hourly"]
        hours = [
            {
                "time": t,
                "temp": float(h["temperature_2m"][i]),
                "rain": float(h["precipitation_probability"][i]),
                "code": int(h["weather_code"][i]),
            }
            for i, t in enumerate(h["time"])
        ]
        d = j["daily"]
        days = [
            {
                "time": t,
                "tmin": float(d["temperature_2m_min"][i]),
                "tmax": float(d["temperature_2m_max"][i]),
                "sum": float(d["precipitation_sum"][i]),
                "pmax": d["precipitation_probability_max"][i],
                "code": int(d["weathercode"][i]),
            }
            for i, t in enumerate(d["time"])
        ]
        _weather.update({"online": True, "updated": time.time(), "hours": hours, "days": days})
        return True
    except Exception:
        _weather["online"] = False
        return False


async def weather_loop():
    """Lädt Open-Meteo für Remscheid: einmal sofort, dann nach jeder vollen
    Stunde neu; bei Fehlschlag alle 5 min."""
    while True:
        ok = _weather_refresh()
        if ok:
            # bis zur nächsten vollen Stunde warten
            now = time.time()
            nxt = (int(now // 3600) + 1) * 3600 - now
            await asyncio.sleep(max(30, nxt))
        else:
            await asyncio.sleep(300)


# ---------------------------------------------------------------- websockets
_shelly_last = {"online": False, "watts": 0.0, "today_wh": 0.0, "total_wh": 0.0}


async def energy_loop():
    """Globale Energie-Integration (alle 0,5 s) + ShellyEM3-Poll (alle 2 s)."""
    n = 0
    while True:
        await asyncio.sleep(0.5)
        try:
            gpus = _nvidia()
            _meter.sample(sum(g["power"] for g in gpus), 0.5)
        except Exception:
            pass
        n += 1
        if n % 4 == 0:      # Shelly alle 2 s
            try:
                _shelly_last.update(_shelly_read())
            except Exception:
                pass


async def ws_stats(ws: WebSocket):
    await ws.accept()
    # warm start: cpu_percent braucht einen ersten Takt
    psutil.cpu_percent(interval=None)
    while True:
        data = collect()
        await ws.send_text("stats " + json.dumps(data))
        await asyncio.sleep(0.5)


async def ws_logs(ws: WebSocket):
    await ws.accept()
    proc = await asyncio.create_subprocess_exec(
        *JOURNAL_LOGS,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        bufsize=0,
    )
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            # journalctl-Prefix ("Sep 10 14:45:50 host proc[pid]: ") wegschneiden
            if "]: " in text:
                text = text.split("]: ", 1)[1]
            if text:
                await ws.send_text("log " + text)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@app.websocket("/ws/stats")
async def stats_route(ws: WebSocket):
    try:
        await ws_stats(ws)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/logs")
async def logs_route(ws: WebSocket):
    try:
        await ws_logs(ws)
    except WebSocketDisconnect:
        pass


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- radar (DWD-Proxy)
# maps.dwd.de hat mehrere Backends: eins liefert teils Daten von gestern, und
# viele parallele Requests hängen ~50 s. Daher hier: feste Zeit pro Bild (altes
# Backend antwortet dann mit XML-Fehler → neuer Versuch), Timeout, max. 3
# gleichzeitige Requests, Cache.
DWD_WMS = "https://maps.dwd.de/geoserver/dwd/wms"
RADAR_STYLE = "radar_rv_product_1x1km_ger"
RADAR_LAYERS = {
    "radar": "dwd:Radar_rv_product_1x1km_ger",          # Messung + Nowcast bis +2 h
    "model": "dwd:Icon-eu_reg00625_fd_sl_TOTPREC01H",   # ICON-EU, Niederschlag 1 h
}
_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:00\.000Z$")
_radar_cache = OrderedDict()    # key -> (bytes, läuft_ab)
_RADAR_CACHE_MAX = 400
_dwd_sem = asyncio.Semaphore(3)


def _dwd_get(params: dict) -> bytes:
    req = Request(DWD_WMS + "?" + urlencode(params), headers={"User-Agent": "sysdash"})
    with urlopen(req, timeout=12) as r:
        ct = r.headers.get("Content-Type", "")
        body = r.read()
    if not ct.startswith("image/"):
        raise ValueError(body[:200].decode(errors="replace"))
    return body


async def _dwd_image(kind: str, t: str, bbox: str, w: int, h: int) -> bytes:
    key = (kind, t, bbox, w, h)
    hit = _radar_cache.get(key)
    if hit and hit[1] > time.time():
        _radar_cache.move_to_end(key)
        return hit[0]
    params = {
        "service": "WMS", "request": "GetMap", "version": "1.1.1",
        "layers": RADAR_LAYERS[kind], "styles": RADAR_STYLE,
        "format": "image/png", "transparent": "true", "srs": "EPSG:3857",
        "bbox": bbox, "width": w, "height": h, "time": t,
    }
    last = None
    for attempt in range(4):
        try:
            async with _dwd_sem:
                body = await asyncio.to_thread(_dwd_get, params)
            ts = datetime.strptime(t, "%Y-%m-%dT%H:%M:00.000Z").replace(tzinfo=timezone.utc).timestamp()
            # Vergangenheit ändert sich nicht mehr; Vorhersagen werden neu gerechnet
            ttl = 3600 if ts <= time.time() else (300 if kind == "radar" else 1800)
            _radar_cache[key] = (body, time.time() + ttl)
            while len(_radar_cache) > _RADAR_CACHE_MAX:
                _radar_cache.popitem(last=False)
            return body
        except Exception as e:
            last = e
            await asyncio.sleep(0.4 * (attempt + 1))
    raise last


@app.get("/api/radar")
async def radar_image(kind: str, t: str, bbox: str, w: int = 512, h: int = 512):
    """Ein Radar-/Modellbild für Zeit t (UTC, ISO) und bbox (EPSG:3857)."""
    try:
        parts = [float(x) for x in bbox.split(",")]
    except ValueError:
        parts = []
    if kind not in RADAR_LAYERS or not _TIME_RE.match(t) or len(parts) != 4 \
            or not (16 <= w <= 2048 and 16 <= h <= 2048):
        return Response(status_code=400)
    bbox = ",".join(f"{x:.0f}" for x in parts)
    try:
        body = await _dwd_image(kind, t, bbox, w, h)
    except Exception:
        return Response(status_code=502)
    return Response(content=body, media_type="image/png", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
