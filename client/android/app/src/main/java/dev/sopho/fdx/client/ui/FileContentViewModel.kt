package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.sopho.fdx.client.network.FileResponse
import dev.sopho.fdx.client.network.ViewerClient
import kotlinx.coroutines.launch

/**
 * 文件内容数据：随目的地（NavBackStackEntry）存活，返回时复用缓存不重拉。
 */
class FileContentViewModel(
    private val projectName: String,
    private val path: String,
) : ViewModel() {
    var file by mutableStateOf<FileResponse?>(null)
        private set
    /** content 拆分一次的结果（渲染用），避免每次重组重复 split。 */
    var lines by mutableStateOf<List<String>?>(null)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    private var started = false

    fun start(client: ViewerClient) {
        if (started) return
        started = true
        viewModelScope.launch {
            runCatching { client.file(projectName, path) }
                .onSuccess {
                    file = it
                    lines = it.content.split("\n")
                }
                .onFailure {
                    Log.e("FileContent", "failed to load file", it)
                    error = it.message
                }
        }
    }
}
