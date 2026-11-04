#!/usr/bin/env python3
"""
pi-agent — agente ligero de diagnóstico y control para Raspberry Pi (hub de bots de Pi Monitor).
Solo usa la librería estándar de Python 3. Sirve la app web (index.html),
una API protegida con token, gestiona los bots y manda avisos por Telegram.

Novedades 2.0:
  - Desactivar de verdad (para + deshabilita el servicio y su timer).
  - Feed común: los bots mandan eventos a /api/evento (desde la propia Pi, sin token).
  - Señal de vida de los bots (/api/latido) y detección de bots colgados.
  - Autorreinicio con límite diario (por defecto 3).
  - Historial de temperatura/CPU/RAM para gráficas de 24 h y 7 días.
  - Reparto del disco por bot, estado de Tailscale y limpieza de logs.
  - Versión de cada bot y actualización con git pull.
  - Horario de cada bot guardado en el agente (lo leerá pihub.py).
  - Resumen diario.
"""
import hmac
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.1"
CONFIG_PATH = os.environ.get("PI_AGENT_CONFIG", "/etc/pi-agent/config.json")
DATA_DIR = os.path.dirname(CONFIG_PATH)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(APP_DIR, "pi-agent-unit")
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]+$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_OUT = 200_000
HOST = socket.gethostname()
CFG_LOCK = threading.Lock()
CATS = ("wallapop", "opos", "diecast", "musica", "otros", "sistema")

DEFAULTS = {
    "port": 8790, "bind": "0.0.0.0", "services": [], "bots": [],
    "allow_exec": True, "exec_timeout": 30,
    "telegram": {"token": "", "chat_id": ""},
    "peers": [],
    "alerts": {"temp": 75, "disk": 90, "offline_after": 3, "boot": True, "shutdown": True},
    "heartbeat_url": "",
    "resumen": {"activo": True, "hora": "22:00"},
}
BOT_DEFAULTS = {"cat": "otros", "autorestart": True, "max_restarts": 3, "hang_after": 15}


# ---------------------------------------------------------------- config
def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        if isinstance(v, dict):
            cfg[k] = {**v, **(cfg.get(k) or {})}
        elif isinstance(v, list):
            cfg.setdefault(k, list(v))
        else:
            cfg.setdefault(k, v)
    for b in cfg["bots"]:
        for k, v in BOT_DEFAULTS.items():
            b.setdefault(k, v)
    return cfg


def save_config(cfg):
    with CFG_LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)


