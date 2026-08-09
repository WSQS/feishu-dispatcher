package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
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
import dev.sopho.fdx.client.network.ProjectItem
import dev.sopho.fdx.client.network.ViewerClient
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** 项目列表 UI 状态（Activity 级 ViewModel 持有，跨导航缓存）。 */
data class ProjectListUiState(
    val projects: List<ProjectItem>? = null,
    val error: String? = null,
)

/**
 * 缓存 [ViewerClient.projects] 结果。`viewModel()` 默认绑 Activity，
 * AnimatedContent 拆掉再挂回 Screen 时不销毁，故返回不重请求。
 */
class ProjectListViewModel : ViewModel() {
    private val _ui = MutableStateFlow(ProjectListUiState())
    val ui: StateFlow<ProjectListUiState> = _ui.asStateFlow()

    private var loadStarted = false

    /** 首次进入才请求；之后（含失败）都走已有 state，避免导航返回/预览重打。 */
    fun ensureLoaded(client: ViewerClient) {
        if (loadStarted) return
        loadStarted = true
        viewModelScope.launch {
            runCatching { client.projects().items }
                .onSuccess { _ui.value = ProjectListUiState(projects = it) }
                .onFailure {
                    Log.e("ProjectList", "failed to load projects", it)
                    _ui.value = ProjectListUiState(error = it.message)
                }
        }
    }
}

/**
 * 项目列表页：观察 [ProjectListViewModel] 显示 daemon 注册的项目。
 *
 * 点击项目暂不跳转；先验通 /api/projects 端到端。
 */
@Composable
fun ProjectListScreen(
    client: ViewerClient,
    modifier: Modifier = Modifier,
    onProjectClick: ((String) -> Unit)? = null,
    vm: ProjectListViewModel = viewModel(),
) {
    val ui by vm.ui.collectAsState()
    LaunchedEffect(client) { vm.ensureLoaded(client) }

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text("项目列表", style = MaterialTheme.typography.titleLarge)
        when {
            ui.error != null -> Text("❌ ${ui.error}")
            ui.projects == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            ui.projects!!.isEmpty() -> Text("（无项目）", modifier = Modifier.padding(top = 16.dp))
            else -> LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                items(ui.projects!!) { project ->
                    Column(
                        modifier = Modifier
                            .padding(vertical = 8.dp)
                            .clickable { onProjectClick?.invoke(project.name) },
                    ) {
                        Text(project.name, style = MaterialTheme.typography.titleMedium)
                        Text(
                            "${project.defaultAgent} · ${project.path}",
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
