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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.network.ProjectItem
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 项目列表页：调 [ViewerClient.projects] 显示 daemon 注册的项目。
 *
 * 点击项目暂不跳转；先验通 /api/projects 端到端。
 */
@Composable
fun ProjectListScreen(
    client: ViewerClient,
    modifier: Modifier = Modifier,
    onProjectClick: ((String) -> Unit)? = null,
) {
    var projects by remember { mutableStateOf<List<ProjectItem>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        runCatching { client.projects().items }
            .onSuccess { projects = it }
            .onFailure {
                Log.e("ProjectList", "failed to load projects", it)
                error = it.message
            }
    }

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Text("项目列表", style = MaterialTheme.typography.titleLarge)
        when {
            error != null -> Text("❌ $error")
            projects == null -> CircularProgressIndicator(modifier = Modifier.padding(top = 16.dp))
            projects!!.isEmpty() -> Text("（无项目）", modifier = Modifier.padding(top = 16.dp))
            else -> LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                items(projects!!) { project ->
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
