package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.network.DiffFile
import dev.sopho.fdx.client.network.ViewerClient

/**
 * Diff 页：调 [ViewerClient.diff] 展示工作区 vs HEAD 的 patch。
 *
 * 渲染在安卓端（决策 Q6）：按行给 +/-/@@ 着色；服务端只给纯文本 patch。
 */
@Composable
fun DiffScreen(
    client: ViewerClient,
    projectName: String,
    modifier: Modifier = Modifier,
) {
    var files by remember { mutableStateOf<List<DiffFile>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(projectName) {
        runCatching { client.diff(projectName).files }
            .onSuccess { files = it }
            .onFailure {
                Log.e("DiffScreen", "failed to load diff", it)
                error = it.message
            }
    }

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text("Diff · $projectName", style = MaterialTheme.typography.titleLarge)
        Text(
            "工作区 vs HEAD",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        when {
            error != null -> Text("❌ $error", modifier = Modifier.padding(top = 16.dp))
            files == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            files!!.isEmpty() -> Text(
                "（无改动）",
                modifier = Modifier.padding(top = 16.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            else -> LazyColumn(modifier = Modifier.fillMaxSize().padding(top = 12.dp)) {
                items(files!!) { file ->
                    Text(
                        "${file.status}  ${file.path}",
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.padding(vertical = 8.dp),
                    )
                    Text(
                        annotatedPatch(file.patch),
                        style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                        modifier = Modifier
                            .fillMaxWidth()
                            .horizontalScroll(rememberScrollState())
                            .padding(bottom = 8.dp),
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

private val AddColor = Color(0xFF1B7F3A)
private val DelColor = Color(0xFFB00020)
private val HunkColor = Color(0xFF1565C0)

/** 按行给 unified diff 着色（+ 绿 / − 红 / @@ 蓝）。 */
private fun annotatedPatch(patch: String) = buildAnnotatedString {
    if (patch.isEmpty()) {
        append("（空 patch）")
        return@buildAnnotatedString
    }
    patch.lineSequence().forEachIndexed { i, line ->
        if (i > 0) append('\n')
        val color = when {
            line.startsWith("+++") || line.startsWith("---") -> null
            line.startsWith('+') -> AddColor
            line.startsWith('-') -> DelColor
            line.startsWith("@@") -> HunkColor
            else -> null
        }
        if (color != null) {
            withStyle(SpanStyle(color = color)) { append(line) }
        } else {
            append(line)
        }
    }
}
