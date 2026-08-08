package dev.sopho.fdx.client.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ListItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.data.Connection
import dev.sopho.fdx.client.data.ConnectionRepository
import kotlinx.coroutines.launch

/**
 * 配置页：填 viewer 地址 + token，保存（[ConnectionRepository] 持久化）。
 *
 * UI 对整个 [Connection] 模型读写——字段改动用 `connection.copy(...)`，保存存整个模型。
 * 不依赖具体存储（参数是 [ConnectionRepository] 接口）；启动读回填，验证持久化。
 */
@Composable
fun ConfigScreen(
    repo: ConnectionRepository,
    modifier: Modifier = Modifier,
) {
    var connection by remember { mutableStateOf(Connection()) }
    var savedMsg by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    // 启动时读已存的配置，填进输入框（验证持久化）
    LaunchedEffect(Unit) {
        repo.load()?.let { connection = it }
    }

    Column(
        modifier = modifier.padding(24.dp),
    ) {
        OutlinedTextField(
            value = connection.url,
            onValueChange = { connection = connection.copy(url = it); savedMsg = null },
            label = { Text("Viewer 地址") },
            placeholder = { Text("http://<ip>:7321") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = connection.token,
            onValueChange = { connection = connection.copy(token = it); savedMsg = null },
            label = { Text("Bearer Token") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        // ZeroTier 开关行：ListItem（标准设置行，整行可点 + trailing Switch）
        ListItem(
            headlineContent = { Text("ZeroTier") },
            supportingContent = { Text("启用 zerotier 组网") },
            trailingContent = {
                Switch(
                    checked = connection.zerotier.enabled,
                    onCheckedChange = {
                        connection = connection.copy(zerotier = connection.zerotier.copy(enabled = it))
                        savedMsg = null
                    },
                )
            },
            modifier = Modifier.clickable {
                connection = connection.copy(
                    zerotier = connection.zerotier.copy(enabled = !connection.zerotier.enabled),
                )
                savedMsg = null
            },
        )
        Spacer(Modifier.height(12.dp))
        AnimatedVisibility(
            visible = connection.zerotier.enabled,
            enter = expandVertically() + fadeIn(),
            exit = shrinkVertically() + fadeOut(),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = connection.zerotier.networkId,
                    onValueChange = {
                        connection = connection.copy(zerotier = connection.zerotier.copy(networkId = it))
                        savedMsg = null
                    },
                    label = { Text("ZeroTier 网络 ID（16 位 hex）") },
                    placeholder = { Text("a1b2c3d4e5f60718") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = connection.zerotier.moonId,
                    onValueChange = {
                        connection = connection.copy(zerotier = connection.zerotier.copy(moonId = it))
                        savedMsg = null
                    },
                    label = { Text("ZeroTier Moon ID（10 位 hex，可选）") },
                    placeholder = { Text("deadbeef00") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = {
                scope.launch {
                    repo.save(connection.copy(url = connection.url.trim(), token = connection.token.trim()))
                    savedMsg = "已保存（杀进程重启仍在）"
                }
            },
            enabled = connection.isValid,
            modifier = Modifier.align(Alignment.CenterHorizontally),
        ) { Text("保存") }
        savedMsg?.let { Text(it) }
    }
}
