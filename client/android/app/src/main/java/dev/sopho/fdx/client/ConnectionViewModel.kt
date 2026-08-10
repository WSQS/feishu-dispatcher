package dev.sopho.fdx.client

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import dev.sopho.fdx.client.network.ViewerClient

/**
 * 连接（ViewerClient）：Activity scope，跨配置变更存活；进程死亡仍丢失（退回 Config 重连）。
 */
class ConnectionViewModel : ViewModel() {
    var client by mutableStateOf<ViewerClient?>(null)
        private set

    fun connect(c: ViewerClient) {
        client = c
    }
}
