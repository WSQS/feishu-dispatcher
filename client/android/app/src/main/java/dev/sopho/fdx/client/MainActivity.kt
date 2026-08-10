package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.BackEventCompat
import androidx.activity.ComponentActivity
import androidx.activity.compose.PredictiveBackHandler
import androidx.activity.compose.setContent
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.Easing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
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
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.network.ViewerClient
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.coroutines.CancellationException
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

// —— 屏间返回动效对齐 AOSP（android15-release）——
// 借鉴源（frameworks/base）：
//   libs/WindowManager/Shell/src/com/android/wm/shell/back/CrossActivityBackAnimation.kt
//   libs/WindowManager/Shell/src/com/android/wm/shell/back/DefaultCrossActivityBackAnimation.kt
//   libs/WindowManager/Shell/src/com/android/wm/shell/animation/Interpolators.java
//   core/java/android/view/animation/BackGestureInterpolator.java
//   core/java/android/window/BackProgressAnimator.java（系统给 app 回传的进度已在此 spring 平滑）

/** pre-commit 进度曲线。对应 CrossActivityBackAnimation.onGestureProgress 里
 *  `gestureInterpolator.getInterpolation(backEvent.progress)`，
 *  即 Interpolators.BACK_GESTURE = BackGestureInterpolator(0.1,0.1,0,1)。 */
private val BackGestureEasing = CubicBezierEasing(0.1f, 0.1f, 0f, 1f)

/** post-commit 曲线。对应 DefaultCrossActivityBackAnimation 的
 *  `postCommitInterpolator = Interpolators.EMPHASIZED`
 *  （Interpolators.createEmphasizedInterpolator 的两段 cubic 路径）。 */
private object EmphasizedEasing : Easing {
    // 段1 x:0→0.166666，控制点 (0.05,0)/(0.133333,0.06)；段2 x:0.166666→1，控制点 (0.208333,0.82)/(0.25,1)
    private fun x1(t: Float) =
        3f * (1 - t) * (1 - t) * t * 0.05f + 3f * (1 - t) * t * t * 0.133333f + t * t * t * 0.166666f

    private fun y1(t: Float) = 3f * (1 - t) * t * t * 0.06f + t * t * t * 0.4f

    private fun x2(t: Float) =
        (1 - t) * (1 - t) * (1 - t) * 0.166666f + 3f * (1 - t) * (1 - t) * t * 0.208333f +
            3f * (1 - t) * t * t * 0.25f + t * t * t

    private fun y2(t: Float) =
        (1 - t) * (1 - t) * (1 - t) * 0.4f + 3f * (1 - t) * (1 - t) * t * 0.82f +
            3f * (1 - t) * t * t + t * t * t

    override fun transform(fraction: Float): Float {
        // PathInterpolator 语义：按 x 反解参数 t 再取 y（两段 x 均单调递增）。
        val seg2 = fraction > 0.166666f
        val t = if (seg2) solveT(::x2, fraction) else solveT(::x1, fraction)
        return if (seg2) y2(t) else y1(t)
    }

    private fun solveT(x: (Float) -> Float, target: Float): Float {
        var lo = 0f
        var hi = 1f
        repeat(24) {
            val mid = (lo + hi) / 2f
            if (x(mid) < target) lo = mid else hi = mid
        }
        return (lo + hi) / 2f
    }
}

// 常量对应 DefaultCrossActivityBackAnimation / CrossActivityBackAnimation 的字段与资源：
private const val POST_COMMIT_MS = 450            // DefaultCrossActivityBackAnimation.POST_COMMIT_DURATION
private const val MAX_SCALE = 0.9f                // CrossActivityBackAnimation.MAX_SCALE（closing/entering 最大缩放）
private const val ENTERING_START_OFFSET_DP = 96f  // R.dimen.cross_activity_back_entering_start_offset
private const val EDGE_MARGIN_DP = 8f             // R.dimen.cross_task_back_vertical_margin

/** 垂直位移。对应 CrossActivityBackAnimation.getYOffset：随手指 Y 位移，
 *  ratio 过 DecelerateInterpolator()，并留 displayBoundsMargin(8dp) 屏边距。 */
