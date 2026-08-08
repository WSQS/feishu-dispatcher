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
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    var client by remember { mutableStateOf<ViewerClient?>(null) }
                    var selectedProject by remember { mutableStateOf<String?>(null) }

                    val c = client
                    val proj = selectedProject
                    when {
                        c != null && proj != null -> TreeScreen(client = c, projectName = proj)
                        c != null -> ProjectListScreen(
                            client = c,
                            onProjectClick = { name -> selectedProject = name },
                        )
                        else -> ConfigScreen(
                            repo = store,
                            storagePath = applicationContext.filesDir.absolutePath,
                            onConnected = { conn ->
                                client = ViewerClient.fromConnection(conn)
                            },
                        )
                    }
                }
            }
        }
    }
}
