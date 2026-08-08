package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    var showProjects by remember { mutableStateOf(false) }
                    var client by remember { mutableStateOf<ViewerClient?>(null) }

                    if (showProjects && client != null) {
                        ProjectListScreen(client = client!!)
                    } else {
                        ConfigScreen(
                            repo = store,
                            storagePath = applicationContext.filesDir.absolutePath,
                            onConnected = { conn ->
                                client = ViewerClient.fromConnection(conn)
                                showProjects = true
                            },
                        )
                    }
                }
            }
        }
    }
}
