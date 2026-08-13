package dev.sopho.fdx.client.tree

import dev.sopho.fdx.client.network.TreeChildrenEntry
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.flow.updateAndGet
import kotlinx.coroutines.launch

/**
 * 目录树加载协调器：把展开/折叠与目录加载结果应用到 [TreeState]，
 * 并保证并发正确性——同路径去重、generation 防迟到覆盖、折叠/关闭时取消在途请求。
 * 通过 [loadChildren] 与数据源解耦，不依赖 Android/Compose，可在 JVM 单测中验证。
 */
class TreeLoader(
    private val scope: CoroutineScope,
    private val loadChildren: suspend (path: String) -> List<TreeChildrenEntry>,
) {
    private val _state = MutableStateFlow(TreeState())
    val state: StateFlow<TreeState> = _state.asStateFlow()

    private val activeLoads = mutableMapOf<String, Job>()
    private val generations = mutableMapOf<String, Long>()
    private var started = false

    /** 进入页面：只加载根目录；重复调用不产生第二次请求。 */
    fun start() {
        if (started) return
        started = true
        _state.update { it.setLoading(ROOT_PATH, true) }
        load(ROOT_PATH)
    }

    /** 展开/折叠目录：冷目录展开触发加载；折叠取消在途请求并使其响应失效。 */
    fun toggle(path: String) {
        val after = _state.updateAndGet { it.toggle(path) }
        if (path !in after.expandedPaths) {
            cancelLoad(path)
        } else if (after.directories[path]?.loading == true) {
            load(path)
        }
    }

    /** 离开页面：取消全部在途加载，并让迟到响应失效。 */
    fun close() {
        started = true
        activeLoads.keys.toList().forEach { cancelLoad(it) }
    }

    private fun load(path: String) {
        if (activeLoads.containsKey(path)) return
        val gen = generations[path] ?: 0L
        lateinit var job: Job
        job = scope.launch {
            try {
                val entries = loadChildren(path)
                if (isCurrent(path, gen)) {
                    _state.update { it.setChildren(path, entries) }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (isCurrent(path, gen)) {
                    _state.update { it.setError(path, e.message ?: e.javaClass.simpleName) }
                }
            } finally {
                if (activeLoads[path] === job) activeLoads.remove(path)
            }
        }
        activeLoads[path] = job
        if (job.isCompleted && activeLoads[path] === job) activeLoads.remove(path)
    }

    private fun cancelLoad(path: String) {
        generations[path] = (generations[path] ?: 0L) + 1
        activeLoads.remove(path)?.cancel()
    }

    private fun isCurrent(path: String, gen: Long): Boolean =
        (generations[path] ?: 0L) == gen
}
