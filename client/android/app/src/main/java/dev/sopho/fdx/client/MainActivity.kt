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
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.coroutines.CancellationException

/**
 * 导航目的地：sealed class 表示每个屏 + 它所需的参数。
 *
 * - [Config]：配置/连接页（栈底，根屏）。
 * - [ProjectList]：项目列表（连接成功后进入）。
 * - [Tree]：某项目的文件树（点项目进入，带 projectName）。
 *
 * 后续加 FileContent / Diff 屏时，在这里加一个子类即可。
 */
sealed class Destination {
    data object Config : Destination()

    data object ProjectList : Destination()

    data class Tree(val projectName: String) : Destination()
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

                    // 返回手势进度（0 = 未触发，1 = 完全滑出）。驱动当前屏的位移/缩放，
                    // 以及底层上一屏的显隐——实现 predictive back 预览。
                    val backProgress = remember { Animatable(0f) }

                    Box(Modifier.fillMaxSize()) {
                        // 底层（Z 序在下）：返回手势期间渲染上一屏（逐渐露出）。
                        // backProgress 为 0 时不渲染（正常状态只有顶层）。预览不可交互（回调 no-op）。
                        // alpha 随手势进度渐显，和顶层当前屏的渐隐配合。
                        if (backProgress.value > 0f && screenStack.size > 1) {
                            val prevDest = screenStack[screenStack.size - 2]
                            RenderDestination(
                                dest = prevDest,
                                store = store,
                                storagePath = applicationContext.filesDir.absolutePath,
                                client = client,
                                onConnected = {},
                                onProjectClick = {},
                                modifier = Modifier.graphicsLayer {
                                    alpha = backProgress.value
                                },
                            )
                        }

                        // 顶层：当前屏。手势期间向右滑 + 轻微缩放 + 渐隐，让底层露出。
                        // 前进（push）路径用对称 fade；返回路径由 PredictiveBackHandler 的手势进度驱动。
                        AnimatedContent(
                            targetState = screenStack.last(),
                            transitionSpec = { fadeIn() togetherWith fadeOut() },
                            label = "navTransition",
                            modifier = Modifier.graphicsLayer {
                                translationX = size.width * backProgress.value
                                val scale = 1f - 0.05f * backProgress.value
                                scaleX = scale
                                scaleY = scale
                                alpha = 1f - backProgress.value
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
                            )
                        }
                    }

                    // 返回手势：栈深 > 1 时拦截。手势进度实时映射到 backProgress，
                    // 松手完成 → animateTo(1f) 让当前屏完全滑出，再 pop + 归零；
                    // 中途取消 → spring 弹回原位。
                    // 根屏（栈深 ≤ 1）enabled = false，系统接管退出 App 的 predictive back。
                    PredictiveBackHandler(enabled = screenStack.size > 1) { events ->
                        backProgress.snapTo(0f)
                        try {
                            events.collect { e: BackEventCompat ->
                                backProgress.snapTo(e.progress)
                            }
                            // Flow 正常结束 → 手势完成（commit）：先让当前屏继续滑到完全移出，
                            // 再 pop（换栈顶），最后归零（下一帧顶层已是上一屏，归零无视觉跳变）。
                            backProgress.animateTo(1f, spring())
                            pop()
                            backProgress.snapTo(0f)
                        } catch (c: CancellationException) {
                            // Flow 被取消 → 手势取消：spring 弹回原位。
                            backProgress.animateTo(0f, spring())
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
            TreeScreen(client = c, projectName = dest.projectName, modifier = modifier)
        }
    }
}
