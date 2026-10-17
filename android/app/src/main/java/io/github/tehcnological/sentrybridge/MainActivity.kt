package io.github.tehcnological.sentrybridge

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import java.io.File

/** Offline shared UI. Decoding stays in the native foreground service. */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private var chooser: ValueCallback<Array<Uri>>? = null
    private var exporting: File? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Color.rgb(11, 23, 39)
        window.navigationBarColor = Color.rgb(11, 23, 39)
        NativeGui.start(applicationContext)
        web = WebView(this).apply {
            setBackgroundColor(Color.rgb(11, 23, 39))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            // Document-picker uploads use content URIs; CSP and navigation restrict page loads.
            settings.allowContentAccess = true
            settings.setSupportMultipleWindows(false)
            CookieManager.getInstance().setAcceptThirdPartyCookies(this, false)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    if (request.url.host == "127.0.0.1" && request.url.port == NativeGui.port) return false
                    if (request.url.scheme in listOf("http", "https"))
                        runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                    chooser?.onReceiveValue(null)
                    chooser = callback
                    startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"), 10)
                    return true
                }
            }
            setDownloadListener { url, _, _, _, _ ->
                val token = Uri.parse(url).lastPathSegment ?: return@setDownloadListener
                runCatching {
                    exporting = NativeGui.file(token)
                    startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType("application/octet-stream").putExtra(Intent.EXTRA_TITLE, NativeGui.fileName(token)), 11)
                }.onFailure { Toast.makeText(this@MainActivity, it.message, Toast.LENGTH_LONG).show() }
            }
        }
        setContentView(web)
        // Always issue a fresh credential after process recreation; preserve appearance in WebView storage.
        web.loadUrl("http://127.0.0.1:${NativeGui.port}/", mapOf("Authorization" to "Bearer ${NativeGui.secret}"))
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 10) {
            chooser?.onReceiveValue(if (resultCode == RESULT_OK && data?.data != null) arrayOf(data.data!!) else null)
            chooser = null
        } else if (requestCode == 11) {
            val source = exporting
            exporting = null
            if (resultCode == RESULT_OK && data?.data != null && source != null) Thread {
                runCatching { contentResolver.openOutputStream(data.data!!)?.use { out -> source.inputStream().use { it.copyTo(out) } } }
                    .onFailure { error -> runOnUiThread { Toast.makeText(this, error.message, Toast.LENGTH_LONG).show() } }
            }.start()
        }
    }
    override fun onResume() { super.onResume(); if (::web.isInitialized) { web.resumeTimers(); web.onResume() } }
    override fun onPause() { if (::web.isInitialized) { web.onPause(); web.pauseTimers() }; super.onPause() }
    override fun onDestroy() { chooser?.onReceiveValue(null); web.destroy(); super.onDestroy() }
}
