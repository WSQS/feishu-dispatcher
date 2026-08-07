package dev.sopho.fdx.client.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
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
import dev.sopho.fdx.client.data.ConnectionStore
import kotlinx.coroutines.launch

/**
 * 配置页：填 viewer 地址 + token，保存（DataStore 持久化）。
 *
 * 本页只管存配置；「测试连接」按钮归 #125 整合时连网络层（#122）。
 * 启动时从 DataStore 读回填进输入框，验证持久化。
 */
@Composable
fun ConfigScreen(
    store: ConnectionStore,
    modifier: Modifier = Modifier,
) {
    var url by remember { mutableStateOf("") }
    var token by remember { mutableStateOf("") }
    var savedMsg by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    // 启动时读已存的配置，填进输入框（验证持久化）
    LaunchedEffect(Unit) {
        store.load()?.let { url = it.url; token = it.token }
    }

    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        OutlinedTextField(
            value = url,
            onValueChange = { url = it; savedMsg = null },
            label = { Text("Viewer 地址") },
            placeholder = { Text("http://<ip>:7321") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = token,
            onValueChange = { token = it; savedMsg = null },
            label = { Text("Bearer Token") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(4.dp))
        Button(
            onClick = {
                scope.launch {
                    store.save(url.trim(), token.trim())
                    savedMsg = "已保存（杀进程重启仍在）"
                }
            },
            enabled = Connection(url, token).isValid,
            modifier = Modifier.align(Alignment.CenterHorizontally),
        ) { Text("保存") }
        savedMsg?.let { Text(it) }
    }
}
