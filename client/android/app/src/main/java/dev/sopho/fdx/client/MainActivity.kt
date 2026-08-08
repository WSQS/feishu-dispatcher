package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    ConfigScreen(store, storagePath = applicationContext.filesDir.absolutePath)
                }
            }
        }
    }
}