def save_json(path, data):
    """Guardado atómico de los ficheros de datos (eventos, historial…)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        print("guardar", os.path.basename(path), e, flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


CFG = load_config()


# ---------------------------------------------------------------- helpers
def _txt(x):
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return x


def run(cmd, timeout=10, shell=False, cwd=None, input=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input,
                           shell=shell, cwd=cwd, executable="/bin/bash" if shell else None)
        return p.returncode, p.stdout[-MAX_OUT:], p.stderr[-MAX_OUT:]
    except FileNotFoundError:
        return 127, "", "comando no encontrado"
    except subprocess.TimeoutExpired as e:
        return 124, _txt(e.stdout)[-MAX_OUT:], _txt(e.stderr)[-MAX_OUT:] + f"\n[tiempo agotado tras {timeout}s]"


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:30].strip("-")


def http_json(url, data=None, headers=None, timeout=8):
    body, h = None, dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def today(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def day_bounds(fecha):
    t0 = time.mktime(time.strptime(fecha, "%Y-%m-%d"))
    return t0, t0 + 86400


def find_bot(ident):
    """Busca un bot por unidad (wallabot.service), por nombre de unidad (wallabot) o por etiqueta."""
    ident = str(ident or "").strip()
    if not ident:
        return None
    for b in CFG["bots"]:
        unit = b["unit"]
        if ident in (unit, unit.rsplit(".", 1)[0], b.get("name")):
            return b
    return None


# ---------------------------------------------------------------- métricas
def cpu_percent(interval=0.3):
    def snap():
        parts = [int(x) for x in read("/proc/stat", "cpu 0 0 0 0").splitlines()[0].split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return idle, sum(parts)
    i1, t1 = snap()
    time.sleep(interval)
    i2, t2 = snap()
    dt = t2 - t1
    return round(100 * (1 - (i2 - i1) / dt), 1) if dt else 0.0


def meminfo():
    info = {}
    for line in (read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        try:
            info[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    swap_t = info.get("SwapTotal", 0)
    swap_f = info.get("SwapFree", 0)
    return {
        "total": total, "used": total - avail,
        "percent": round(100 * (total - avail) / total, 1) if total else 0,
        "swap_total": swap_t, "swap_used": swap_t - swap_f,
    }


def temperature():
    t = read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(t) / 1000, 1)
    except (TypeError, ValueError):
        return None


def disk_percent():
    du = shutil.disk_usage("/")
    return du, round(100 * du.used / du.total, 1)


THROTTLE_BITS = {
    0: "Bajo voltaje AHORA", 1: "Frecuencia limitada AHORA", 2: "Throttling AHORA",
    3: "Límite de temperatura AHORA", 16: "Bajo voltaje desde el arranque",
    17: "Frecuencia limitada desde el arranque", 18: "Throttling desde el arranque",
    19: "Límite de temperatura desde el arranque",
}


def throttled():
    code, out, _ = run(["vcgencmd", "get_throttled"], timeout=3)
    if code != 0 or "=" not in out:
        return {"available": False}
    val = int(out.strip().split("=")[1], 16)
    flags = [txt for bit, txt in THROTTLE_BITS.items() if val & (1 << bit)]
    return {"available": True, "raw": hex(val), "value": val, "now": bool(val & 0xF),
            "past": bool(val & 0xF0000), "flags": flags}


UNIT_PROPS = ("ActiveState,SubState,Description,ActiveEnterTimestamp,InactiveEnterTimestamp,"
              "NRestarts,LoadState,UnitFileState,ExecMainStatus,Result")


def systemctl_show(unit, props):
    _, out, _ = run(["systemctl", "show", unit, "--property=" + props], timeout=5)
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def unit_info(unit):
    return {"name": unit, **systemctl_show(unit, UNIT_PROPS)}


ENABLED_STATES = ("enabled", "enabled-runtime", "static", "alias", "indirect", "generated")


def timer_of(unit):
    return unit[:-8] + ".timer" if unit.endswith(".service") else None


def bot_activado(unit, unit_file_state=None, timer_props=None):
    """Un bot está activado si su servicio (o su timer, si lo tiene) arranca solo."""
    t = timer_of(unit)
    if t:
        ts = timer_props if timer_props is not None else systemctl_show(t, "LoadState,UnitFileState")
        if ts.get("LoadState") == "loaded":
            return ts.get("UnitFileState") in ENABLED_STATES
    if unit_file_state is None:
        unit_file_state = systemctl_show(unit, "UnitFileState").get("UnitFileState", "")
    return unit_file_state in ENABLED_STATES


def bot_workdir(b):
    wd = (b.get("workdir") or "").strip()
    if not wd:
        wd = systemctl_show(b["unit"], "WorkingDirectory").get("WorkingDirectory", "").lstrip("!")
    if wd.startswith("~"):
        wd = os.path.expanduser(wd)
    return wd if wd and os.path.isdir(wd) else ""


_PORTS = {"at": 0, "data": {}}
_PORTS_LOCK = threading.Lock()


def listening_ports():
    """Puertos TCP en los que escucha cada servicio (para mostrar homelab1:8787 en la app).
    Se ignoran los que solo escuchan en 127.0.0.1, porque desde el móvil no se llega a ellos.
    ss solo enseña el proceso si es del mismo usuario que el agente (sergio), como tus bots."""
    with _PORTS_LOCK:
        if time.time() - _PORTS["at"] < 30:
            return _PORTS["data"]
        code, out, _ = run(["ss", "-Hltnp"], timeout=5)
        res = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            addr, _, port = parts[3].rpartition(":")
            if not port.isdigit() or addr.startswith("127.") or addr in ("[::1]", "::1"):
                continue
            for pid in set(re.findall(r"pid=(\d+)", line)):
                m = re.search(r"/([A-Za-z0-9@._:-]+\.service)", read(f"/proc/{pid}/cgroup", "") or "")
                if m:
                    res.setdefault(m.group(1), set()).add(int(port))
        _PORTS["data"] = {k: sorted(v) for k, v in res.items()}
        _PORTS["at"] = time.time()
        return _PORTS["data"]


def bot_info(b):
    unit = b["unit"]
    d = unit_info(unit)
    d["puertos"] = listening_ports().get(unit, [])
    d["puerto"] = b.get("puerto")  # puesto a mano en la app; tiene prioridad sobre el detectado
    lat = LAT.get(unit)
    t = timer_of(unit)
    tprops = systemctl_show(t, "LoadState,ActiveState,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec") if t else {}
    d.update({
        "unit": unit, "label": b.get("name") or unit, "managed": bool(b.get("managed")),
        "schedule": b.get("schedule", ""),
        "def": {k: b.get(k) for k in ("command", "workdir", "restart", "schedule", "autostart")},
        "cat": b.get("cat", "otros"),
        "activado": bot_activado(unit, d.get("UnitFileState"), tprops),
        "autorestart": bool(b.get("autorestart", True)),
        "max_restarts": int(b.get("max_restarts", 3)),
        "hang_after": int(b.get("hang_after", 15)),
        "latido": lat,
        "latido_hace": int(time.time() - lat) if lat else None,
        "colgado": GUARD.is_hung(unit),
        "reinicios_hoy": GUARD.total(unit),
        "bloqueado": GUARD.is_blocked(unit),
        "horario": b.get("horario") or {},
    })
    if tprops.get("LoadState") == "loaded":
        d["timer"] = {"active": tprops.get("ActiveState"), "next": tprops.get("NextElapseUSecRealtime", ""),
                      "last": tprops.get("LastTriggerUSec", "")}
    return d


def failed_units():
    _, out, _ = run(["systemctl", "--failed", "--no-legend", "--plain"], timeout=5)
    return [l.split()[0] for l in out.splitlines() if l.strip()]


def ip_addresses():
    _, out, _ = run(["hostname", "-I"], timeout=3)
    return out.split()


def status():
    du, dpct = disk_percent()
    load = (read("/proc/loadavg", "0 0 0") or "0 0 0").split()[:3]
    uptime_s = float((read("/proc/uptime", "0") or "0").split()[0])
    os_name = "Linux"
    for line in (read("/etc/os-release", "") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    th = throttled()
    th.pop("value", None)
    return {
        "hostname": HOST,
        "agent_version": VERSION,
        "time": int(time.time()),
        "model": (read("/proc/device-tree/model", "") or "").replace("\x00", ""),
        "os": os_name,
        "kernel": platform.release(),
        "uptime": int(uptime_s),
        "cpu": {"percent": cpu_percent(), "count": os.cpu_count() or 1,
                "load": [float(x) for x in load]},
        "temp": temperature(),
        "memory": meminfo(),
        "disk": {"total": du.total, "used": du.used, "percent": dpct},
        "throttled": th,
        "ips": ip_addresses(),
        "failed_units": failed_units(),
        "services": [unit_info(u) for u in CFG["services"]],
        "bots": [bot_info(b) for b in CFG["bots"]],
        "notify": telegram_on(),
        "peers": [p["name"] for p in CFG["peers"]],
        "allow_exec": CFG["allow_exec"],
        "eventos_ultimo": EV.data["ultimo"],
    }


def list_units():
    _, out, _ = run(["systemctl", "list-units", "--type=service", "--all",
                     "--no-legend", "--plain", "--no-pager"], timeout=8)
    units = []
    for l in out.splitlines():
        p = l.split(None, 4)
        if len(p) >= 4 and p[1] == "loaded":
            units.append({"name": p[0], "active": p[2], "sub": p[3],
                          "description": p[4] if len(p) > 4 else ""})
    return units


def sudo_puede(*cmd):
    return run(["sudo", "-n", "-l", *cmd], timeout=5)[0] == 0


def restart_unit(unit):
    return run(["sudo", "-n", "systemctl", "restart", unit], timeout=60)


# ---------------------------------------------------------------- eventos (feed)
EVENTS_PATH = os.path.join(DATA_DIR, "eventos.json")
MAX_EVENTOS = 2000


class Eventos:
    """Historial de eventos que la app del móvil va recogiendo (/api/eventos).
    El id es un contador guardado en disco: no depende del reloj (la Pi no tiene RTC).
    Cada evento lleva de dónde viene (bot, cat) y de qué tipo es, para el feed y el resumen."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"ultimo": 0, "eventos": []}
        d = load_json(EVENTS_PATH, {})
        if isinstance(d.get("eventos"), list):
            self.data = {"ultimo": int(d.get("ultimo", 0)), "eventos": d["eventos"][-MAX_EVENTOS:]}
        self.ratelimit = {}

    def add(self, titulo, msg, prio=3, bot=None, cat="sistema", tipo="aviso"):
        with self.lock:
            self.data["ultimo"] += 1
            ev = {"id": self.data["ultimo"], "ts": int(time.time()), "host": HOST,
                  "titulo": titulo, "msg": msg, "prio": prio, "cat": cat, "tipo": tipo}
            if bot:
                ev["bot"] = bot
            self.data["eventos"].append(ev)
            self.data["eventos"] = self.data["eventos"][-MAX_EVENTOS:]
            save_json(EVENTS_PATH, self.data)
            return ev

    def permitido(self, key, por_min=30):
        """Evita que un bot desbocado llene el feed: máximo 30 eventos por minuto y bot."""
        now = time.time()
        with self.lock:
            lst = [t for t in self.ratelimit.get(key, []) if now - t < 60]
            if len(lst) >= por_min:
                self.ratelimit[key] = lst
                return False
            lst.append(now)
            self.ratelimit[key] = lst
            return True

    def since(self, n):
        with self.lock:
            return {"ultimo": self.data["ultimo"],
                    "eventos": [e for e in self.data["eventos"] if n is not None and e["id"] > n]}

    def entre(self, t0, t1):
        with self.lock:
            return [e for e in self.data["eventos"] if t0 <= e["ts"] < t1]


