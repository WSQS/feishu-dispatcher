package dev.sopho.fdx.client

import android.os.Bundle
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.FileContentScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.coroutines.CancellationException
import kotlin.math.abs
import kotlin.math.min

/**
 * 导航目的地：sealed class 表示每个屏 + 它所需的参数。
 *
 * - [Config]：配置/连接页（栈底，根屏）。
 * - [ProjectList]：项目列表（连接成功后进入）。
 * - [Tree]：某项目的文件树（点项目进入，带 projectName）。
 * - [FileContent]：文件内容（点树里文件进入，带 projectName + path）。
 *
 * 后续加 Diff 屏时，在这里加一个子类即可。
 */
sealed class Destination {
    data object Config : Destination()

    data object ProjectList : Destination()

    data class Tree(val projectName: String) : Destination()

    data class FileContent(val projectName: String, val path: String) : Destination()
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    // 连接得到的 client：ProjectList / Tree 都靠它请求 daemon。
                    // 连接成功建一次，随导航在各屏间传递（与原逻辑一致，只是改为按栈顶渲染）。
                    var client by remember { mutableStateOf<ViewerClient?>(null) }

                    // 屏幕栈：栈底恒为 Config（根屏）。push 压入新屏，pop 弹回上一屏。
                    val screenStack = remember { mutableStateListOf<Destination>(Destination.Config) }

                    fun push(dest: Destination) {
                        // 同目的地不重复压栈（避免连点造成栈里塞多个相同项）。
                        if (screenStack.last() != dest) screenStack.add(dest)
                    }

                    fun pop(): Boolean {
                        if (screenStack.size <= 1) return false
                        screenStack.removeAt(screenStack.lastIndex)
                        return true
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

                    Box(Modifier.fillMaxSize()) {
                        // 底层（Z 序在下）：返回手势期间渲染上一屏（原地不动，被缩小的当前屏露出）。
                        // backProgress 为 0 时不渲染（正常状态只有顶层）。预览不可交互（回调 no-op）。
                        if (backProgress > 0f && screenStack.size > 1) {
                            val prevDest = screenStack[screenStack.size - 2]
                            RenderDestination(
                                dest = prevDest,
                                store = store,
                                storagePath = applicationContext.filesDir.absolutePath,
                                client = client,
                                onConnected = {},
                                onProjectClick = {},
                                onFileClick = { _, _ -> },
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
                                storagePath = applicationContext.filesDir.absolutePath,
                                client = client,
                                onConnected = { c ->
                                    client = c
                                    push(Destination.ProjectList)
                                },
                                onProjectClick = { name -> push(Destination.Tree(name)) },
                                onFileClick = { project, path ->
                                    push(Destination.FileContent(project, path))
                                },
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
    client: ViewerClient?,
    onConnected: (ViewerClient) -> Unit,
    onProjectClick: (String) -> Unit,
    onFileClick: (String, String) -> Unit,
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

        is Destination.ProjectList -> {
            // client 在 onConnected 中先于 push(ProjectList) 赋值，
            // 故到达此分支时 client 非空（与原 when 中 c != null 同义）。
            val c = client!!
            ProjectListScreen(client = c, onProjectClick = onProjectClick, modifier = modifier)
        }

        is Destination.Tree -> {
            val c = client!!
            TreeScreen(
                client = c,
                projectName = dest.projectName,
                onFileClick = { path -> onFileClick(dest.projectName, path) },
                modifier = modifier,
            )
        }

        is Destination.FileContent -> {
            val c = client!!
            FileContentScreen(
                client = c,
                projectName = dest.projectName,
                path = dest.path,
                modifier = modifier,
            )
        }
    }
}
