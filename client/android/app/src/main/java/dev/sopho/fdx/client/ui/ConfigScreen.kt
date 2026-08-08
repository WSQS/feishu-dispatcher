package dev.sopho.fdx.client.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.OutlinedButton
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
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.network.ViewerException
import dev.sopho.fdx.client.network.ZtManager
import dev.sopho.fdx.client.network.ZtState
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import java.net.URI

/** 测试连接的状态：idle（未测）/ 阶段提示（loading）/ 结果。 */
sealed interface TestState {
    data object Idle : TestState
    data class Loading(val message: String) : TestState
    data class Success(val version: String) : TestState
    data class Error(val message: String) : TestState
}

/**
 * 配置页：填 viewer 地址 + token + ZeroTier，保存（[ConnectionRepository] 持久化）+ 测试连接。
 *
 * 测试连接按 [Connection.zerotier.enabled] 走两条路：
 * - false → 普通 HTTP（CIO），直接 health()。
 * - true → libzt：先 ZtManager.startNode + 等 NetworkReady，再 health()（OkHttp+SocketFactory）。
 */
@Composable
fun ConfigScreen(
    repo: ConnectionRepository,
    storagePath: String,
    modifier: Modifier = Modifier,
    onConnected: ((Connection) -> Unit)? = null,
) {
    var connection by remember { mutableStateOf(Connection()) }
    var savedMsg by remember { mutableStateOf<String?>(null) }
    var testState by remember { mutableStateOf<TestState>(TestState.Idle) }
    var isTesting by remember { mutableStateOf(false) }
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
            onValueChange = { connection = connection.copy(url = it); savedMsg = null; testState = TestState.Idle },
            label = { Text("Viewer 地址") },
            placeholder = { Text("http://<ip>:7321") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = connection.token,
            onValueChange = { connection = connection.copy(token = it); savedMsg = null; testState = TestState.Idle },
            label = { Text("Bearer Token") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        // ZeroTier 开关行
        ListItem(
            headlineContent = { Text("ZeroTier") },
            supportingContent = { Text("启用 zerotier 组网") },
            trailingContent = {
                Switch(
                    checked = connection.zerotier.enabled,
                    onCheckedChange = {
                        connection = connection.copy(zerotier = connection.zerotier.copy(enabled = it))
                        savedMsg = null
                        testState = TestState.Idle
                    },
                )
            },
            modifier = Modifier.clickable {
                connection = connection.copy(
                    zerotier = connection.zerotier.copy(enabled = !connection.zerotier.enabled),
                )
                savedMsg = null
                testState = TestState.Idle
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
                        testState = TestState.Idle
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
                        testState = TestState.Idle
                    },
                    label = { Text("ZeroTier Moon ID（10 位 hex，可选）") },
                    placeholder = { Text("deadbeef00") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        // 保存 + 测试连接按钮行
        Row(
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            modifier = Modifier.align(Alignment.CenterHorizontally),
        ) {
            Button(
                onClick = {
                    scope.launch {
                        repo.save(connection.copy(url = connection.url.trim(), token = connection.token.trim()))
                        savedMsg = "已保存（杀进程重启仍在）"
                    }
                },
                enabled = connection.isValid && !isTesting,
            ) { Text("保存") }
            OutlinedButton(
                onClick = {
                    if (isTesting) return@OutlinedButton
                    isTesting = true
                    testState = TestState.Loading("连接中…")
                    scope.launch {
                        testState = runTest(connection, storagePath)
                        isTesting = false
                        if (testState is TestState.Success) onConnected?.invoke(connection)
                    }
                },
                enabled = connection.isValid && !isTesting,
            ) { Text("测试连接") }
        }
        savedMsg?.let { Text(it) }
        // 测试状态/结果显示
        when (val ts = testState) {
            TestState.Idle -> {}
            is TestState.Loading -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.height(16.dp))
                Text(ts.message, modifier = Modifier.padding(start = 8.dp))
            }
            is TestState.Success -> Text("✅ ok=true, version=${ts.version}")
            is TestState.Error -> Text("❌ ${ts.message}")
        }
    }
}

/**
 * 执行测试连接。按 useZerotier 走两条路，返回最终 [TestState]。
 * 中间态（Loading）通过 ZtManager.state 观察但不在这返回（简化：只返回最终态）。
 *
 * 注：本函数 suspend，调用方在 scope.launch 里调。中间 loading 提示靠 ZtManager.state
 * 流——v1 简化版直接等最终态，分阶段提示留后续增强。
 */
private suspend fun runTest(connection: Connection, storagePath: String): TestState {
    val url = connection.url.trim()
    val token = connection.token.trim()
    val host = try { URI(url).host } catch (e: Exception) { null } ?: return TestState.Error("无法解析地址")
    val port = try { URI(url).port } catch (e: Exception) { -1 }.let { if (it > 0) it else 7321 }

    return try {
        if (connection.zerotier.enabled) {
            // libzt 路径：先 startNode + 等 NetworkReady
            ZtManager.startNode(storagePath, connection.zerotier.networkId.trim(), connection.zerotier.moonId.trim())
            val ready = ZtManager.state.first { it is ZtState.NetworkReady || it is ZtState.Error }
            if (ready is ZtState.Error) return TestState.Error("ZT: ${ready.message}")
            ViewerClient(url, token, useZerotier = true, ztHost = host, ztPort = port).use {
                val h = it.health()
                TestState.Success(h.version)
            }
        } else {
            ViewerClient(url, token).use {
                val h = it.health()
                TestState.Success(h.version)
            }
        }
    } catch (e: ViewerException) {
        TestState.Error("${e.kind}: ${e.message}")
    } catch (e: Exception) {
        TestState.Error("${e.javaClass.simpleName}: ${e.message}")
    }
}
