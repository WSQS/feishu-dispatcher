package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
 * 项目列表相对 daemon 的会话态（由 MainActivity 后台连 DataStore 配置驱动）。
 *
 * - [Unconfigured]：尚无有效已存配置 → 引导去设置。
 * - [Connecting]：正在读配置 / 建连 / health。
 * - [Ready]：已连上，可拉项目列表。
 * - [Failed]：建连失败，可重试或去设置。
 */
sealed interface ProjectListSession {
    data object Unconfigured : ProjectListSession
    data object Connecting : ProjectListSession
    data class Ready(val client: ViewerClient) : ProjectListSession
    data class Failed(val message: String) : ProjectListSession
}

/**
 * 项目列表页（App 根屏）：按 [session] 展示未配置 / 连接中 / 失败 / 已连接列表。
 *
 * 已连接时调 [ViewerClient.projects]；齿轮进设置（[onOpenSettings]）。
 */
@Composable
fun ProjectListScreen(
    session: ProjectListSession,
    onOpenSettings: () -> Unit,
    modifier: Modifier = Modifier,
    onRetryConnect: (() -> Unit)? = null,
    onProjectClick: ((String) -> Unit)? = null,
) {
    var projects by remember { mutableStateOf<List<ProjectItem>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val client = (session as? ProjectListSession.Ready)?.client

    LaunchedEffect(client) {
        projects = null
        error = null
        if (client == null) return@LaunchedEffect
        runCatching { client.projects().items }
            .onSuccess { projects = it }
            .onFailure {
                Log.e("ProjectList", "failed to load projects", it)
                error = it.message
            }
    }

    Column(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.surface).padding(16.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("项目列表", style = MaterialTheme.typography.titleLarge)
            IconButton(onClick = onOpenSettings) {
                Icon(Icons.Filled.Settings, contentDescription = "设置")
            }
        }
        when (session) {
            ProjectListSession.Unconfigured -> {
                Text(
                    "未配置连接。请先在设置里填写 Viewer 地址与 Token。",
                    modifier = Modifier.padding(top = 16.dp),
                )
                Button(
                    onClick = onOpenSettings,
                    modifier = Modifier.padding(top = 12.dp),
                ) { Text("去设置") }
            }
            ProjectListSession.Connecting -> {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(top = 16.dp),
                ) {
                    CircularProgressIndicator()
                    Text("连接中…", modifier = Modifier.padding(start = 12.dp))
                }
            }
            is ProjectListSession.Failed -> {
                Text("❌ ${session.message}", modifier = Modifier.padding(top = 16.dp))
                Row(
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    modifier = Modifier.padding(top = 12.dp),
                ) {
                    if (onRetryConnect != null) {
                        Button(onClick = onRetryConnect) { Text("重试") }
                    }
                    OutlinedButton(onClick = onOpenSettings) { Text("去设置") }
                }
            }
            is ProjectListSession.Ready -> when {
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
}
