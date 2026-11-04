package com.sergio.pimonitor

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Al encender el móvil, vuelve a arrancar el servicio de avisos si estaba activado. */
class ArranqueReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) return
        MainActivity.gestionarAvisos(context)
    }
}
