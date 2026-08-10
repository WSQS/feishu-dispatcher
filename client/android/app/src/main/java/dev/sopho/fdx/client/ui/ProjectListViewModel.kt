package dev.sopho.fdx.client.ui

import android.util.Log
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.sopho.fdx.client.network.ProjectItem
import dev.sopho.fdx.client.network.ViewerClient
import kotlinx.coroutines.launch

/**
 * 项目列表数据：随目的地（NavBackStackEntry）存活，返回时复用缓存不重拉。
 */
class ProjectListViewModel : ViewModel() {
    var projects by mutableStateOf<List<ProjectItem>?>(null)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    private var started = false

    fun start(client: ViewerClient) {
        if (started) return
        started = true
        viewModelScope.launch {
            runCatching { client.projects().items }
                .onSuccess { projects = it }
                .onFailure {
                    Log.e("ProjectList", "failed to load projects", it)
                    error = it.message
                }
        }
    }
}
