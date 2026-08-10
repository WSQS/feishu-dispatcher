package dev.sopho.fdx.client.ui

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
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 文件树页：数据在 [TreeViewModel]（随目的地存活），返回时复用缓存不重拉。
 *
 * 点击文件暂不跳转。
 */
@Composable
fun TreeScreen(
    client: ViewerClient,
    projectName: String,
    modifier: Modifier = Modifier,
) {
    val vm: TreeViewModel = viewModel { TreeViewModel(projectName) }
    LaunchedEffect(client) { vm.start(client) }

    val entries = vm.entries
    val error = vm.error

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text(projectName, style = MaterialTheme.typography.titleLarge)
        when {
            error != null -> Text("❌ $error")
            entries == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            entries.isEmpty() -> Text("（空）", modifier = Modifier.padding(top = 16.dp))
            else -> LazyColumn(modifier = Modifier.fillMaxSize()) {
                items(entries) { entry ->
                    Column(modifier = Modifier.padding(vertical = 8.dp)) {
                        Text(
                            entry.path,
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.clickable { /* TODO: open file content */ },
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
