package dev.sopho.fdx.client.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 文件内容页：数据在 [FileContentViewModel]（随目的地存活），返回时复用缓存不重拉。
 *
 * 内容按行渲染（LazyColumn 虚拟化，长行自动换行），左侧带行号。
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
            file.content.isEmpty() -> Text(
                "（空文件）",
                modifier = Modifier.padding(top = 16.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            else -> {
                val lines = vm.lines.orEmpty()
                // 行号栏按总行数位数固定宽度，位数变化（9→10）内容不横移
                val gutterWidth = lines.size.toString().length
                LazyColumn(modifier = Modifier.fillMaxSize().padding(top = 8.dp)) {
                    itemsIndexed(lines) { index, line ->
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.Top,
                        ) {
                            Text(
                                "${index + 1}".padStart(gutterWidth),
                                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(
                                line,
                                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                                modifier = Modifier.weight(1f),
                            )
                        }
                    }
                }
            }
        }
    }
}
