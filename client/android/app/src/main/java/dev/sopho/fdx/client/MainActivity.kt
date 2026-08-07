package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview
import dev.sopho.fdx.client.network.HealthResponse
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.network.ViewerException
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.coroutines.launch

// 临时硬编码的 viewer 地址 + token（#123 配置页会替换成可输入 + DataStore 持久化）。
// 验证时改成你 daemon 的实际地址（局域网/zerotier IP）+ 日志里打印的 token。
private const val VIEWER_URL = "http://10.0.2.2:7321"  // 10.0.2.2 = 模拟器访本机宿主
private const val VIEWER_TOKEN = "REPLACE_ME"

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            FdxViewerTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { inner ->
                    HealthScreen(modifier = Modifier.padding(inner))
                }
            }
        }
    }
}

@Composable
private fun HealthScreen(modifier: Modifier = Modifier) {
    // health() 用一次 client；正式配置页改用注入的 client（#123/#125）。
    val client = remember { ViewerClient(VIEWER_URL, VIEWER_TOKEN) }
    val scope = rememberCoroutineScope()
    var result by remember { mutableStateOf<Result<HealthResponse>?>(null) }

    Column(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Button(onClick = {
            scope.launch {
                result = runCatching { client.health() }
            }
        }) {
            Text("测试连接 /api/health")
        }
        Text(text = result?.format() ?: "（未测试）", style = MaterialTheme.typography.bodyMedium)
    }
}

private fun Result<HealthResponse>.format(): String = fold(
    onSuccess = { "✅ ok=${it.ok}, version=${it.version}" },
    onFailure = { e ->
        when (e) {
            is ViewerException -> "❌ ${e.kind}: ${e.message}"
            else -> "❌ ${e.javaClass.simpleName}: ${e.message}"
        }
    },
)

@Preview(showBackground = true)
@Composable
private fun HealthScreenPreview() {
    FdxViewerTheme {
        HealthScreen()
    }
}
