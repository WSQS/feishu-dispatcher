package dev.sopho.fdx.client.network

import android.util.Log
import com.zerotier.sockets.ZeroTierNative
import com.zerotier.sockets.ZeroTierNode
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * libzt node 的生命周期状态。状态机顺序：
 *
 * `Idle` → `Starting`（node.start 后等 isOnline）→ `Online`（join 后）→ `Joining`
 * → `NetworkReady`（socket 可用）；任一步失败 → `Error`；`stopNode()` 回 `Idle`。
 */
sealed interface ZtState {
    /** 未启动（初始 / stopNode 后）。 */
    data object Idle : ZtState

    /** node.start() 后，等 node 上线（拿到 node id）。 */
    data object Starting : ZtState

    /** node 已上线，正在加入网络。 */
    data object Online : ZtState

    /** 已 join 网络，等 isNetworkTransportReady（拿到虚拟 IP、可路由）。 */
    data object Joining : ZtState

    /** 网络就绪，libzt socket 可用。 */
    data object NetworkReady : ZtState

    /** 任一步失败。 [message] 描述原因。 */
    data class Error(val message: String) : ZtState
}

/**
 * libzt node 生命周期管理（单例）。用协程 + [StateFlow] 暴露状态，禁用 demo 的主线程
 * busy-wait（用 [withTimeoutOrNull] + 协程 [delay] 轮询，超时转 [ZtState.Error]）。
 *
 * 用法：\`ZtManager.startNode(context.filesDir.absolutePath, networkId)\`，
 * 观察态用 \`ZtManager.state.collect { ... }\`。停止用 [stopNode]。
 *
 * 单例（`object`）—— libzt node 进程内一份；filesDir 由调用方传（startNode 的 storagePath）。
 */
object ZtManager {
    private const val TAG = "ZtManager"
    private var node: ZeroTierNode? = null

    private val _state = MutableStateFlow<ZtState>(ZtState.Idle)
    /** 当前 node 状态；UI 观察用。 */
    val state: StateFlow<ZtState> = _state.asStateFlow()

    /**
     * 启动 node 并加入 [networkId]。协程包好 init/start/join/wait 序列：
     *
     * 1. \`initFromStorage(storagePath)\` + \`start()\` → 等 \`isOnline\`
     * 2. \`join(nwid)\` → 等 \`isNetworkTransportReady(nwid)\`
     *
     * 每步更新 [_state]；任一步超时（默认 30s）或失败 → [ZtState.Error]。轮询在
     * [Dispatchers.IO] 上，不阻塞调用方的协程上下文（但仍 suspend，调用方应避开主线程）。
     *
     * [networkId] 16 位 hex（如 \`a1b2c3d4e5f60718\`），内部 \`toLong(16)\`。
     */
    suspend fun startNode(
        storagePath: String,
        networkId: String,
        moonId: String = "",
        timeoutMs: Long = 30_000,
    ) = withContext(Dispatchers.IO) {
        // 已就绪/已在跑 → 幂等直接返回（避免重复 start）
        if (_state.value is ZtState.NetworkReady) return@withContext
        // 旧 node 还在 → 先停
        stopNode()

        val n = ZeroTierNode()
        node = n
        try {
            _state.value = ZtState.Starting
            val t0 = System.nanoTime()
            Log.i(TAG, "startNode: initFromStorage + start")
            n.initFromStorage(storagePath)
            n.start()

            // 等 node 上线（轮询 isOnline，超时转 Error）
            val onlineOk = withTimeoutOrNull(timeoutMs) {
                while (!n.isOnline) delay(50)
            }
            val onlineMs = (System.nanoTime() - t0) / 1_000_000
            if (onlineOk == null) {
                Log.w(TAG, "startNode: node online timeout (${timeoutMs}ms)")
                _state.value = ZtState.Error("等 node 上线超时（${timeoutMs}ms）")
                return@withContext
            }
            Log.i(TAG, "startNode: node online in ${onlineMs}ms")
            _state.value = ZtState.Online

            // moon（可选）：moonId 非空时 orbit
            if (moonId.isNotBlank()) {
                val moon = moonId.toLong(16)
                Log.i(TAG, "startNode: orbit moon $moonId")
                ZeroTierNative.zts_moon_orbit(moon, moon)
            }

            // 加入网络
            val nwid = networkId.toLong(16)
            val t1 = System.nanoTime()
            n.join(nwid)
            _state.value = ZtState.Joining

            // 等网络就绪（轮询 isNetworkTransportReady）
            val readyOk = withTimeoutOrNull(timeoutMs) {
                while (!n.isNetworkTransportReady(nwid)) delay(50)
            }
            val readyMs = (System.nanoTime() - t1) / 1_000_000
            if (readyOk == null) {
                Log.w(TAG, "startNode: network ready timeout (${timeoutMs}ms)")
                _state.value = ZtState.Error("等网络就绪超时（${timeoutMs}ms）；检查 networkId 或 ZeroTier Central 是否 authorize 了本节点")
                return@withContext
            }
            val totalMs = (System.nanoTime() - t0) / 1_000_000
            Log.i(TAG, "startNode: network ready in ${readyMs}ms (join->ready), total ${totalMs}ms")
            _state.value = ZtState.NetworkReady
        } catch (e: NumberFormatException) {
            _state.value = ZtState.Error("networkId/moonId 不是合法 hex：${e.message}")
        } catch (e: Exception) {
            _state.value = ZtState.Error("${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /** 停 node，状态回 Idle。重复调用安全。 */
    fun stopNode() {
        node?.let {
            runCatching { it.stop() }
        }
        node = null
        _state.value = ZtState.Idle
    }
}
