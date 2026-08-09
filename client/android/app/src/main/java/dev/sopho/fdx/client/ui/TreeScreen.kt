package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.network.TreeEntry
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 文件树页：调 [ViewerClient.tree] 显示 project 的文件列表（扁平）。
 *
 * 点击文件经 [onFileClick] 打开内容页。
 */
@Composable
fun TreeScreen(
    client: ViewerClient,
    projectName: String,
    modifier: Modifier = Modifier,
    onFileClick: ((String) -> Unit)? = null,
) {
    var entries by remember { mutableStateOf<List<TreeEntry>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(projectName) {
        runCatching { client.tree(projectName).entries }
            .onSuccess { entries = it }
            .onFailure {
                Log.e("TreeScreen", "failed to load tree", it)
                error = it.message
            }
    }

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text(projectName, style = MaterialTheme.typography.titleLarge)
        when {
            error != null -> Text("❌ $error")
            entries == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            entries!!.isEmpty() -> Text("（空）", modifier = Modifier.padding(top = 16.dp))
            else -> LazyColumn(modifier = Modifier.fillMaxSize()) {
                items(entries!!) { entry ->
                    Column(modifier = Modifier.padding(vertical = 8.dp)) {
                        Text(
                            entry.path,
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.clickable { onFileClick?.invoke(entry.path) },
                        )
                        Text(
                            "${entry.size} bytes",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}
