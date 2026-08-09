package dev.sopho.fdx.client

import android.os.Bundle
import android.util.Log
import androidx.activity.BackEventCompat
import androidx.activity.ComponentActivity
import androidx.activity.compose.PredictiveBackHandler
import androidx.activity.compose.setContent
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.spring
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.data.Connection
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.network.ViewerException
import dev.sopho.fdx.client.network.ZtManager
import dev.sopho.fdx.client.network.ZtState
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.ProjectListSession
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.first
import kotlin.math.abs
import kotlin.math.min

/**
 * 导航目的地：sealed class 表示每个屏 + 它所需的参数。
 *
 * - [ProjectList]：项目列表（栈底，根屏）；后台用 DataStore 配置建连。
 * - [Config]：连接设置页（从 ProjectList 齿轮进入，可 push）。
 * - [Tree]：某项目的文件树（点项目进入，带 projectName）。
 *
 * 后续加 FileContent / Diff 屏时，在这里加一个子类即可。
 */
sealed class Destination {
    data object ProjectList : Destination()

    data object Config : Destination()

    data class Tree(val projectName: String) : Destination()
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        val storagePath = applicationContext.filesDir.absolutePath
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    // 根屏会话：启动即进 ProjectList，后台读 DataStore 建连。
                    var session by remember {
                        mutableStateOf<ProjectListSession>(ProjectListSession.Connecting)
                    }
                    // 递增以触发重试（失败态「重试」或设置页测连成功后也可再走后台路径）。
                    var connectAttempt by remember { mutableIntStateOf(0) }

                    // 屏幕栈：栈底恒为 ProjectList（根屏）。push 压入新屏，pop 弹回上一屏。
                    val screenStack = remember {
                        mutableStateListOf<Destination>(Destination.ProjectList)
                    }

                    fun push(dest: Destination) {
                        // 同目的地不重复压栈（避免连点造成栈里塞多个相同项）。
                        if (screenStack.last() != dest) screenStack.add(dest)
                    }

                    fun pop(): Boolean {
                        if (screenStack.size <= 1) return false
                        screenStack.removeAt(screenStack.lastIndex)
                        return true
                    }

                    fun adoptClient(client: ViewerClient) {
                        val prev = (session as? ProjectListSession.Ready)?.client
                        session = ProjectListSession.Ready(client)
                        if (prev != null && prev !== client) prev.close()
                    }

                    LaunchedEffect(connectAttempt) {
                        session = ProjectListSession.Connecting
                        val conn = store.load()
                        if (conn == null || !conn.isValid || !conn.zerotier.isValid) {
                            session = ProjectListSession.Unconfigured
                            return@LaunchedEffect
                        }
                        val client = ViewerClient.fromConnection(conn)
                        try {
                            probeConnection(conn, storagePath, client)
                            adoptClient(client)
                        } catch (c: CancellationException) {
                            client.close()
                            throw c
                        } catch (e: Exception) {
                            client.close()
                            Log.w("MainActivity", "background connect failed", e)
                            // 设置页可能已先连上：失败时不要盖掉 Ready。
                            if (session !is ProjectListSession.Ready) {
                                session = ProjectListSession.Failed(formatConnectError(e))
                            }
                        }
                    }

                    // 返回手势状态——对齐 AOSP CrossActivityBackAnimation 的动效参数：
                    // pre-commit（手势进行中）当前屏缩小到 0.9 + 跟随手指方向偏移（含 Y 轴），
                    // 不做 alpha 渐变（系统靠 scrim 遮罩挡住缝隙，不是靠透明度）。
                    // post-commit（松手后）才用 alpha 快速渐隐（max(1 - progress*5, 0)）。
                    var backProgress by remember { mutableFloatStateOf(0f) }
                    var touchStartY by remember { mutableFloatStateOf(0f) }
                    var touchY by remember { mutableFloatStateOf(0f) }
                    var swipeEdge by remember { mutableIntStateOf(BackEventCompat.EDGE_LEFT) }
                    var isCommitting by remember { mutableStateOf(false) }
                    val commitProgress = remember { Animatable(0f) }
                    val screenHeightPx = with(LocalDensity.current) { LocalDensity.current.run { 0.dp } }

