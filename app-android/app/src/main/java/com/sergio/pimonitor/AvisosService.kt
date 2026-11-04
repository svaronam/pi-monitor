package com.sergio.pimonitor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Cada 30 s pregunta a cada Pi por sus avisos nuevos (/api/eventos) y los muestra como notificaciones.
 * Si una Pi deja de responder durante "caida_min" minutos, avisa el propio móvil (y cuando vuelve).
 * Funciona mientras el móvil llegue a las Pi: en casa por wifi, o fuera con Tailscale activo.
 */
class AvisosService : Service() {

    @Volatile private var activo = false
    private var hilo: Thread? = null
    private val despertar = LinkedBlockingQueue<Int>()
    private var textoFija = ""

    private val prefs by lazy { getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE) }
    private val nm by lazy { getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        crearCanales()
        val fija = notificacionFija("Vigilando tus Raspberry Pi")
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_FIJA, fija, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(ID_FIJA, fija)
        }
        if (hilo == null) {
            activo = true
            hilo = Thread { bucle() }.also { it.isDaemon = true; it.start() }
        } else if (intent?.action == ACCION_REVISAR) {
            despertar.offer(1)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        activo = false
        hilo?.interrupt()
        hilo = null
        super.onDestroy()
    }

    private fun bucle() {
        while (activo) {
            try {
                revisar()
            } catch (e: Exception) {
                // nunca dejar morir el bucle
            }
            try {
                despertar.poll(30, TimeUnit.SECONDS)
                despertar.clear()
            } catch (e: InterruptedException) {
                return
            }
        }
    }

    private fun revisar() {
        val pis = try { JSONArray(prefs.getString("pis", "[]")) } catch (e: Exception) { JSONArray() }
        if (pis.length() == 0) {
            actualizarFija("Sin Raspberry configuradas")
            return
        }
        val umbral = prefs.getInt("caida_min", 3).coerceAtLeast(1) * 2   // ciclos de 30 s
        val (internet, vpn) = estadoRed()

        val resultados = ArrayList<Pair<JSONObject, Boolean>>()
        for (i in 0 until pis.length()) {
            val pi = pis.optJSONObject(i) ?: continue
            resultados.add(pi to consultar(pi))
        }
        // Si no responde ninguna, solo avisamos si el móvil tiene internet y Tailscale (VPN) activo:
        // así distinguimos "se ha ido la luz en casa" de "el móvil no tiene conexión".
        val algunaOk = resultados.any { it.second }
        val puedoJuzgar = algunaOk || (internet && vpn)

        val caidas = ArrayList<String>()
        val ed = prefs.edit()
        for ((pi, ok) in resultados) {
            val id = pi.optString("id")
            val nombre = pi.optString("name", "Raspberry")
            val kFallos = "fallos_$id"
            val kCaida = "caida_$id"
            val estabaCaida = prefs.getBoolean(kCaida, false)
            if (ok) {
                if (estabaCaida) {
                    mostrar(idCaida(id), "🟢 $nombre", "Vuelve a responder", 3)
                }
                ed.putInt(kFallos, 0).putBoolean(kCaida, false)
            } else if (puedoJuzgar) {
                val f = prefs.getInt(kFallos, 0) + 1
                ed.putInt(kFallos, f)
                if (f >= umbral && !estabaCaida) {
                    mostrar(idCaida(id), "🔴 $nombre", "No responde desde hace ${f / 2} min. ¿Apagada, sin corriente o sin red?", 5)
                    ed.putBoolean(kCaida, true)
                }
                if (f >= umbral || estabaCaida) caidas.add(nombre)
            } else if (estabaCaida) {
                caidas.add(nombre)
            }
        }
        ed.apply()

        val n = resultados.size
        actualizarFija(
            when {
                !puedoJuzgar -> "Sin conexión con las Pi (¿Tailscale desactivado?)"
                caidas.isNotEmpty() -> "Sin respuesta: " + caidas.joinToString(", ")
                n == 1 -> "1 Pi · todo responde"
                else -> "$n Pi · todas responden"
            }
        )
    }

    /** Pide los avisos nuevos a una Pi. Devuelve true si la Pi ha contestado (aunque sea con error). */
    private fun consultar(pi: JSONObject): Boolean {
        val id = pi.optString("id")
        val url = pi.optString("url").trimEnd('/')
        if (url.isEmpty()) return false
        val kUltimo = "ultimo_$id"
        val ultimo = prefs.getLong(kUltimo, -1L)
        val q = if (ultimo < 0) "" else "?desde=$ultimo"
        var con: HttpURLConnection? = null
        return try {
            con = URL("$url/api/eventos$q").openConnection() as HttpURLConnection
            con.connectTimeout = 8000
            con.readTimeout = 8000
            con.setRequestProperty("Authorization", "Bearer " + pi.optString("token"))
            val code = con.responseCode
            if (code == 200) {
                val j = JSONObject(con.inputStream.bufferedReader().use { it.readText() })
                val ultimoPi = j.optLong("ultimo", 0L)
                val nuevo = ultimoPi
                if (ultimo >= 0 && ultimoPi >= ultimo) {
                    val arr = j.optJSONArray("eventos") ?: JSONArray()
                    for (i in 0 until arr.length()) {
                        val e = arr.getJSONObject(i)
                        val eid = e.optLong("id")
                        mostrar(idEvento(id, eid), e.optString("titulo"), e.optString("msg"), e.optInt("prio", 3))
                    }
                }
                // Primera vez (o la Pi reinició su historial): solo apuntamos dónde vamos, sin avisar de lo antiguo
                prefs.edit().putLong(kUltimo, nuevo).apply()
            }
            true
        } catch (e: Exception) {
            false
        } finally {
            con?.disconnect()
        }
    }

    /** (internet validado por una red que no es VPN, hay una VPN activa como Tailscale) */
    @Suppress("DEPRECATION")
    private fun estadoRed(): Pair<Boolean, Boolean> {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return false to false
        var internet = false
        var vpn = false
        for (red in cm.allNetworks) {
            val c = cm.getNetworkCapabilities(red) ?: continue
            if (c.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                vpn = true
            } else if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) {
                internet = true
            }
        }
        return internet to vpn
    }

    private fun abrirApp(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE
    )

    private fun notificacionFija(texto: String): Notification =
        NotificationCompat.Builder(this, CANAL_ESTADO)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setColor(COLOR_APP)
            .setContentTitle("Pi Monitor")
            .setContentText(texto)
            .setContentIntent(abrirApp())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()

    private fun actualizarFija(texto: String) {
        if (texto == textoFija) return
        textoFija = texto
        try { nm.notify(ID_FIJA, notificacionFija(texto)) } catch (e: SecurityException) { }
    }

    private fun mostrar(notifId: Int, titulo: String, msg: String, prio: Int) {
        val n = NotificationCompat.Builder(this, CANAL_AVISOS)
            .setSmallIcon(R.drawable.ic_notificacion)
            .setColor(COLOR_APP)
            .setLargeIcon(BitmapFactory.decodeResource(resources, R.mipmap.ic_launcher))
            .setContentTitle(titulo)
            .setContentText(msg)
            .setStyle(NotificationCompat.BigTextStyle().bigText(msg))
            .setContentIntent(abrirApp())
            .setAutoCancel(true)
            .setPriority(if (prio >= 4) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .build()
        try { nm.notify(notifId, n) } catch (e: SecurityException) { /* sin permiso de notificaciones */ }
    }

    private fun idCaida(piId: String) = ("caida:$piId").hashCode() or 0x40000000
    private fun idEvento(piId: String, eid: Long) = ("$piId:$eid").hashCode() and 0x3FFFFFFF or 0x100

    private fun crearCanales() {
        nm.createNotificationChannel(NotificationChannel(CANAL_ESTADO, "Servicio de vigilancia", NotificationManager.IMPORTANCE_MIN))
        nm.createNotificationChannel(NotificationChannel(CANAL_AVISOS, "Avisos de las Raspberry", NotificationManager.IMPORTANCE_HIGH))
    }

    companion object {
        const val ACCION_REVISAR = "com.sergio.pimonitor.REVISAR"
        const val COLOR_APP = 0xFF4ADE80.toInt()
        const val CANAL_ESTADO = "estado"
        const val CANAL_AVISOS = "avisos"
        const val ID_FIJA = 1
    }
}
