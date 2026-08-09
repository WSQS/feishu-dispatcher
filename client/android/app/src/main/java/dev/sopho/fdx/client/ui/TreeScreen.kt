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
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.sopho.fdx.client.network.TreeEntry
import dev.sopho.fdx.client.network.ViewerClient
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** 文件树 UI 状态（按 project 缓存，跨导航保留）。 */
data class TreeUiState(
    val projectName: String = "",
    val entries: List<TreeEntry>? = null,
    val error: String? = null,
)

/**
 * 按 projectName 缓存 [ViewerClient.tree]。Activity 级 ViewModel，
 * 返回同一项目时直接出缓存；换项目才请求（或命中该项目旧缓存）。
 */
class TreeViewModel : ViewModel() {
    /** 成功条目；失败记在 [errors]，二者都算「已加载」以免返回/预览重打。 */
    private val cache = mutableMapOf<String, List<TreeEntry>>()
    private val errors = mutableMapOf<String, String?>()
    private val inFlight = mutableSetOf<String>()

    private val _ui = MutableStateFlow(TreeUiState())
    val ui: StateFlow<TreeUiState> = _ui.asStateFlow()

    fun ensureLoaded(client: ViewerClient, projectName: String) {
        val cached = cache[projectName]
        if (cached != null) {
            _ui.value = TreeUiState(projectName = projectName, entries = cached)
            return
        }
        if (projectName in errors) {
            _ui.value = TreeUiState(projectName = projectName, error = errors[projectName])
            return
        }
        if (projectName in inFlight) {
            if (_ui.value.projectName != projectName) {
                _ui.value = TreeUiState(projectName = projectName)
            }
            return
        }
        inFlight.add(projectName)
        _ui.value = TreeUiState(projectName = projectName)
        viewModelScope.launch {
            try {
                runCatching { client.tree(projectName).entries }
                    .onSuccess {
                        cache[projectName] = it
                        if (_ui.value.projectName == projectName) {
                            _ui.value = TreeUiState(projectName = projectName, entries = it)
                        }
                    }
                    .onFailure {
                        Log.e("TreeScreen", "failed to load tree", it)
                        errors[projectName] = it.message
                        if (_ui.value.projectName == projectName) {
                            _ui.value = TreeUiState(projectName = projectName, error = it.message)
                        }
                    }
            } finally {
                inFlight.remove(projectName)
            }
        }
    }
}

/**
 * 文件树页：观察 [TreeViewModel] 显示 project 的文件列表（扁平）。
 *
 * 点击文件暂不跳转。
 */
@Composable
fun TreeScreen(
    client: ViewerClient,
    projectName: String,
    modifier: Modifier = Modifier,
    vm: TreeViewModel = viewModel(),
) {
    val ui by vm.ui.collectAsState()
    LaunchedEffect(client, projectName) { vm.ensureLoaded(client, projectName) }

    // 切换项目瞬间可能仍短暂持有上一项目 state；以参数为准显示标题，内容仅在匹配时展示。
    val showing = ui.projectName == projectName
    val entries = if (showing) ui.entries else null
    val error = if (showing) ui.error else null

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
