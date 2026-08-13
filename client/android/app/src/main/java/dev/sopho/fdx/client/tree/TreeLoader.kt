package dev.sopho.fdx.client.tree

import dev.sopho.fdx.client.network.TreeChildrenEntry
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
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

    private val trackingLock = Any()
    private val activeLoads = mutableMapOf<String, Job>()
    private val generations = mutableMapOf<String, Long>()
    private var started = false
    private var closed = false

    /** 进入页面：只加载根目录；重复调用不产生第二次请求。 */
    fun start() {
        val job = synchronized(trackingLock) {
            if (started || closed) return
            started = true
            _state.update { it.setLoading(ROOT_PATH, true) }
            registerLoad(ROOT_PATH)
        }
        job?.start()
    }

    /** 展开/折叠目录：冷目录展开触发加载；折叠取消在途请求并使其响应失效。 */
    fun toggle(path: String) {
        var jobToCancel: Job? = null
        val jobToStart = synchronized(trackingLock) {
            if (closed) return
            val after = _state.updateAndGet { it.toggle(path) }
            if (path !in after.expandedPaths) {
                jobToCancel = unregisterLoad(path)
                null
            } else if (after.directories[path]?.loading == true) {
                registerLoad(path)
            } else {
                null
            }
        }
        jobToCancel?.cancel()
        jobToStart?.start()
    }

    /** 离开页面：取消全部在途加载，并让迟到响应失效。 */
    fun close() {
        val jobs = synchronized(trackingLock) {
            if (closed) return
            closed = true
            started = true
            activeLoads.keys.forEach { path ->
                generations[path] = (generations[path] ?: 0L) + 1
            }
            activeLoads.values.toList().also { activeLoads.clear() }
        }
        jobs.forEach { it.cancel() }
    }

    /** 必须在 [trackingLock] 内调用；返回的 lazy job 由调用方在离开锁后启动。 */
    private fun registerLoad(path: String): Job? {
        if (activeLoads.containsKey(path)) return null
        val gen = generations[path] ?: 0L
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                val entries = loadChildren(path)
                updateIfCurrent(path, gen) { it.setChildren(path, entries) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                updateIfCurrent(path, gen) {
                    it.setError(path, e.message ?: e.javaClass.simpleName)
                }
            }
        }
        activeLoads[path] = job
        job.invokeOnCompletion {
            synchronized(trackingLock) {
                if (activeLoads[path] === job) activeLoads.remove(path)
            }
        }
        return job
    }

    /** 必须在 [trackingLock] 内调用。 */
    private fun unregisterLoad(path: String): Job? {
        generations[path] = (generations[path] ?: 0L) + 1
        return activeLoads.remove(path)
    }

    private inline fun updateIfCurrent(path: String, gen: Long, update: (TreeState) -> TreeState) {
        synchronized(trackingLock) {
            if ((generations[path] ?: 0L) == gen) _state.update(update)
        }
    }
}