EV = Eventos()


def telegram_on():
    tg = CFG["telegram"]
    return bool(tg.get("token") and tg.get("chat_id"))


def notify(text):
    """Manda un mensaje por Telegram (opcional)."""
    tg = CFG["telegram"]
    if not telegram_on():
        return False, "Telegram no está configurado"
    try:
        r = http_json(f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                      {"chat_id": tg["chat_id"], "text": text, "disable_web_page_preview": True})
        return bool(r.get("ok")), r.get("description", "")
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", str(e))
        except Exception:
            desc = str(e)
        return False, desc
    except Exception as e:
        return False, str(e)


def notify_retry(text, tries=6, wait=15):
    """Para el aviso de encendido: la red puede tardar en estar lista."""
    for _ in range(tries):
        ok, _ = notify(text)
        if ok:
            return
        time.sleep(wait)


def aviso(icono, texto, prio=3, telegram=True, bot=None, cat="sistema", tipo="aviso"):
    """Registra un aviso de esta Pi para la app del móvil y, si está configurado, lo manda a Telegram."""
    EV.add(f"{icono} {HOST}", texto, prio, bot=bot, cat=cat, tipo=tipo)
    if telegram and telegram_on():
        notify(f"{icono} {HOST}: {texto}")


# ---------------------------------------------------------------- señal de vida
LATIDOS_PATH = os.path.join(DATA_DIR, "latidos.json")


class Latidos:
    """Última señal de vida de cada bot. Solo se vigila a los bots que la han mandado alguna vez."""

    def __init__(self):
        self.lock = threading.Lock()
        d = load_json(LATIDOS_PATH, {})
        self.data = {k: float(v) for k, v in d.items()} if isinstance(d, dict) else {}
        self.dirty = False

    def mark(self, unit, ts=None):
        with self.lock:
            self.data[unit] = ts or time.time()
            self.dirty = True

    def get(self, unit):
        with self.lock:
            return self.data.get(unit)

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            data, self.dirty = dict(self.data), False
        save_json(LATIDOS_PATH, data)


LAT = Latidos()


# ---------------------------------------------------------------- autorreinicio
class Guardian:
    """Reinicia los bots que fallan o se cuelgan, con un máximo de reinicios al día.
    Cuenta también los reinicios que hace systemd por su cuenta (Restart=always/on-failure):
    si un bot pasa del límite, lo para hasta el día siguiente o hasta que lo arranques tú."""

    def __init__(self):
        self.lock = threading.Lock()
        self.day = today()
        self.ours = {}       # reinicios hechos por el agente hoy
        self.sysacc = {}     # reinicios hechos por systemd hoy
        self.last_n = {}     # último NRestarts visto
        self.blocked = set() # parados por superar el límite
        self.hung = set()    # avisados como colgados (se limpia con el siguiente latido)

    def total(self, unit):
        with self.lock:
            return self.ours.get(unit, 0) + self.sysacc.get(unit, 0)

    def is_blocked(self, unit):
        return unit in self.blocked

    def is_hung(self, unit):
        return unit in self.hung

    def reset(self, unit):
        """El usuario ha arrancado o reiniciado el bot a mano: empieza de cero."""
        with self.lock:
            self.ours.pop(unit, None)
            self.sysacc.pop(unit, None)
            self.blocked.discard(unit)
            self.hung.discard(unit)

    def latido(self, unit):
        with self.lock:
            self.hung.discard(unit)

    def roll(self):
        if today() == self.day:
            return
        with self.lock:
            self.day = today()
            self.ours, self.sysacc = {}, {}
            to_start, self.blocked = list(self.blocked), set()
        for unit in to_start:
            b = find_bot(unit)
            if b and bot_activado(unit):
                run(["sudo", "-n", "systemctl", "start", unit], timeout=60)
                aviso("🔄", f"Nuevo día: vuelvo a arrancar {b.get('name') or unit}", 3,
                      bot=b.get("name") or unit, cat=b.get("cat", "otros"), tipo="reinicio")

    def _block(self, b, why):
        unit, name = b["unit"], b.get("name") or b["unit"]
        with self.lock:
            if unit in self.blocked:
                return
            self.blocked.add(unit)
        run(["sudo", "-n", "systemctl", "stop", unit], timeout=60)
        aviso("⛔", f"{name}: {why}. Ha llegado al límite de {b.get('max_restarts', 3)} reinicios hoy; "
                    f"lo dejo parado hasta mañana o hasta que lo arranques tú.", 5,
              bot=name, cat=b.get("cat", "otros"), tipo="bloqueado")

    def _restart(self, b, why):
        unit, name = b["unit"], b.get("name") or b["unit"]
        mx = int(b.get("max_restarts", 3))
        if self.total(unit) >= mx:
            return self._block(b, why)
        code, out, err = restart_unit(unit)
        with self.lock:
            self.ours[unit] = self.ours.get(unit, 0) + 1
            n = self.ours[unit] + self.sysacc.get(unit, 0)
        if code == 0:
            aviso("🔄", f"{name}: {why}. Reiniciado automáticamente ({n}/{mx}).", 4,
                  bot=name, cat=b.get("cat", "otros"), tipo="reinicio")
        else:
            aviso("❌", f"{name}: {why}. No se pudo reiniciar: {(err or out).strip()[:200]}", 5,
                  bot=name, cat=b.get("cat", "otros"), tipo="fallo")

    def check(self, b):
        unit = b["unit"]
        st = systemctl_show(unit, "ActiveState,SubState,NRestarts,UnitFileState")
        active = st.get("ActiveState", "")
        # reinicios de systemd: NRestarts vuelve a 0 cuando la unidad se arranca a mano
        try:
            n = int(st.get("NRestarts") or 0)
        except ValueError:
            n = 0
        with self.lock:
            prev = self.last_n.get(unit)
            if prev is not None:
                delta = n - prev if n >= prev else n
                if delta > 0:
                    self.sysacc[unit] = self.sysacc.get(unit, 0) + delta
            self.last_n[unit] = n
        if not b.get("autorestart", True) or unit in self.blocked:
            return
        if not bot_activado(unit, st.get("UnitFileState")):
            return  # desactivado a propósito: no se toca
        mx = int(b.get("max_restarts", 3))
        if self.total(unit) > mx:
            return self._block(b, "se ha reiniciado demasiadas veces")
        if active == "failed":
            return self._restart(b, "ha fallado")
        lat = LAT.get(unit)
        hang = int(b.get("hang_after", 15))
        if hang > 0 and lat and active == "active" and time.time() - lat > hang * 60 and unit not in self.hung:
            with self.lock:
                self.hung.add(unit)
            mins = int((time.time() - lat) / 60)
            if b.get("autorestart", True):
                LAT.mark(unit)  # le damos margen tras reiniciarlo
                self._restart(b, f"colgado, {mins} min sin señal de vida")
            else:
                name = b.get("name") or unit
                aviso("🥶", f"{name}: colgado, {mins} min sin señal de vida", 5,
                      bot=name, cat=b.get("cat", "otros"), tipo="colgado")


