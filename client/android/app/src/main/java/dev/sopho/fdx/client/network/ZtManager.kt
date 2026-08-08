package dev.sopho.fdx.client.network

import android.content.Context
import android.util.Log
import com.zerotier.sockets.ZeroTierNode
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * libzt ZeroTier node 的生命周期管理器（单例）。
 *
 * 把 libzt 的 `init / start / join / wait` 序列用协程包好，对外暴露一个
 * [StateFlow]<[ZtState]>，让 UI（#137「测试连接」按钮）能观察 ZT 进度并据此发 HTTP。
 *
 * 这是 #136（③-b）的产物：只管 node 生命周期 + 状态暴露，**不**管何时调
 * [startNode]（由 #137 整合时决定，本 App v1 不挂 Application.onCreate）。
 *
 * - libzt 的 `start()` 起的是 native 后台线程（不阻塞），但 `isOnline` /
 *   `isNetworkTransportReady` 要轮询——这里用协程 [delay] 轮询（不用 demo 的
 *   主线程 busy-wait），并配 [withTimeoutOrNull] 超时保护。
 * - node 的身份密钥对由 libzt 落盘到 `filesDir`，重启 App 自动复用，node id 稳定。
 *
 * 序列与 API 形态见 `docs/research/libzt-android.md` §4.1。
 */
object ZtManager {

    private const val TAG = "ZtManager"

    /** 等待 node 上线（拿到 node id）的超时（首次生成 identity 可能稍慢）。 */
    private const val NODE_ONLINE_TIMEOUT_MS = 30_000L

    /** 等待网络就绪（拿到虚拟 IP、可路由）的超时（首次走 Central 下发配置较慢）。 */
    private const val NETWORK_READY_TIMEOUT_MS = 30_000L

    /** 轮询间隔（替代 demo 的 `zts_util_delay(50)` busy-wait）。 */
    private const val POLL_INTERVAL_MS = 200L

    // 背景见 task：同一时刻只允许一个 node；用 holder + isRunning 保护。
    private val _state = MutableStateFlow<ZtState>(ZtState.Idle)
    /** ZT node 的当前状态（UI 观察用）。 */
    val state: StateFlow<ZtState> = _state.asStateFlow()

    /** 当前 node 实例（非 null 表示已 start，尚未 stop）。 */
    @Volatile
    private var node: ZeroTierNode? = null

    /**
     * 启动并加入网络。幂等：
     * - 已就绪（[ZtState.NetworkReady]）且 networkId 相同 → 直接返回；
     * - 已在运行中（Starting/Online/Joining）→ 先 [stopNode] 再重启（networkId 变更场景）；
     * - 其他 → 走完整 init/start/join/wait 序列。
     *
     * 任一步失败或超时 → 转入 [ZtState.Error]，并尝试 stop 已起的 node。
     *
     * [context] 任意 Context；内部取 applicationContext.filesDir 作 libzt 存储路径。
     * [networkId] 16 位 hex 的 ZeroTier network ID（如 `a1b2c3d4e5f60718`）。
     */
    suspend fun startNode(context: Context, networkId: String) {
        val nwid = try {
            networkId.trim().toLong(16)
        } catch (e: NumberFormatException) {
            _state.value = ZtState.Error("networkId 不是合法 16 位 hex：${e.message}")
            return
        }
        if (networkId.trim().isBlank()) {
            _state.value = ZtState.Error("networkId 为空")
            return
        }

        // 已就绪且 networkId 没变 → 不重复启动。
        val current = _state.value
        if (current is ZtState.NetworkReady && current.networkId == networkId.trim()) return

        // 之前的 node（networkId 变更或重新启动）先停掉。
        if (node != null) stopNode()

        val filesDir = context.applicationContext.filesDir.absolutePath
        _state.value = ZtState.Starting

        val zt = withContext(Dispatchers.IO) {
            try {
                val n = ZeroTierNode()
                // initFromStorage 必须在 start() 前调（身份/网络配置落盘路径）。
                n.initFromStorage(filesDir)
                n.start() // 起 native 后台线程，非阻塞
                n
            } catch (e: Exception) {
                _state.value = ZtState.Error("node 初始化失败：${e.message}")
                null
            }
        } ?: return

        node = zt

        // 3. 等 node 上线（拿到 node id）。
        val online = withContext(Dispatchers.IO) {
            withTimeoutOrNull(NODE_ONLINE_TIMEOUT_MS) {
                while (!zt.isOnline) delay(POLL_INTERVAL_MS)
                true
            } ?: false
        }
        if (!online) {
            _state.value = ZtState.Error("node 上线超时（${NODE_ONLINE_TIMEOUT_MS}ms）")
            stopNodeInternal()
            return
        }
        Log.i(TAG, "node online, id=${"%010x".format(zt.id)}")
        _state.value = ZtState.Online

        // 4. 加入网络。
        _state.value = ZtState.Joining(networkId.trim())
        withContext(Dispatchers.IO) { zt.join(nwid) }

        // 5. 等网络就绪（拿到虚拟 IP、可路由）。
        val ready = withContext(Dispatchers.IO) {
            withTimeoutOrNull(NETWORK_READY_TIMEOUT_MS) {
                while (!zt.isNetworkTransportReady(nwid)) delay(POLL_INTERVAL_MS)
                true
            } ?: false
        }
        if (!ready) {
            _state.value = ZtState.Error("网络就绪超时（${NETWORK_READY_TIMEOUT_MS}ms），确认 networkId 正确且节点已在 Central authorize")
            stopNodeInternal()
            return
        }
        Log.i(TAG, "network ready: $networkId")
        _state.value = ZtState.NetworkReady(networkId.trim())
    }

    /**
     * 停止 node，状态回 [ZtState.Idle]。重复调用安全。
     *
     * 注意：App 进程被杀时 libzt 的 native 线程会随之退出，但**显式 stop 更干净**
     * （identity 等状态落盘，下次冷启动更快）。
     */
    fun stopNode() {
        stopNodeInternal()
        _state.value = ZtState.Idle
    }

    private fun stopNodeInternal() {
        node?.let {
            runCatching { it.stop() }
                .onFailure { e -> Log.w(TAG, "node.stop() failed: ${e.message}") }
        }
        node = null
    }
}

/**
 * ZtManager 的状态机（UI / 调用方观察 [ZtManager.state]）。
 *
 * 转移（正常路径）：[Idle] → [Starting] → [Online] → [Joining] → [NetworkReady]。
 * 任一步失败或超时 → [Error]（之后再 startNode 可从 Error 重新起步）。
 * [stopNode] → [Idle]。
 */
sealed interface ZtState {
    /** 未启动。 */
    data object Idle : ZtState

    /** `node.start()` 已调，等 `isOnline`。 */
    data object Starting : ZtState

    /** node 上线，已拿到 node id，可 join。 */
    data object Online : ZtState

    /** `node.join(nwid)` 已调，等 `isNetworkTransportReady`；[networkId] 是去空格后的 hex 串。 */
    data class Joining(val networkId: String) : ZtState

    /**
     * 网络就绪，ZT socket 可用；此时 #137 可基于 libzt 发 HTTP。
     * [networkId] 是去空格后的 hex 串。
     */
    data class NetworkReady(val networkId: String) : ZtState

    /** 任一步失败或超时；[message] 给人看。 */
    data class Error(val message: String) : ZtState
}
