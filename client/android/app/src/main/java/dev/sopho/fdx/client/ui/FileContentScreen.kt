package dev.sopho.fdx.client.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 文件内容页：数据在 [FileContentViewModel]（随目的地存活），返回时复用缓存不重拉。
 *
 * 内容以等宽纯文本展示，并支持横竖滚动。
 */
@Composable
fun FileContentScreen(
    client: ViewerClient,
    projectName: String,
    path: String,
    modifier: Modifier = Modifier,
) {
    val vm: FileContentViewModel = viewModel { FileContentViewModel(projectName, path) }
    LaunchedEffect(client) { vm.start(client) }

    val file = vm.file
    val error = vm.error

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text(path, style = MaterialTheme.typography.titleMedium)
        Text(
            projectName,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        when {
            error != null -> Text("❌ $error", modifier = Modifier.padding(top = 16.dp))
            file == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            file.binary -> Text(
                "（二进制文件，无法预览）",
                modifier = Modifier.padding(top = 16.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            else -> Text(
                file.content.ifEmpty { "（空文件）" },
                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                modifier = Modifier
                    .padding(top = 16.dp)
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .horizontalScroll(rememberScrollState()),
            )
        }
    }
}