private fun calcBackYOffset(
    height: Float,
    scale: Float,
    touchStartY: Float,
    touchY: Float,
    edgeMarginPx: Float,
): Float {
    if (height <= 0f) return 0f // 未布局时避免除 0
    val rawYDelta = touchY - touchStartY
    val yDirection = if (rawYDelta < 0) -1f else 1f
    val deltaYRatio = min(height / 2f, abs(rawYDelta)) / (height / 2f)
    // DecelerateInterpolator() = 1-(1-x)^2
    val interpolatedYRatio = 1f - (1f - deltaYRatio) * (1f - deltaYRatio)
    val maxShiftY = max(0f, (height - height * scale) / 2f - edgeMarginPx)
    return maxShiftY * interpolatedYRatio * yDirection
}

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

                    // 返回手势状态——对齐 AOSP CrossActivityBackAnimation：
                    // - 进度：系统（BackProgressAnimator，core/java/android/window/）已对原始手势
                    //   spring 平滑后才回调给 app，这里再套 BackGestureEasing（onGestureProgress 里
                    //   的 gestureInterpolator）得到「先快后慢」的减速观感。
                    // - pre-commit：closing 缩放/位移对应 preparePreCommitClosingRectMovement，
                    //   entering 对应 preparePreCommitEnteringRectMovement；scrim 恒亮对应
                    //   ensureScrimLayer 的 MAX_SCRIM_ALPHA_LIGHT/DARK。
                    // - post-commit：对应 onGestureCommitted + DefaultCrossActivityBackAnimation
                    //   .onPostCommitProgress（450ms，closing 右滑淡出 alpha max(1-5p,0)，
                    //   entering 滑入全屏）。
                    // - cancel：progress 用 spring 弹回 0（BackProgressAnimator.onBackCancelled）。
                    val gestureProgress = remember { Animatable(0f) }
                    var touchStartY by remember { mutableFloatStateOf(0f) }
                    var touchY by remember { mutableFloatStateOf(0f) }
                    var swipeEdge by remember { mutableIntStateOf(BackEventCompat.EDGE_LEFT) }
                    var isCommitting by remember { mutableStateOf(false) }
                    val commitProgress = remember { Animatable(0f) }
                    var navigatingBack by remember { mutableStateOf(false) }
                    val enteringOffsetPx =
                        with(LocalDensity.current) { ENTERING_START_OFFSET_DP.dp.toPx() }
                    val edgeMarginPx = with(LocalDensity.current) { EDGE_MARGIN_DP.dp.toPx() }
                    val maxScrimAlpha = if (isSystemInDarkTheme()) 0.8f else 0.2f

                    // 视觉进度（AOSP：BackGestureInterpolator 套在系统 spring 平滑后的进度上）
                    val gestureP = BackGestureEasing.transform(gestureProgress.value)
                    // post-commit 进度（450ms 线性驱动，曲线用 AOSP EMPHASIZED）
                    val postP = EmphasizedEasing.transform(commitProgress.value)
                    val gestureActive = gestureProgress.value > 0f
                    val closingAlpha = if (isCommitting) {
                        // 对应 DefaultCrossActivityBackAnimation.onPostCommitProgress 的
                        // `closingAlpha = max(1 - linearProgress*5, 0)`
                        max(1f - 5f * commitProgress.value, 0f)
                    } else {
                        1f
                    }
                    // scrim：pre-commit/cancel 恒亮（ensureScrimLayer 满 alpha），
                    // post-commit 线性淡出（CrossActivityBackAnimation.onPostCommitProgress）
                    val scrimAlpha = maxScrimAlpha * (1f - (if (isCommitting) commitProgress.value else 0f))

                    Box(Modifier.fillMaxSize()) {
                        // 底层（Z 序在下）：返回手势期间渲染上一屏（entering）。
                        // 对应 DefaultCrossActivityBackAnimation.preparePreCommitEnteringRectMovement
                        // （起始全屏左移 96dp、随手势缩放 0.9）+ onPostCommitProgress（滑入到 0）。
                        // gestureActive 为 0 时不渲染（正常状态只有顶层）。预览不可交互（回调 no-op）。
                        if (gestureActive && screenStack.size > 1) {
                            val prevDest = screenStack[screenStack.size - 2]
                            Box(
                                Modifier
                                    .fillMaxSize()
                                    .graphicsLayer {
                                        val p = gestureP
                                        val pc = postP
                                        val scale = 1f - (1f - MAX_SCALE) * p * (1f - pc)
                                        scaleX = scale
                                        scaleY = scale
                                        translationX = -enteringOffsetPx * (1f - pc)
                                        translationY = calcBackYOffset(
                                            size.height, scale, touchStartY, touchY, edgeMarginPx
                                        )
                                    }
                            ) {
                                RenderDestination(
                                    dest = prevDest,
                                    store = store,
                                    storagePath = applicationContext.filesDir.absolutePath,
                                    client = client,
                                    onConnected = {},
                                    onProjectClick = {},
                                )
                            }
                        }

                        // scrim 遮罩：黑色半透明，盖在上一屏上方、当前屏下方。
                        // 对应 CrossActivityBackAnimation.ensureScrimLayer（MAX_SCRIM_ALPHA_LIGHT/DARK）
                        // + onPostCommitProgress：浅色 0.2 / 深色 0.8，pre-commit 恒亮，post-commit 淡出。
                        if (gestureActive && screenStack.size > 1) {
                            Box(
                                Modifier
                                    .fillMaxSize()
                                    .background(Color.Black.copy(alpha = scrimAlpha))
                            )
                        }

                        // 顶层：当前屏（closing）。手势期间缩小 + 跟随手指偏移；post-commit 右滑淡出。
                        // 前进（push）路径用对称 fade；返回（back）路径由 PredictiveBackHandler 驱动，
                        // pop 时直接切换（上一屏已在预览层就位，AOSP 无淡入）。
                        AnimatedContent(
                            targetState = screenStack.last(),
                            transitionSpec = {
                                if (navigatingBack) {
                                    EnterTransition.None togetherWith ExitTransition.None
                                } else {
                                    fadeIn() togetherWith fadeOut()
                                }
                            },
                            label = "navTransition",
                            modifier = Modifier.graphicsLayer {
                                val p = gestureP
                                val pc = postP
                                val scale = 1f - (1f - MAX_SCALE) * p * (1f - pc)
                                scaleX = scale
                                scaleY = scale
                                // AOSP preparePreCommitClosingRectMovement：
                                // 左缘手势目标左缘 (0.1W-8dp)*p；右缘手势仅居中缩放（0.05W*p）。
                                // post-commit 整体再右推 enteringOffset。
                                val left0 = if (swipeEdge == BackEventCompat.EDGE_LEFT) {
                                    (0.1f * size.width - edgeMarginPx).coerceAtLeast(0f) * p
                                } else {
                                    0.05f * size.width * p
                                }
                                val left = left0 + enteringOffsetPx * pc
                                // 中心 pivot 换算：tx = 目标左缘 - 缩放缩进量(W - W*scale)/2
                                translationX = left - 0.05f * size.width * p * (1f - pc)
                                translationY = calcBackYOffset(
                                    size.height, scale, touchStartY, touchY, edgeMarginPx
                                )
                                alpha = closingAlpha
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

                    // 返回切换已生效（None/None 即时完成），复位标志，避免影响后续前进的 fade。
                    LaunchedEffect(screenStack.last()) { navigatingBack = false }

                    // 返回手势：栈深 > 1 时拦截。手势进度实时驱动当前屏的缩小/位移；
                    // 松手完成 → 对应 onGestureCommitted 的 post-commit 动画
                    // （450ms，closing 右滑淡出 + entering 滑入），再 pop；
                    // 中途取消 → progress 用 spring 弹回 0（BackProgressAnimator.onBackCancelled）。
                    // 根屏（栈深 ≤ 1）enabled = false，系统接管退出 App 的 predictive back。
                    PredictiveBackHandler(enabled = screenStack.size > 1) { events ->
                        isCommitting = false
                        commitProgress.snapTo(0f)
                        var firstEvent = true
                        try {
                            events.collect { e: BackEventCompat ->
                                if (firstEvent) {
                                    touchStartY = e.touchY
                                    firstEvent = false
                                }
                                touchY = e.touchY
                                swipeEdge = e.swipeEdge
                                // 系统（BackProgressAnimator）已 spring 平滑 progress，这里直接采纳
                                gestureProgress.snapTo(e.progress)
                            }
                            // Flow 正常结束 → 手势完成（commit）：450ms post-commit 动画。
                            // AOSP 里 post-commit 时长固定为 POST_COMMIT_DURATION，
                            // 这里用线性 tween 驱动、曲线由 EmphasizedEasing 施加（见 onPostCommitProgress）。
                            isCommitting = true
                            commitProgress.snapTo(0f)
                            commitProgress.animateTo(
                                1f,
                                tween(durationMillis = POST_COMMIT_MS, easing = LinearEasing)
                            )
                            navigatingBack = true
                            pop()
                            gestureProgress.snapTo(0f)
                            isCommitting = false
                        } catch (c: CancellationException) {
                            // Flow 被取消 → 手势取消：progress 用 spring 弹回 0。
                            // 对应 BackProgressAnimator.onBackCancelled 的
                            // SpringForce(STIFFNESS_MEDIUM, DAMPING_RATIO_NO_BOUNCY)，
                            // 即 Compose spring() 默认参数。
                            gestureProgress.animateTo(0f, spring())
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
