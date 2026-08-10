package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.sopho.fdx.client.network.TreeEntry
import dev.sopho.fdx.client.network.ViewerClient
import kotlinx.coroutines.launch

/**
 * 文件树数据：随目的地（NavBackStackEntry）存活，返回时复用缓存不重拉。
 */
class TreeViewModel(private val projectName: String) : ViewModel() {
    var entries by mutableStateOf<List<TreeEntry>?>(null)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    private var started = false

    fun start(client: ViewerClient) {
        if (started) return
        started = true
        viewModelScope.launch {
            runCatching { client.tree(projectName).entries }
                .onSuccess { entries = it }
                .onFailure {
                    Log.e("TreeScreen", "failed to load tree", it)
                    error = it.message
                }
        }
    }
}
