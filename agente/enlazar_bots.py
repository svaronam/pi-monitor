#!/usr/bin/env python3
"""
Enlaza en /etc/pi-agent/config.json los bots que ya tienes instalados como servicios,
con su nombre y su categoría para el feed. Solo añade los que existen en esta Pi y no
toca los que ya estaban. Se puede ejecutar las veces que haga falta.

  sudo python3 enlazar_bots.py [/etc/pi-agent/config.json]
"""
import json
import os
import subprocess
import sys

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "/etc/pi-agent/config.json"

# unidad -> (nombre en la app, categoría del feed)
CONOCIDOS = {
    "spotisync.service": ("spotisync", "musica"),
    "wallabot.service": ("wallabot", "wallapop"),
    "wallapop.service": ("wallapop (servidor y republicar)", "wallapop"),
    "diecast-radar.service": ("diecast-radar", "diecast"),
    "insta-bot.service": ("instagram", "otros"),
    "oporadar.service": ("oporadar", "opos"),
    "opotest.service": ("opotest", "opos"),
    "recetabot.service": ("recetabot", "otros"),
    "gsialerta.service": ("gsi-alerta", "opos"),
}
BOT_DEFAULTS = {"managed": False, "autorestart": True, "max_restarts": 3, "hang_after": 15}


def existe(unit):
    r = subprocess.run(["systemctl", "show", unit, "--property=LoadState"], capture_output=True, text=True)
    return "LoadState=loaded" in r.stdout


def main():
    st = os.stat(CONFIG)
    with open(CONFIG) as f:
        cfg = json.load(f)
    bots = cfg.setdefault("bots", [])
    ya = {b["unit"] for b in bots}
    nuevos = []
    for unit, (name, cat) in CONOCIDOS.items():
        if unit in ya or not existe(unit):
            continue
        bots.append({**BOT_DEFAULTS, "unit": unit, "name": name, "cat": cat})
        nuevos.append(name)
    if not nuevos:
        print("  No hay bots nuevos que enlazar.")
        return
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.chown(tmp, st.st_uid, st.st_gid)  # el agente corre como sergio: el fichero sigue siendo suyo
    os.replace(tmp, CONFIG)
    print("  Bots enlazados: " + ", ".join(nuevos))


if __name__ == "__main__":
    main()
