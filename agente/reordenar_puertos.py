#!/usr/bin/env python3
"""Reordena los puertos de los bots de una Raspberry.

Uso (en cada Pi):
    sudo python3 reordenar_puertos.py            # simulacro: solo enseña lo que cambiaría
    sudo python3 reordenar_puertos.py --aplicar  # lo hace de verdad

Por cada puerto viejo busca qué servicio escucha en él (ss + cgroup), localiza dónde está
escrito el puerto (unidad systemd, EnvironmentFile y archivos de configuración de la carpeta
del bot), lo cambia, reinicia el servicio y comprueba que escucha en el nuevo. Si no arranca,
deshace los cambios de ese bot y se para. Guarda copia de todo en /var/backups/.

Lo que NO puede hacer: cambiar la dirección guardada en las apps Android de cada bot.
Al final imprime la lista de apps que tienes que tocar.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

# puerto viejo -> puerto nuevo. homelab1 en el 879x y homelab2 en el 880x; el 8790 es del Pi Monitor.
# Bots nuevos ya instalados en su sitio (no hace falta moverlos): gsi-alerta en homelab2:8806.
PLANES = {
    "homelab1": {8787: 8791, 8788: 8792, 8791: 8793, 8796: 8794},
    "homelab2": {8791: 8801, 8792: 8802, 8795: 8803, 8899: 8804, 8080: 8805},
}
PI_AGENT_CFG = "/etc/pi-agent/config.json"

SALTAR_DIRS = {".git", "venv", ".venv", "env", "__pycache__", "node_modules", "site-packages",
               "logs", "log", "backups", "backup", "cache", ".cache", "build", "dist", "android", "extension"}
EXT_OK = {".py", ".yaml", ".yml", ".json", ".env", ".toml", ".ini", ".cfg", ".conf", ".sh", ".service",
          ".js", ".html", ""}
MAX_BYTES = 256 * 1024
# Solo se cambia el número cuando va pegado a algo con pinta de puerto, para no tocar precios o ids:
#   port: 8791 · "puerto": 8787 · WALLABOT_PORT=8788 · port=8788 · --port 8796 · get("port", 8791) · 0.0.0.0:8787
def patrones(viejo):
    n = rf"(?<!\d){viejo}(?!\d)"
    return [
        re.compile(rf"(?i)((?:port|puerto|listen)\w*[\"']?\s*[:=]\s*[\"']?){n}"),
        re.compile(rf"(?i)(--port[=\s]+|\s-p\s+){n}"),
        re.compile(rf"(?i)([\"']port[\"']\s*,\s*){n}"),
        re.compile(rf"(\w:|\]:|:-){n}"),
    ]


def sh(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def escuchando():
    """{puerto: servicio} de todo lo que escucha por TCP."""
    _, out, _ = sh(["ss", "-Hltnp"])
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        port = parts[3].rpartition(":")[2]
        if not port.isdigit():
            continue
        unit = None
        for pid in set(re.findall(r"pid=(\d+)", line)):
            try:
                cg = open(f"/proc/{pid}/cgroup").read()
            except OSError:
                continue
            m = re.search(r"/([A-Za-z0-9@._:-]+\.service)", cg)
            if m:
                unit = m.group(1)
        res.setdefault(int(port), unit)
    return res


def props(unit):
    _, out, _ = sh(["systemctl", "show", unit, "-p", "FragmentPath,DropInPaths,WorkingDirectory,EnvironmentFiles,ExecStart"])
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def carpeta_bot(p):
    wd = p.get("WorkingDirectory", "").lstrip("!-")
    if wd and os.path.isdir(wd):
        return wd
    # sin WorkingDirectory: la carpeta del script que arranca (python3 /home/sergio/bot/run.py)
    for tok in re.findall(r"(/[^\s;]+)", p.get("ExecStart", "")):
        if tok.endswith((".py", ".sh")) and os.path.isfile(tok):
            return os.path.dirname(tok)
    return None


def candidatos(p):
    files = []
    for k in ("FragmentPath",):
        if p.get(k) and os.path.isfile(p[k]):
            files.append(p[k])
    files += [f for f in p.get("DropInPaths", "").split() if os.path.isfile(f)]
    for f in re.findall(r"(/[^\s()]+)", p.get("EnvironmentFiles", "")):
        if os.path.isfile(f):
            files.append(f)
    base = carpeta_bot(p)
    if base:
        base_depth = base.rstrip("/").count("/")
        for root, dirs, fs in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SALTAR_DIRS and not d.startswith(".")
                       and root.count("/") - base_depth < 4]
            for f in fs:
                path = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()
                if f.startswith(".env") or ext in EXT_OK:
                    try:
                        if os.path.getsize(path) <= MAX_BYTES and not os.path.islink(path):
                            files.append(path)
                    except OSError:
                        pass
    return list(dict.fromkeys(files)), base


def buscar(files, viejo):
    pats = patrones(viejo)
    hits = {}
    for f in files:
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = [(i, l) for i, l in enumerate(text.splitlines(), 1) if any(q.search(l) for q in pats)]
        if lines:
            hits[f] = lines
    return hits


def en_git(path):
    d = os.path.dirname(path)
    if not shutil.which("git"):
        return False
    code, _, _ = sh(["git", "-C", d, "ls-files", "--error-unmatch", path])
    return code == 0


def cambiar(path, viejo, nuevo):
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    for q in patrones(viejo):
        text = q.sub(lambda m: m.group(1) + str(nuevo), text)
    with open(path, "w", encoding="utf-8", newline="") as fh:  # mismo archivo: conserva dueño y permisos
        fh.write(text)


def respaldar(path, backup_dir):
    dst = os.path.join(backup_dir, path.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(path, dst)
    return dst


def espera_puerto(unit, puerto, segundos=40):
    fin = time.time() + segundos
    while time.time() < fin:
        if escuchando().get(puerto) == unit:
            return True
        time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true", help="hacer los cambios (sin esto es un simulacro)")
    ap.add_argument("--pi", help="nombre de la Pi si no coincide con el hostname (homelab1 / homelab2)")
    a = ap.parse_args()

    pi = (a.pi or socket.gethostname()).lower()
    if pi not in PLANES:
        sys.exit(f"No tengo plan para «{pi}». Usa --pi homelab1 o --pi homelab2.")
    if a.aplicar and os.geteuid() != 0:
        sys.exit("Ejecútalo con sudo.")
    plan = PLANES[pi]
    ahora = escuchando()

    print(f"== {pi}: {'APLICANDO' if a.aplicar else 'SIMULACRO (no cambia nada)'}\n")
    tareas, problemas = [], []
    for viejo, nuevo in plan.items():
        unit = ahora.get(viejo)
        if viejo not in ahora:
            print(f"-  {viejo} → {nuevo}: nadie escucha en el {viejo}; se salta (¿bot parado?)")
            continue
        if not unit:
            problemas.append(f"{viejo}: escucha algo que no es un servicio systemd")
            continue
        p = props(unit)
        files, base = candidatos(p)
        hits = buscar(files, viejo)
        print(f"●  {unit}: {viejo} → {nuevo}   (carpeta: {base or '¿?'})")
        if not hits:
            print("     ⚠ no encuentro dónde está escrito el puerto: puede que use el valor por defecto del código")
            problemas.append(f"{unit}: no sé dónde cambiar el {viejo}")
        repo = {f for f in hits if en_git(f)}
        # si el puerto está en un archivo de configuración propio, no se toca el código del repo
        # (es solo el valor por defecto, y cambiarlo haría chocar «actualizar bot»)
        usar = [f for f in hits if f not in repo] or list(hits)
        for f, lines in hits.items():
            if f not in usar:
                marca = "  [código del repo: no se toca, manda la configuración]"
            elif f in repo:
                marca = "  [código del repo: se cambia, pero «actualizar bot» chocará; mejor cámbialo también en GitHub]"
            else:
                marca = ""
            print(f"     {f}{marca}")
            for i, l in lines[:6]:
                print(f"       {i:>4}: {l.strip()[:110]}")
        tareas.append({"unit": unit, "viejo": viejo, "nuevo": nuevo, "files": usar if hits else []})

    # orden: no mover un bot a un puerto que otro aún no ha dejado libre (p. ej. 8791 en homelab1)
    pendientes, orden = list(tareas), []
    ocupados = set(ahora)
    while pendientes:
        libre = next((t for t in pendientes if t["nuevo"] not in ocupados), None)
        if not libre:
            sys.exit("Hay un ciclo de puertos que no sé resolver: " + ", ".join(f"{t['viejo']}→{t['nuevo']}" for t in pendientes))
        orden.append(libre)
        pendientes.remove(libre)
        ocupados.discard(libre["viejo"])
        ocupados.add(libre["nuevo"])

    agente = {}
    try:
        cfg = json.load(open(PI_AGENT_CFG))
        agente = {b["unit"]: b.get("puerto") for b in cfg.get("bots", []) if b.get("puerto")}
    except (OSError, ValueError, PermissionError):
        cfg = None
    manuales = [t for t in orden if agente.get(t["unit"]) == t["viejo"]]
    if manuales:
        print("\n   Pi Monitor tiene puesto a mano el puerto de: " + ", ".join(t["unit"] for t in manuales) + " (se actualizará)")

    print("\nOrden: " + ", ".join(f"{t['unit']} {t['viejo']}→{t['nuevo']}" for t in orden))
    if problemas:
        print("\n⚠ Revisa antes de aplicar:\n  - " + "\n  - ".join(problemas))
    if not a.aplicar:
        print("\nNo he tocado nada. Si lo de arriba está bien:  sudo python3 reordenar_puertos.py --aplicar")
        return

    backup = f"/var/backups/reordenar-puertos-{dt.datetime.now():%Y%m%d-%H%M%S}"
    os.makedirs(backup)
    hechos = []
    for t in orden:
        if not t["files"]:
            print(f"\n✗ {t['unit']}: lo salto (no sé dónde está el puerto)")
            continue
        print(f"\n→ {t['unit']}: {t['viejo']} → {t['nuevo']}")
        copias = {f: respaldar(f, backup) for f in t["files"]}
        for f in t["files"]:
            cambiar(f, t["viejo"], t["nuevo"])
        sh(["systemctl", "daemon-reload"])
        sh(["systemctl", "restart", t["unit"]], timeout=90)
        if espera_puerto(t["unit"], t["nuevo"]):
            print(f"  ✓ escucha en el {t['nuevo']}")
            hechos.append(t)
            continue
        print(f"  ✗ no arranca en el {t['nuevo']}: deshago los cambios de este bot y paro aquí")
        for f, c in copias.items():
            shutil.copy2(c, f)
        sh(["systemctl", "daemon-reload"])
        sh(["systemctl", "restart", t["unit"]], timeout=90)
        print(f"    vuelto al {t['viejo']}: {'sí' if espera_puerto(t['unit'], t['viejo']) else 'NO, revisa: journalctl -u ' + t['unit']}")
        break

    if cfg is not None and any(agente.get(t["unit"]) == t["viejo"] for t in hechos):
        respaldar(PI_AGENT_CFG, backup)
        sh(["systemctl", "stop", "pi-agent"])
        cfg = json.load(open(PI_AGENT_CFG))  # releer: el agente puede haberlo guardado al pararse
        for b in cfg.get("bots", []):
            for t in hechos:
                if b["unit"] == t["unit"] and b.get("puerto") == t["viejo"]:
                    b["puerto"] = t["nuevo"]
        with open(PI_AGENT_CFG, "w") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        sh(["systemctl", "start", "pi-agent"])
        print("\n✓ Pi Monitor actualizado con los puertos puestos a mano")

    print(f"\nCopia de todo lo cambiado en {backup}")
    if hechos:
        print("\nAhora cambia la dirección en estas apps del móvil (y en tus marcadores):")
        for t in hechos:
            print(f"  - {t['unit'].removesuffix('.service')}:  http://{pi}:{t['viejo']}  →  http://{pi}:{t['nuevo']}")


if __name__ == "__main__":
    main()
