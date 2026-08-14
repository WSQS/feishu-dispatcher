package dev.sopho.fdx.client.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.tree.TreeLoader
import dev.sopho.fdx.client.tree.TreeState
import kotlinx.coroutines.flow.StateFlow

/**
 * 文件树数据：随目的地（NavBackStackEntry）存活，按访问目录加载并在返回时复用已访问状态。
 */
class TreeViewModel(
    projectName: String,
    client: ViewerClient,
) : ViewModel() {
    private val loader = TreeLoader(viewModelScope) { path ->
        client.treeChildren(projectName, path).entries
    }
    val state: StateFlow<TreeState> = loader.state

    init {
        loader.start()
    }

    fun toggle(path: String) = loader.toggle(path)

    fun retry(path: String) = loader.retry(path)

    override fun onCleared() {
        loader.close()
    }
}
