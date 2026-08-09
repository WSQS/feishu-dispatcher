package dev.sopho.fdx.client.ui

import android.util.Log
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.network.FileResponse
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 文件内容页：调 [ViewerClient.file] 只读展示工作区文件。
 *
 * 语法高亮留给后续（决策 Q6：可在安卓端做）；v1 等宽纯文本 + 横竖滚动。
 */
@Composable
fun FileContentScreen(
    client: ViewerClient,
    projectName: String,
    path: String,
    modifier: Modifier = Modifier,
) {
    var file by remember { mutableStateOf<FileResponse?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(projectName, path) {
        runCatching { client.file(projectName, path) }
            .onSuccess { file = it }
            .onFailure {
                Log.e("FileContent", "failed to load file", it)
                error = it.message
            }
    }

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
            file!!.binary -> Text(
                "（二进制文件，无法预览）",
                modifier = Modifier.padding(top = 16.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            else -> Text(
                file!!.content.ifEmpty { "（空文件）" },
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