                    Box(modifier = Modifier.fillMaxSize()) {
                        // 底层（Z 序在下）：返回手势期间渲染上一屏（原地不动，被缩小的当前屏露出）。
                        // backProgress 为 0 时不渲染（正常状态只有顶层）。预览不可交互（回调 no-op）。
                        if (backProgress > 0f && screenStack.size > 1) {
                            val prevDest = screenStack[screenStack.size - 2]
                            RenderDestination(
                                dest = prevDest,
                                store = store,
                                storagePath = storagePath,
                                session = session,
                                onConnected = {},
                                onOpenSettings = {},
                                onRetryConnect = {},
                                onProjectClick = {},
                            )
                        }

                        // scrim 遮罩：黑色半透明，盖在上一屏上方、当前屏下方。
                        // 对齐 AOSP：浅色模式 alpha 0.2，深色模式 0.8——这里取 0.2。
                        if (backProgress > 0f && screenStack.size > 1) {
                            Box(
                                Modifier
                                    .fillMaxSize()
                                    .background(Color.Black.copy(alpha = 0.2f * backProgress))
                            )
                        }

                        // 顶层：当前屏。手势期间缩小 + 跟随手指偏移（对齐 AOSP CrossActivityBackAnimation）。
                        // 前进（push）路径用对称 fade；返回路径由 PredictiveBackHandler 的手势进度驱动。
                        AnimatedContent(
                            targetState = screenStack.last(),
                            transitionSpec = { fadeIn() togetherWith fadeOut() },
                            label = "navTransition",
                            modifier = Modifier.graphicsLayer {
                                val gestureProgress = backProgress
                                val currentAlpha = if (isCommitting) {
                                    // post-commit：快速渐隐，对齐 AOSP max(1 - progress*5, 0)
                                    val cp = commitProgress.value
                                    (1f - cp * 5f).coerceAtLeast(0f)
                                } else {
                                    // pre-commit：不透明（AOSP 用 scrim 不是 alpha）
                                    1f
                                }
                                // 缩小：1.0 → 0.9（AOSP MAX_SCALE）
                                val scale = 1f - 0.1f * gestureProgress
                                scaleX = scale
                                scaleY = scale
                                // 位移：水平方向跟随 swipeEdge（左边缘滑→往左偏，右边缘滑→往右偏）
                                val direction = if (swipeEdge == BackEventCompat.EDGE_LEFT) -1f else 1f
                                translationX = direction * size.width * 0.1f * gestureProgress
                                // 位移：垂直方向跟随手指 Y（对齐 AOSP getYOffset）
                                val rawYDelta = touchY - touchStartY
                                val yDirection = if (rawYDelta < 0) -1f else 1f
                                val deltaYRatio = min(size.height / 2f, abs(rawYDelta)) / (size.height / 2f)
                                val maxShiftY = ((size.height - size.height * scale) / 2f).coerceAtLeast(0f)
                                translationY = maxShiftY * deltaYRatio * yDirection
                                alpha = currentAlpha
                            },
                        ) { dest ->
                            RenderDestination(
                                dest = dest,
                                store = store,
                                storagePath = storagePath,
                                session = session,
                                onConnected = { c -> adoptClient(c) },
                                onOpenSettings = { push(Destination.Config) },
                                onRetryConnect = { connectAttempt += 1 },
                                onProjectClick = { name -> push(Destination.Tree(name)) },
                            )
                        }
                    }

                    // 返回手势：栈深 > 1 时拦截。手势进度实时驱动当前屏的缩小/位移；
                    // 松手完成 → post-commit 动画（alpha 快速渐隐），再 pop + 归零；
                    // 中途取消 → spring 弹回原位。
                    // 根屏（栈深 ≤ 1）enabled = false，系统接管退出 App 的 predictive back。
                    PredictiveBackHandler(enabled = screenStack.size > 1) { events ->
                        backProgress = 0f
                        isCommitting = false
                        var firstEvent = true
                        try {
                            events.collect { e: BackEventCompat ->
                                if (firstEvent) {
                                    touchStartY = e.touchY
                                    firstEvent = false
                                }
                                touchY = e.touchY
                                swipeEdge = e.swipeEdge
                                backProgress = e.progress
                            }
                            // Flow 正常结束 → 手势完成（commit）：post-commit 动画——alpha 快速渐隐，
                            // 对齐 AOSP 的 max(1 - progress*5, 0)，时长 ~450ms。
                            isCommitting = true
                            commitProgress.snapTo(0f)
                            commitProgress.animateTo(1f, spring())
                            pop()
                            backProgress = 0f
                            isCommitting = false
                        } catch (c: CancellationException) {
                            // Flow 被取消 → 手势取消：spring 弹回原位。
                            backProgress = 0f
                            isCommitting = false
                            throw c
                        }
                    }
                }
            }
        }
    }
}

/** 按目的地渲染对应的 Screen。当前屏和预览的上一屏都调它（预览时回调传 no-op）。 */
@Composable
private fun RenderDestination(
    dest: Destination,
    store: ConnectionStore,
    storagePath: String,
    session: ProjectListSession,
    onConnected: (ViewerClient) -> Unit,
    onOpenSettings: () -> Unit,
    onRetryConnect: () -> Unit,
    onProjectClick: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    when (dest) {
        is Destination.Config -> ConfigScreen(
            repo = store,
            storagePath = storagePath,
            // onConnected 接收已建好并测过 health 的 client（复用，不再重建）。
            onConnected = onConnected,
            modifier = modifier,
        )

        is Destination.ProjectList -> ProjectListScreen(
            session = session,
            onOpenSettings = onOpenSettings,
            onRetryConnect = onRetryConnect,
            onProjectClick = onProjectClick,
            modifier = modifier,
        )

        is Destination.Tree -> {
            val client = (session as? ProjectListSession.Ready)?.client
            if (client != null) {
                TreeScreen(client = client, projectName = dest.projectName, modifier = modifier)
            }
        }
    }
}

/**
 * 启 ZT（若需要）并对 [client] 调 health。成功不关 client；失败抛给调用方关闭。
 * 与 ConfigScreen 测试连接同路径（后台自动连复用同一语义）。
 */
private suspend fun probeConnection(
    connection: Connection,
    storagePath: String,
    client: ViewerClient,
) {
    if (connection.zerotier.enabled) {
        ZtManager.startNode(
            storagePath,
            connection.zerotier.networkId.trim(),
            connection.zerotier.moonId.trim(),
        )
        val ready = ZtManager.state.first { it is ZtState.NetworkReady || it is ZtState.Error }
        if (ready is ZtState.Error) error("ZT: ${ready.message}")
    }
    client.health()
}

private fun formatConnectError(e: Exception): String = when (e) {
    is ViewerException -> "${e.kind}: ${e.message}"
    else -> "${e.javaClass.simpleName}: ${e.message}"
}