GUARD = Guardian()


# ---------------------------------------------------------------- historial (gráficas)
HIST_PATH = os.path.join(DATA_DIR, "historial.json")
HIST_STEP = 300                  # una muestra cada 5 min
HIST_MAX = 7 * 24 * 3600 // HIST_STEP


class Historial(threading.Thread):
    """Guarda temperatura, CPU y RAM cada 5 minutos durante 7 días. Escribe a la SD cada 30 min."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        d = load_json(HIST_PATH, [])
        self.points = [p for p in d if isinstance(p, list) and len(p) == 4][-HIST_MAX:] if isinstance(d, list) else []
        self.unsaved = 0

    def sample(self):
        p = [int(time.time()), temperature(), cpu_percent(1.0), meminfo()["percent"]]
        with self.lock:
            self.points.append(p)
            self.points = self.points[-HIST_MAX:]
            self.unsaved += 1
        if self.unsaved >= 6:
            self.save()

    def save(self):
        with self.lock:
            data, self.unsaved = list(self.points), 0
        save_json(HIST_PATH, data)

    def serie(self, rango):
        now = time.time()
        span, bucket = (86400, HIST_STEP) if rango != "7d" else (7 * 86400, 1800)
        with self.lock:
            pts = [p for p in self.points if p[0] >= now - span]
        if bucket == HIST_STEP:
            return {"rango": rango, "paso": bucket, "puntos": pts}
        out, cur, acc = [], None, []
        for p in pts:
            k = p[0] - p[0] % bucket
            if cur is not None and k != cur:
                out.append(self._avg(cur, acc))
                acc = []
            cur = k
            acc.append(p)
        if acc:
            out.append(self._avg(cur, acc))
        return {"rango": rango, "paso": bucket, "puntos": out}

    @staticmethod
    def _avg(ts, acc):
        def mean(i, fn=lambda x: sum(x) / len(x)):
            vals = [p[i] for p in acc if p[i] is not None]
            return round(fn(vals), 1) if vals else None
        return [ts, mean(1, max), mean(2), mean(3)]  # temperatura: la máxima del tramo

    def dia(self, fecha):
        t0, t1 = day_bounds(fecha)
        with self.lock:
            pts = [p for p in self.points if t0 <= p[0] < t1]
        temps = [p[1] for p in pts if p[1] is not None]
        cpus = [p[2] for p in pts if p[2] is not None]
        return {"muestras": len(pts),
                "temp_max": max(temps) if temps else None,
                "cpu_media": round(sum(cpus) / len(cpus), 1) if cpus else None,
                "cobertura": round(100 * len(pts) / (86400 / HIST_STEP), 1)}

    def run(self):
        time.sleep(20)
        while True:
            t0 = time.time()
            try:
                self.sample()
            except Exception as e:
                print("historial:", e, flush=True)
            time.sleep(max(5, HIST_STEP - (time.time() - t0)))


HIST = Historial()


# ---------------------------------------------------------------- disco por bot
class Disco(threading.Thread):
    """Calcula cada 6 h cuánto ocupa la carpeta de cada bot (du es lento en la SD:
    va en segundo plano y con la mínima prioridad)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.result = {"calculando": True}
        self.wake = threading.Event()

    def measure(self):
        du, pct = disk_percent()
        groups = {}
        for b in CFG["bots"]:
            wd = bot_workdir(b)
            if wd:
                groups.setdefault(wd, []).append(b.get("name") or b["unit"])
        items = []
        for wd, names in groups.items():
            code, out, _ = run(["nice", "-n", "19", "ionice", "-c", "3", "du", "-sb", "--one-file-system", wd],
                               timeout=1800)
            try:
                size = int(out.split()[0])
            except (IndexError, ValueError):
                size = None
            items.append({"nombre": " + ".join(names), "ruta": wd, "bytes": size})
        _, out, _ = run(["journalctl", "--disk-usage"], timeout=20)
        m = re.search(r"take up ([\d.]+)([KMGT]?)B?", out)
        if m:
            mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
            items.append({"nombre": "logs del sistema", "ruta": "journal", "bytes": int(float(m.group(1)) * mult)})
        items.sort(key=lambda x: x["bytes"] or 0, reverse=True)
        known = sum(i["bytes"] or 0 for i in items)
        items.append({"nombre": "sistema y otros", "ruta": "", "bytes": max(0, du.used - known)})
        self.result = {"ts": int(time.time()), "total": du.total, "used": du.used, "percent": pct,
                       "aviso": CFG["alerts"]["disk"], "items": items}

    def run(self):
        time.sleep(120)
        while True:
            try:
                self.measure()
            except Exception as e:
                print("disco:", e, flush=True)
            self.wake.wait(6 * 3600)
            self.wake.clear()


DISCO = Disco()


# ---------------------------------------------------------------- tailscale, git
def tailscale_status():
    code, out, err = run(["tailscale", "status", "--json"], timeout=10)
    if code != 0:
        return {"ok": False, "error": (err or out).strip()[:300] or "tailscale no responde"}
    try:
        d = json.loads(out)
    except ValueError:
        return {"ok": False, "error": "respuesta no válida de tailscale"}
    me = d.get("Self") or {}
    peers = list((d.get("Peer") or {}).values())
    _, ver, _ = run(["tailscale", "version"], timeout=5)
    return {"ok": True, "estado": d.get("BackendState"), "nombre": me.get("HostName"),
            "ips": me.get("TailscaleIPs") or [], "online": me.get("Online"),
            "version": (ver.splitlines() or [""])[0],
            "dispositivos": [{"nombre": p.get("HostName"), "online": p.get("Online"),
                              "visto": p.get("LastSeen"), "so": p.get("OS")} for p in peers]}


def git(wd, *args, timeout=30):
    return run(["git", "-C", wd, *args], timeout=timeout)


