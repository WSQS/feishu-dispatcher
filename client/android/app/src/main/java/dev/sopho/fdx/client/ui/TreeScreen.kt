package dev.sopho.fdx.client.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.tree.ROOT_PATH
import dev.sopho.fdx.client.tree.TreeRow

/**
 * 文件树页：按目录加载直接子项，数据随目的地（NavBackStackEntry）存活。
 * 点击目录展开/折叠，点击文件经 [onFileClick] 打开内容页。
 */
@Composable
fun TreeScreen(
    client: ViewerClient,
    projectName: String,
    modifier: Modifier = Modifier,
    onFileClick: ((String) -> Unit)? = null,
) {
    val vm: TreeViewModel = viewModel { TreeViewModel(projectName, client) }
    val state by vm.state.collectAsState()
    val root = state.directories.getValue(ROOT_PATH)
    val rows = state.visibleRows()

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text(projectName, style = MaterialTheme.typography.titleLarge)
        when {
            root.loading && root.entries.isEmpty() ->
                CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            root.error != null && root.entries.isEmpty() ->
                TreeLoadError(
                    message = root.error,
                    modifier = Modifier.padding(top = 8.dp),
                    onRetry = { vm.retry(ROOT_PATH) },
                )
            root.entries.isEmpty() ->
                Text("（空）", modifier = Modifier.padding(top = 16.dp))
            else -> {
                if (root.error != null) {
                    TreeLoadError(
                        message = root.error,
                        modifier = Modifier.padding(top = 8.dp),
                        onRetry = { vm.retry(ROOT_PATH) },
                    )
                }
                LazyColumn(modifier = Modifier.fillMaxSize()) {
                    items(items = rows, key = { it.path }) { row ->
                        val expanded = row.path in state.expandedPaths
                        TreeNodeRow(
                            row = row,
                            expanded = expanded,
                            onToggle = vm::toggle,
                            onRetry = vm::retry,
                            onFileClick = onFileClick,
                        )
                        HorizontalDivider()
                    }
                }
            }
        }
    }
}

@Composable
private fun TreeNodeRow(
    row: TreeRow,
    expanded: Boolean,
    onToggle: (String) -> Unit,
    onRetry: (String) -> Unit,
    onFileClick: ((String) -> Unit)?,
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable {
                    if (row.isDirectory) onToggle(row.path) else onFileClick?.invoke(row.path)
                }
                .minimumInteractiveComponentSize()
                .padding(start = (row.depth * 16).dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (row.isDirectory) {
                Text(if (expanded) "▾" else "▸", modifier = Modifier.width(24.dp))
            } else {
                Spacer(modifier = Modifier.width(24.dp))
            }
            Text(
                row.name,
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
            )
            if (row.isDirectory && expanded && row.loading) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
            }
        }
        if (row.isDirectory && expanded && row.error != null) {
            TreeLoadError(
                message = row.error,
                modifier = Modifier.padding(start = ((row.depth + 1) * 16).dp),
                onRetry = { onRetry(row.path) },
            )
        }
    }
}

@Composable
private fun TreeLoadError(
    message: String,
    modifier: Modifier = Modifier,
    onRetry: () -> Unit,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "加载失败：$message",
            modifier = Modifier.weight(1f),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error,
        )
        TextButton(onClick = onRetry) {
            Text("重试")
        }
    }
}
