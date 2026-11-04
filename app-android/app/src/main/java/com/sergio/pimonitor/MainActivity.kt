package com.sergio.pimonitor

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.util.concurrent.Executors

/**
 * Pi Monitor: la misma página que sirve el agente de las Pi, pero metida en la app (assets/index.html).
 * La página habla con Android a través de "PiNative": guarda aquí la lista de Pi (para el servicio de avisos)
 * y las peticiones HTTP las hace Android, así no hay problemas de CORS ni de red local en el WebView.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private val red = Executors.newCachedThreadPool()
    private val prefs by lazy { getSharedPreferences(PREFS, Context.MODE_PRIVATE) }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        web = findViewById(R.id.web)
        swipe = findViewById(R.id.swipe)
        val raiz: View = findViewById(R.id.raiz)

        // Márgenes del sistema (barra de estado, teclado, barra de navegación)
        ViewCompat.setOnApplyWindowInsetsListener(raiz) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            insets
        }

        web.setBackgroundColor(0xFF0F1412.toInt())
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.addJavascriptInterface(Puente(), "PiNative")

        web.webViewClient = object : WebViewClient() {
            // Los enlaces externos (p. ej. healthchecks.io) se abren en el navegador
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val u = request?.url ?: return false
                if (u.scheme == "file") return false
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (e: Exception) { }
                return true
            }
        }

        // Sin esto, los avisos y confirmaciones de la página (¿Seguro?) no se muestran
        web.webChromeClient = object : WebChromeClient() {
            override fun onJsAlert(view: WebView?, url: String?, message: String?, result: JsResult?): Boolean {
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("OK") { _, _ -> result?.confirm() }
                    .setOnCancelListener { result?.cancel() }
                    .show()
                return true
            }

            override fun onJsConfirm(view: WebView?, url: String?, message: String?, result: JsResult?): Boolean {
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("Sí") { _, _ -> result?.confirm() }
                    .setNegativeButton("No") { _, _ -> result?.cancel() }
                    .setOnCancelListener { result?.cancel() }
                    .show()
                return true
            }
        }

        // Deslizar hacia abajo = actualizar
        swipe.setColorSchemeColors(0xFF4ADE80.toInt())
        swipe.setProgressBackgroundColorSchemeColor(0xFF18201C.toInt())
        swipe.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 }
        swipe.setOnRefreshListener {
            web.evaluateJavascript("if(typeof refreshAll==='function'){refreshAll()}", null)
            swipe.postDelayed({ swipe.isRefreshing = false }, 1200)
        }

        // Botón atrás: primero cierra la hoja abierta o vuelve a la lista
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                web.evaluateJavascript("(typeof goBack==='function'&&goBack())?1:0") { r ->
                    if (r != "1") {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                        isEnabled = true
                    }
                }
            }
        })

        pedirPermisoAvisos()
        web.loadUrl("file:///android_asset/index.html")
        gestionarAvisos(this)
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) web.evaluateJavascript("if(typeof refreshAll==='function'){refreshAll()}", null)
    }

    override fun onDestroy() {
        red.shutdownNow()
        super.onDestroy()
    }

    /** Android 13+ exige permiso para mostrar notificaciones. */
    private fun pedirPermisoAvisos() {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    /** Lo que la página puede pedir a Android. */
    inner class Puente {
        @JavascriptInterface
        fun getPis(): String = prefs.getString("pis", "[]") ?: "[]"

        @JavascriptInterface
        fun setPis(json: String) {
            prefs.edit().putString("pis", json).apply()
            runOnUiThread { gestionarAvisos(this@MainActivity) }
        }

        @JavascriptInterface
        fun getAjustes(): String = JSONObject()
            .put("avisos", prefs.getBoolean("avisos", true))
            .put("caida_min", prefs.getInt("caida_min", 3))
            .toString()

        @JavascriptInterface
        fun setAjustes(json: String) {
            try {
                val j = JSONObject(json)
                prefs.edit()
                    .putBoolean("avisos", j.optBoolean("avisos", true))
                    .putInt("caida_min", j.optInt("caida_min", 3).coerceIn(1, 120))
                    .apply()
            } catch (e: Exception) { }
            runOnUiThread { gestionarAvisos(this@MainActivity) }
        }

        /** Pide al servicio que consulte ya (para la notificación de prueba). */
        @JavascriptInterface
        fun revisarYa() {
            runOnUiThread {
                try {
                    if (prefs.getBoolean("avisos", true)) {
                        ContextCompat.startForegroundService(
                            this@MainActivity,
                            Intent(this@MainActivity, AvisosService::class.java).setAction(AvisosService.ACCION_REVISAR)
                        )
                    }
                } catch (e: Exception) { }
            }
        }

        /** Petición HTTP hecha por Android. Responde llamando a window.__piResolve(id, estado, texto).
         *  estado: código HTTP, 0 = no se pudo conectar, -1 = sin respuesta a tiempo. */
        @JavascriptInterface
        fun http(id: Int, method: String, url: String, headers: String, body: String, timeout: Int) {
            red.execute {
                var estado = 0
                var texto = ""
                var con: HttpURLConnection? = null
                try {
                    con = URL(url).openConnection() as HttpURLConnection
                    con.requestMethod = method
                    con.connectTimeout = timeout.coerceIn(1000, 120_000)
                    con.readTimeout = timeout.coerceIn(1000, 120_000)
                    val h = JSONObject(headers)
                    for (k in h.keys()) con.setRequestProperty(k, h.getString(k))
                    if (method == "POST") {
                        con.doOutput = true
                        con.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                    }
                    estado = con.responseCode
                    val flujo = if (estado >= 400) con.errorStream else con.inputStream
                    texto = flujo?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
                } catch (e: SocketTimeoutException) {
                    estado = -1; texto = ""
                } catch (e: Exception) {
                    estado = 0; texto = e.javaClass.simpleName
                } finally {
                    con?.disconnect()
                }
                val js = "window.__piResolve&&window.__piResolve($id,$estado,${JSONObject.quote(texto)})"
                web.post { web.evaluateJavascript(js, null) }
            }
        }
    }

    companion object {
        const val PREFS = "pimon"

        /** Arranca o para el servicio de avisos según los ajustes y si hay alguna Pi. */
        fun gestionarAvisos(ctx: Context) {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val hayPis = (p.getString("pis", "[]") ?: "[]").trim().length > 2
            val intent = Intent(ctx, AvisosService::class.java)
            try {
                if (p.getBoolean("avisos", true) && hayPis) {
                    ContextCompat.startForegroundService(ctx, intent)
                } else {
                    ctx.stopService(intent)
                }
            } catch (e: Exception) {
                // si Android no lo permite ahora, se reintenta la próxima vez que se abra la app
            }
        }
    }
}