def bot_version(b, fetch=False):
    wd = bot_workdir(b)
    if not wd:
        return {"git": False, "error": "No encuentro la carpeta del bot"}
    code, out, _ = git(wd, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return {"git": False, "ruta": wd, "error": "La carpeta del bot no es un repositorio git"}
    _, ver, _ = git(wd, "describe", "--tags", "--always", "--dirty")
    _, ct, _ = git(wd, "log", "-1", "--format=%ct")
    info = {"git": True, "ruta": wd, "version": ver.strip(),
            "fecha": int(ct.strip() or 0) or None}
    if fetch:
        code, _, err = git(wd, "fetch", "--quiet", timeout=60)
        if code != 0:
            info["error_fetch"] = err.strip()[:300]
    code, behind, _ = git(wd, "rev-list", "--count", "HEAD..@{u}")
    if code == 0:
        info["pendientes"] = int(behind.strip() or 0)
        _, up, _ = git(wd, "describe", "--tags", "--always", "@{u}")
        info["version_remota"] = up.strip()
    else:
        info["pendientes"] = None
        info["error_upstream"] = "La rama no sigue a ningún remoto"
    return info


# ---------------------------------------------------------------- resumen diario
RESUMENES_PATH = os.path.join(DATA_DIR, "resumenes.json")


def resumen(fecha):
    t0, t1 = day_bounds(fecha)
    evs = EV.entre(t0, t1)
    por_bot = {}
    for e in evs:
        if e.get("tipo") != "bot":
            continue
        k = e.get("bot") or "?"
        r = por_bot.setdefault(k, {"bot": k, "cat": e.get("cat", "otros"), "eventos": 0, "ultimos": []})
        r["eventos"] += 1
        r["ultimos"] = (r["ultimos"] + [e["titulo"]])[-3:]
    tipos = {}
    for e in evs:
        tipos[e.get("tipo", "aviso")] = tipos.get(e.get("tipo", "aviso"), 0) + 1
    return {
        "host": HOST, "fecha": fecha, "total": len(evs),
        "eventos_bots": sum(r["eventos"] for r in por_bot.values()),
        "fallos": tipos.get("fallo", 0) + tipos.get("bloqueado", 0) + tipos.get("colgado", 0),
        "reinicios": tipos.get("reinicio", 0),
        "por_bot": sorted(por_bot.values(), key=lambda r: -r["eventos"]),
        "salud": HIST.dia(fecha),
        "avisos": [e for e in evs if e.get("tipo") in ("aviso", "fallo", "bloqueado", "colgado")
                   and e.get("prio", 3) >= 4][-10:],
    }


def resumen_texto(r):
    partes = [f"{r['eventos_bots']} eventos de bots"]
    if r["fallos"]:
        partes.append(f"{r['fallos']} fallos")
    if r["reinicios"]:
        partes.append(f"{r['reinicios']} reinicios")
    if r["salud"].get("temp_max") is not None:
        partes.append(f"temp. máx {r['salud']['temp_max']:.0f} °C")
    top = ", ".join(f"{b['bot']} ({b['eventos']})" for b in r["por_bot"][:3])
    return "Resumen del día: " + ", ".join(partes) + (f". Más activos: {top}." if top else ".")


class Resumenes:
    def __init__(self):
        d = load_json(RESUMENES_PATH, [])
        self.items = d if isinstance(d, list) else []

    def hecho(self, fecha):
        return any(r.get("fecha") == fecha for r in self.items)

    def check(self):
        cfg = CFG["resumen"]
        if not cfg.get("activo", True) or not HHMM_RE.match(cfg.get("hora", "")):
            return
        fecha = today()
        if self.hecho(fecha) or time.strftime("%H:%M") < cfg["hora"]:
            return
        r = resumen(fecha)
        self.items = (self.items + [r])[-30:]
        save_json(RESUMENES_PATH, self.items)
        aviso("📊", resumen_texto(r), 3, telegram=True, tipo="resumen")

    def get(self, fecha):
        for r in self.items:
            if r.get("fecha") == fecha:
                return r
        return None


RES = Resumenes()


# ---------------------------------------------------------------- vigilancia
def peer_alive(p):
    req = urllib.request.Request(p["url"].rstrip("/") + "/api/ping",
                                 headers={"Authorization": "Bearer " + p.get("token", "")})
    try:
        with urllib.request.urlopen(req, timeout=6):
            return True
    except urllib.error.HTTPError:
        return True  # responde (aunque sea con error): está viva
    except Exception:
        return False


class Monitor(threading.Thread):
    """Cada 30 s revisa esta Pi y avisa solo cuando algo cambia.
    Si Telegram está configurado, además vigila a las otras Pi (la app del móvil ya lo hace por su cuenta)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.state = {}
        self.fails = {}
        self.tick = 0

    def edge(self, key, bad, icono, bad_msg, ok_msg, prio=4, **kw):
        prev = self.state.get(key, False)
        if bad and not prev:
            aviso(icono, bad_msg, prio, **kw)
        elif prev and not bad and ok_msg:
            aviso("✅", ok_msg, 3, **kw)
        self.state[key] = bad

    def check(self):
        a = CFG["alerts"]
        t = temperature()
        if t is not None:
            limit = a["temp"] - (5 if self.state.get("temp") else 0)  # histéresis
            self.edge("temp", t >= limit, "🌡️", f"Temperatura alta ({t} °C)", f"Temperatura normal ({t} °C)")

        th = throttled()
        if th.get("available"):
            self.edge("uv", bool(th["value"] & 1), "⚡", "Bajo voltaje. Revisa la fuente de alimentación.",
                      "La alimentación vuelve a estar bien", 5)

        _, pct = disk_percent()
        self.edge("disk", pct >= a["disk"], "💾", f"SD casi llena: {pct:.0f}%", f"SD al {pct:.0f}%")

        GUARD.roll()
        for b in list(CFG["bots"]):
            try:
                GUARD.check(b)
            except Exception as e:
                print("guardian", b.get("unit"), e, flush=True)

        labels = {b["unit"]: b for b in CFG["bots"]}
        for u in dict.fromkeys(list(CFG["services"]) + list(labels)):
            st = systemctl_show(u, "ActiveState").get("ActiveState", "")
            b = labels.get(u)
            kind = "El bot" if b else "El servicio"
            name = (b.get("name") if b else None) or u
            extra = {"bot": name, "cat": b.get("cat", "otros")} if b else {}
            self.edge("unit:" + u, st == "failed", "❌", f"{kind} {name} ha fallado",
                      f"{name} ya no está en error ({st or '?'})", 5, tipo="fallo", **extra)

        self.tick += 1
        if self.tick % 2 == 0:  # cada minuto
            LAT.save()
            RES.check()
            if telegram_on():
                for p in list(CFG["peers"]):
                    key = "peer:" + p["url"]
                    alive = peer_alive(p)
                    n = 0 if alive else self.fails.get(key, 0) + 1
                    self.fails[key] = n
                    down = (not alive) if self.state.get(key) else n >= a["offline_after"]
                    if down and not self.state.get(key):
                        notify(f"🔴 {p['name']} no responde desde hace {n} min (lo detecta {HOST}). "
                               f"¿Apagada, sin corriente o sin red?")
                    elif self.state.get(key) and not down:
                        notify(f"🟢 {p['name']} vuelve a responder")
                    self.state[key] = down
            hb = CFG.get("heartbeat_url")
            if hb:
                try:
                    urllib.request.urlopen(hb, timeout=10).read()
                except Exception:
                    pass

    def run(self):
        time.sleep(15)
        while True:
            try:
                self.check()
            except Exception as e:
                print("monitor:", e, flush=True)
            time.sleep(30)


def on_start():
    up = float((read("/proc/uptime", "0") or "0").split()[0])
    if CFG["alerts"].get("boot", True) and up < 300:
        aviso("🟢", "Se ha encendido", 3, telegram=False)
        if telegram_on():
            threading.Thread(target=notify_retry, args=(f"🟢 {HOST} se ha encendido",), daemon=True).start()


def guardar_todo():
    try:
        HIST.save()
        LAT.save()
    except Exception as e:
        print("guardar:", e, flush=True)


def on_sigterm(signum, frame):
    try:
        guardar_todo()
        _, state, _ = run(["systemctl", "is-system-running"], timeout=3)
        if state.strip() == "stopping" and CFG["alerts"].get("shutdown", True):
            _, jobs, _ = run(["systemctl", "list-jobs", "--no-legend"], timeout=3)
            what = "reiniciando" if "reboot.target" in jobs else "apagando"
            aviso("🔴", f"Se está {what}", 4)
    finally:
        os._exit(0)


def settings_view():
    return {"hostname": HOST, "telegram": CFG["telegram"], "alerts": CFG["alerts"],
            "heartbeat_url": CFG["heartbeat_url"], "resumen": CFG["resumen"],
            "peers": [{"name": p["name"], "url": p["url"]} for p in CFG["peers"]]}


# ---------------------------------------------------------------- HTTP
MANIFEST = {
    "name": "Pi Monitor", "short_name": "Pi Monitor", "start_url": "/",
    "display": "standalone", "background_color": "#0f1115", "theme_color": "#0f1115",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#0f1115"/>'
        '<rect x="14" y="16" width="36" height="32" rx="5" fill="none" stroke="#4ade80" stroke-width="4"/>'
        '<path d="M20 34h6l3-7 5 13 3-6h7" fill="none" stroke="#4ade80" stroke-width="3.5" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>')

LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
# rutas que los bots pueden usar desde la propia Pi sin token
LOCAL_GET = ("/api/horario",)
LOCAL_POST = ("/api/evento", "/api/latido")


class Handler(BaseHTTPRequestHandler):
    server_version = "pi-agent/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def is_local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def authed(self, local_ok=False):
        if local_ok and self.is_local():
            return True
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else ""
        if tok and hmac.compare_digest(tok, CFG["token"]):
            return True
        time.sleep(1)
        self.send(401, {"error": "Token incorrecto"})
        return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            d = json.loads(self.rfile.read(min(n, 100_000)))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        arg = lambda k, d="": (q.get(k) or [d])[0]
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self.send(404, "index.html no encontrado", "text/plain")
        if u.path == "/manifest.webmanifest":
            return self.send(200, MANIFEST, "application/manifest+json")
        if u.path == "/icon.svg":
            return self.send(200, ICON, "image/svg+xml")
        if not u.path.startswith("/api/"):
            return self.send(404, {"error": "no existe"})
        if not self.authed(local_ok=u.path in LOCAL_GET):
            return
        if u.path == "/api/ping":
            return self.send(200, {"ok": True, "hostname": HOST, "version": VERSION})
        if u.path == "/api/status":
            return self.send(200, status())
        if u.path == "/api/units":
            return self.send(200, {"units": list_units()})
        if u.path == "/api/settings":
            return self.send(200, settings_view())
        if u.path == "/api/eventos":
            raw = arg("desde")
            try:
                desde = int(raw) if raw != "" else None
            except ValueError:
                desde = None
            return self.send(200, EV.since(desde))
        if u.path == "/api/historial":
            return self.send(200, HIST.serie(arg("rango", "24h")))
        if u.path == "/api/disco":
            return self.send(200, DISCO.result)
        if u.path == "/api/tailscale":
            return self.send(200, tailscale_status())
        if u.path == "/api/resumen":
            fecha = arg("fecha") or today()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", fecha):
                return self.send(400, {"error": "fecha no válida (AAAA-MM-DD)"})
            r = RES.get(fecha) if fecha != today() else None
            return self.send(200, r or resumen(fecha))
        if u.path == "/api/bots/version":
            b = find_bot(arg("unit"))
            if not b:
                return self.send(404, {"error": "No existe ese bot"})
            return self.send(200, bot_version(b, fetch=arg("fetch") == "1"))
        if u.path == "/api/horario":
            b = find_bot(arg("bot") or arg("unit"))
            if not b:
                return self.send(404, {"error": "No existe ese bot"})
            return self.send(200, {"unit": b["unit"], "horario": b.get("horario") or {}})
        if u.path == "/api/logs":
            unit = arg("unit")
            try:
                n = max(10, min(int(arg("n", "100")), 2000))
            except ValueError:
                n = 100
            prio = arg("priority")
            cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(n)]
            if unit:
                if not UNIT_RE.match(unit):
                    return self.send(400, {"error": "unidad no válida"})
                cmd += ["-u", unit]
            if prio in ("err", "warning"):
                cmd += ["-p", prio, "-b"]
            code, out, err = run(cmd, timeout=15)
            return self.send(200, {"code": code, "output": out or err})
        return self.send(404, {"error": "no existe"})

    # ------------------------------------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        if not self.authed(local_ok=u.path in LOCAL_POST):
            return
        data = self.body()
        route = {
            "/api/exec": self.p_exec, "/api/action": self.p_action,
            "/api/services": self.p_services, "/api/settings": self.p_settings,
            "/api/notify/test": self.p_notify_test, "/api/telegram/detect": self.p_tg_detect,
            "/api/bots/save": self.p_bot_save, "/api/bots/link": self.p_bot_link,
            "/api/bots/remove": self.p_bot_remove,
            "/api/bots/ajustes": self.p_bot_ajustes, "/api/bots/horario": self.p_bot_horario,
            "/api/bots/update": self.p_bot_update,
            "/api/evento": self.p_evento, "/api/latido": self.p_latido,
            "/api/limpiar-logs": self.p_limpiar_logs, "/api/disco/recalcular": self.p_disco_recalc,
        }.get(u.path)
        if not route:
            return self.send(404, {"error": "no existe"})
        return route(data)

    def p_exec(self, data):
        if not CFG["allow_exec"]:
            return self.send(403, {"error": "La terminal está desactivada en esta Pi (allow_exec=false)"})
        cmd = str(data.get("cmd", "")).strip()
        if not cmd:
            return self.send(400, {"error": "comando vacío"})
        t0 = time.time()
        code, out, err = run(cmd, timeout=int(CFG["exec_timeout"]), shell=True, cwd=os.path.expanduser("~"))
        return self.send(200, {"code": code, "stdout": out, "stderr": err,
                               "seconds": round(time.time() - t0, 2)})

    def p_action(self, data):
        action = data.get("action")
        unit = str(data.get("unit", ""))
        # "desactivar" = parar + que no vuelva a arrancar (servicio y timer). enable/disable de la app
        # antigua se tratan igual, para que el interruptor haga lo que se espera.
        alias = {"disable": "off", "enable": "on", "desactivar": "off", "activar": "on"}
        action = alias.get(action, action)
        if action in ("restart", "start", "stop", "on", "off"):
            if not UNIT_RE.match(unit):
                return self.send(400, {"error": "unidad no válida"})
            if action in ("on", "off"):
                cmd = ["sudo", "-n", HELPER, action, unit]
            else:
                cmd = ["sudo", "-n", "systemctl", action, unit]
            code, out, err = run(cmd, timeout=90)
            b = find_bot(unit)
            if code == 0 and b:
                if action in ("on", "start", "restart"):
                    GUARD.reset(unit)
                if action in ("on", "off"):
                    b["autostart"] = action == "on"
                    save_config(CFG)
                    name = b.get("name") or unit
                    aviso("⏸️" if action == "off" else "▶️",
                          f"{name} {'desactivado' if action == 'off' else 'activado'} desde la app", 3,
                          telegram=False, bot=name, cat=b.get("cat", "otros"), tipo="control")
            return self.send(200, {"code": code, "output": (out + err).strip() or "Hecho",
                                   "service": unit_info(unit),
                                   "activado": bot_activado(unit) if code == 0 else None})
        if action in ("reboot", "poweroff"):
            if not sudo_puede("systemctl", action):
                return self.send(500, {"error": "sudo pide contraseña; revisa /etc/sudoers.d/pi-agent"})
            guardar_todo()
            subprocess.Popen(["/bin/bash", "-c", f"sleep 2; sudo -n systemctl {action}"],
                             start_new_session=True)
            return self.send(200, {"code": 0, "output": "Orden enviada"})
        return self.send(400, {"error": "acción desconocida"})

    def p_services(self, data):
        svcs = data.get("services")
        if not isinstance(svcs, list) or not all(isinstance(s, str) and UNIT_RE.match(s) for s in svcs):
            return self.send(400, {"error": "lista no válida"})
        CFG["services"] = list(dict.fromkeys(svcs))
        save_config(CFG)
        return self.send(200, {"services": CFG["services"]})

    def p_settings(self, data):
        if isinstance(data.get("telegram"), dict):
            tg = data["telegram"]
            CFG["telegram"] = {"token": str(tg.get("token", "")).strip(),
                               "chat_id": str(tg.get("chat_id", "")).strip()}
        if isinstance(data.get("alerts"), dict):
            a, cur = data["alerts"], CFG["alerts"]
            for k in ("temp", "disk", "offline_after"):
                if k in a:
                    try:
                        cur[k] = max(1, min(int(a[k]), 100))
                    except (TypeError, ValueError):
                        pass
            for k in ("boot", "shutdown"):
                if k in a:
                    cur[k] = bool(a[k])
        if isinstance(data.get("resumen"), dict):
            r, cur = data["resumen"], CFG["resumen"]
            if "activo" in r:
                cur["activo"] = bool(r["activo"])
            if "hora" in r:
                if not HHMM_RE.match(str(r["hora"])):
                    return self.send(400, {"error": "Hora del resumen no válida (usa HH:MM)"})
                cur["hora"] = str(r["hora"])
        if isinstance(data.get("peers"), list):
            peers = []
            for p in data["peers"]:
                if isinstance(p, dict) and str(p.get("url", "")).startswith(("http://", "https://")):
                    url = str(p["url"]).rstrip("/")
                    old = next((x for x in CFG["peers"] if x["url"] == url), {})
                    peers.append({"name": str(p.get("name") or url)[:40], "url": url,
                                  "token": str(p.get("token") or old.get("token", ""))})
            CFG["peers"] = peers
        if "heartbeat_url" in data:
            hb = str(data.get("heartbeat_url") or "").strip()
            if hb and not hb.startswith(("http://", "https://")):
                return self.send(400, {"error": "La URL de aviso debe empezar por https://"})
            CFG["heartbeat_url"] = hb
        save_config(CFG)
        return self.send(200, settings_view())

    def p_notify_test(self, data):
        EV.add(f"🔔 {HOST}", "Prueba: las notificaciones funcionan", 4)
        tg_ok, tg_msg = notify(f"🔔 Prueba de Pi Monitor desde {HOST}: las notificaciones funcionan")
        return self.send(200, {"ok": True, "telegram": telegram_on(), "telegram_ok": tg_ok,
                               "telegram_error": "" if tg_ok else tg_msg})

    def p_tg_detect(self, data):
        tok = str(data.get("token") or CFG["telegram"].get("token", "")).strip()
        if not tok:
            return self.send(400, {"error": "Pon primero el token del bot"})
        try:
            r = http_json(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10)
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                return self.send(400, {"error": "El token del bot no es válido"})
            if e.code == 409:
                return self.send(400, {"error": "Otro programa está leyendo ese bot. Usa un bot nuevo o escribe el chat id a mano."})
            return self.send(400, {"error": f"Telegram respondió {e.code}"})
        except Exception as e:
            return self.send(502, {"error": f"No se pudo contactar con Telegram: {e}"})
        chats = {}
        for upd in r.get("result", []):
            m = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
            c = m.get("chat")
            if c:
                name = c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")]))
                chats[str(c["id"])] = name or c.get("username") or str(c["id"])
        return self.send(200, {"chats": [{"id": k, "name": v} for k, v in chats.items()]})

    def p_bot_save(self, data):
        name = str(data.get("name", "")).strip()[:60]
        command = str(data.get("command", "")).strip()
        if not name or not command:
            return self.send(400, {"error": "Pon un nombre y un comando"})
        unit = str(data.get("unit") or "")
        if unit:
            existing = next((b for b in CFG["bots"] if b["unit"] == unit), None)
            if not existing or not existing.get("managed"):
                return self.send(400, {"error": "Ese bot no se creó desde la app"})
        else:
            existing = None
            slug = slugify(name)
            if not slug:
                return self.send(400, {"error": "Usa letras o números en el nombre"})
            unit = f"bot-{slug}.service"
            if any(b["unit"] == unit for b in CFG["bots"]):
                return self.send(400, {"error": "Ya hay un bot con ese nombre"})
        spec = {
            "description": f"Bot {name} (Pi Monitor)", "command": command,
            "workdir": str(data.get("workdir") or "").strip(),
            "restart": "on-failure" if data.get("restart", True) else "no",
            "schedule": str(data.get("schedule") or "").strip(),
            "autostart": bool(data.get("autostart")),
        }
        code, out, err = run(["sudo", "-n", HELPER, "write", unit[:-8]], timeout=60, input=json.dumps(spec))
        if code != 0:
            msg = (err or out).strip()
            if "password" in msg or "contraseña" in msg:
                msg = "Faltan permisos: vuelve a ejecutar install.sh en esta Pi"
            return self.send(400, {"error": msg or "No se pudo crear el bot"})
        entry = {**BOT_DEFAULTS, **(existing or {}), "unit": unit, "name": name, "managed": True,
                 **{k: spec[k] for k in ("command", "workdir", "restart", "schedule", "autostart")}}
        if "cat" in data and data["cat"] in CATS:
            entry["cat"] = data["cat"]
        CFG["bots"] = [b for b in CFG["bots"] if b["unit"] != unit] + [entry]
        save_config(CFG)
        return self.send(200, {"ok": True, "unit": unit})

    def p_bot_link(self, data):
        unit = str(data.get("unit", "")).strip()
        if not UNIT_RE.match(unit):
            return self.send(400, {"error": "servicio no válido"})
        if any(b["unit"] == unit for b in CFG["bots"]):
            return self.send(400, {"error": "Ese servicio ya está en tus bots"})
        cat = data.get("cat") if data.get("cat") in CATS else "otros"
        CFG["bots"].append({**BOT_DEFAULTS, "unit": unit, "name": str(data.get("name") or unit)[:60],
                            "managed": False, "cat": cat})
        save_config(CFG)
        return self.send(200, {"ok": True})

    def p_bot_remove(self, data):
        unit = str(data.get("unit", ""))
        bot = next((b for b in CFG["bots"] if b["unit"] == unit), None)
        if not bot:
            return self.send(404, {"error": "No existe ese bot"})
        if data.get("delete") and bot.get("managed"):
            code, out, err = run(["sudo", "-n", HELPER, "remove", unit[:-8]], timeout=60)
            if code != 0:
                return self.send(400, {"error": (err or out).strip() or "No se pudo borrar"})
        CFG["bots"] = [b for b in CFG["bots"] if b["unit"] != unit]
        save_config(CFG)
        return self.send(200, {"ok": True})

    def p_bot_ajustes(self, data):
        """Ajustes de vigilancia de un bot: nombre, categoría, autorreinicio, límite y minutos para 'colgado'."""
        b = find_bot(data.get("unit"))
        if not b:
            return self.send(404, {"error": "No existe ese bot"})
        if "name" in data and str(data["name"]).strip():
            b["name"] = str(data["name"]).strip()[:60]
        if "cat" in data:
            if data["cat"] not in CATS:
                return self.send(400, {"error": "categoría no válida"})
            b["cat"] = data["cat"]
        if "autorestart" in data:
            b["autorestart"] = bool(data["autorestart"])
        for k, lo, hi in (("max_restarts", 0, 20), ("hang_after", 0, 1440)):
            if k in data:
                try:
                    b[k] = max(lo, min(int(data[k]), hi))
                except (TypeError, ValueError):
                    return self.send(400, {"error": f"{k} no válido"})
        if "puerto" in data:
            if data["puerto"] in (None, "", 0):
                b.pop("puerto", None)  # vuelve a usar el detectado
            else:
                try:
                    p = int(data["puerto"])
                    if not 1 <= p <= 65535:
                        raise ValueError
                    b["puerto"] = p
                except (TypeError, ValueError):
                    return self.send(400, {"error": "Puerto no válido (1-65535)"})
        save_config(CFG)
        return self.send(200, {"ok": True, "bot": bot_info(b)})

    def p_bot_horario(self, data):
        """Horario de un bot: horas HH:MM y días (0 = lunes … 6 = domingo). Lo leerá el bot con pihub.py."""
        b = find_bot(data.get("unit"))
        if not b:
            return self.send(404, {"error": "No existe ese bot"})
        horas = data.get("horas", [])
        dias = data.get("dias", list(range(7)))
        if not isinstance(horas, list) or not all(isinstance(h, str) and HHMM_RE.match(h) for h in horas):
            return self.send(400, {"error": "Horas no válidas (usa HH:MM)"})
        if not isinstance(dias, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in dias):
            return self.send(400, {"error": "Días no válidos (0 = lunes … 6 = domingo)"})
        h = {"horas": sorted(set(horas)), "dias": sorted(set(dias)), "cambiado": int(time.time())}
        if "intervalo_min" in data:
            try:
                h["intervalo_min"] = max(1, min(int(data["intervalo_min"]), 1440))
            except (TypeError, ValueError):
                return self.send(400, {"error": "Intervalo no válido"})
        b["horario"] = h
        save_config(CFG)
        return self.send(200, {"ok": True, "horario": h})

    def p_bot_update(self, data):
        b = find_bot(data.get("unit"))
        if not b:
            return self.send(404, {"error": "No existe ese bot"})
        antes = bot_version(b)
        if not antes.get("git"):
            return self.send(400, {"error": antes.get("error", "No se puede actualizar")})
        wd = antes["ruta"]
        code, out, err = git(wd, "pull", "--ff-only", timeout=180)
        if code != 0:
            return self.send(400, {"error": "git pull falló: " + (err or out).strip()[:500]})
        despues = bot_version(b)
        cambiado = despues.get("version") != antes.get("version")
        name = b.get("name") or b["unit"]
        if cambiado:
            rc, rout, rerr = restart_unit(b["unit"])
            GUARD.reset(b["unit"])
            aviso("⬆️", f"{name} actualizado: {antes.get('version')} → {despues.get('version')}", 3,
                  telegram=False, bot=name, cat=b.get("cat", "otros"), tipo="control")
            if rc != 0:
                return self.send(200, {"ok": True, "cambiado": True, "antes": antes, "despues": despues,
                                       "aviso": "Actualizado, pero no se pudo reiniciar: " + (rerr or rout).strip()})
        return self.send(200, {"ok": True, "cambiado": cambiado, "antes": antes, "despues": despues,
                               "salida": out.strip()[-2000:]})

    def p_evento(self, data):
        """Un bot avisa de algo: {"bot": "wallabot", "titulo": "...", "msg": "...", "prio": 3}."""
        b = find_bot(data.get("bot") or data.get("unit"))
        titulo = str(data.get("titulo") or "").strip()[:200]
        if not titulo:
            return self.send(400, {"error": "Falta el título"})
        name = (b.get("name") if b else None) or str(data.get("bot") or "?")[:60]
        cat = (b.get("cat") if b else None) or (data.get("cat") if data.get("cat") in CATS else "otros")
        if not EV.permitido(name):
            return self.send(429, {"error": "Demasiados eventos seguidos"})
        try:
            prio = max(1, min(int(data.get("prio", 3)), 5))
        except (TypeError, ValueError):
            prio = 3
        if b:
            LAT.mark(b["unit"])
            GUARD.latido(b["unit"])
        ev = EV.add(titulo, str(data.get("msg") or "")[:1000], prio, bot=name, cat=cat, tipo="bot")
        return self.send(200, {"ok": True, "id": ev["id"]})

    def p_latido(self, data):
        b = find_bot(data.get("bot") or data.get("unit"))
        if not b:
            return self.send(404, {"error": "Ese bot no está enlazado en pi-agent"})
        LAT.mark(b["unit"])
        GUARD.latido(b["unit"])
        return self.send(200, {"ok": True})

    def p_limpiar_logs(self, data):
        try:
            dias = max(1, min(int(data.get("dias", 7)), 90))
        except (TypeError, ValueError):
            dias = 7
        code, out, err = run(["sudo", "-n", HELPER, "vacuum", str(dias)], timeout=120)
        if code != 0:
            return self.send(400, {"error": (err or out).strip() or "No se pudieron limpiar los logs"})
        DISCO.wake.set()
        return self.send(200, {"ok": True, "output": (out + err).strip()})

    def p_disco_recalc(self, data):
        DISCO.wake.set()
        return self.send(200, {"ok": True})


def main():
    if not CFG.get("token") or len(CFG["token"]) < 16:
        raise SystemExit("Falta un token de al menos 16 caracteres en " + CONFIG_PATH)
    srv = ThreadingHTTPServer((CFG["bind"], int(CFG["port"])), Handler)
    signal.signal(signal.SIGTERM, on_sigterm)
    Monitor().start()
    HIST.start()
    DISCO.start()
    on_start()
    print(f"pi-agent {VERSION} escuchando en {CFG['bind']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
