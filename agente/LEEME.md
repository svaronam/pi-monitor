# Pi Monitor v1.2

App Android para vigilar y controlar tus Raspberry Pi, con avisos nativos en el móvil.

```
agente/        → lo que se instala en cada Pi (agente + página + instalador)
app-android/   → la APK (se compila en GitHub Actions)
.github/       → el flujo que compila la APK al hacer push
```

## 1. Actualizar el agente en cada Pi

```bash
# desde el PC, en esta carpeta
scp -r agente usuario@homelab1:~/pi-monitor
ssh usuario@homelab1 "cd ~/pi-monitor && sudo bash install.sh"
```

Repite con homelab2. Se conservan el token, los bots y la configuración.

## 2. Compilar la APK (igual que Wallapop Control)

1. En GitHub crea un repositorio privado vacío llamado `pi-monitor`.
2. En esta carpeta, que ya es un repositorio git:
   ```bash
   git remote add origin https://github.com/svaronam/pi-monitor.git
   git push -u origin main
   ```
3. Abre GitHub → **Actions** y espera al check verde. Abre la ejecución y descarga el artefacto **PiMonitor-apk**,
   que contiene `app-debug.apk`.
4. Instálala en el móvil.

Cada vez que cambies algo y hagas push, se recompila sola.

## 3. Primer uso

1. Abre Pi Monitor y acepta el permiso de notificaciones.
2. Pulsa **＋** y añade cada Pi con su IP de Tailscale (`http://100.x.x.x:8790`) y su token.
   Si no tienes el token a mano: `sudo cat /etc/pi-agent/config.json`.
3. Quita la restricción de batería para que Android no duerma la app:
   Ajustes → Apps → Pi Monitor → Batería → **Sin restricciones**.
4. Pulsa 🔔 → **Enviar prueba**. Te llegará una notificación en unos segundos.

## Cómo funcionan los avisos

- La app deja una notificación fija mínima («2 Pi · todas responden») y cada 30 s pregunta a cada Pi
  por Tailscale si hay avisos nuevos.
- Avisos que llegan:
  - Bot o servicio que falla.
  - Bajo voltaje, temperatura alta y disco lleno.
  - Pi que se enciende, se apaga o se reinicia.
- **Pi que no responde**: la detecta el propio móvil. Por defecto a los 3 minutos; se cambia en 🔔.
  Si no responde ninguna pero el móvil tiene internet y Tailscale activo, también te avisa,
  así te enteras si se va la luz o internet en casa.
- Si el móvil no tiene Tailscale activo, no da falsas alarmas: la notificación fija dice
  «Sin conexión con las Pi (¿Tailscale desactivado?)».
- Si el móvil se queda un rato sin conexión, al volver recibe los avisos que se perdió (las Pi guardan los últimos 200).
- Telegram sigue disponible como opción en 🔔, por si algún día quieres avisos sin Tailscale.

## Bots

Pestaña **Bots** de cada Pi. Puedes:
- Crear un bot a partir de tu script, para que esté siempre encendido, se ejecute a una hora o solo cuando lo arranques.
- Arrancarlo, pararlo y ver sus logs.

Los bots programados usan la hora de la Pi: `sudo timedatectl set-timezone Europe/Madrid`.

## Notas

- Si cambias solo la página (`agente/index.html`), la app de la APK necesita recompilarse,
  porque la lleva dentro. El navegador la coge directamente de la Pi.
- Todo va por HTTP dentro de tu red o Tailscale. No abras el puerto 8790 en el router.
